import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orca_auto.flow._orca_stage_materialization import validate_workflow_orca_input_bytes
from orca_auto.orca.commands.run_inp import cmd_run_inp
from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RetryRuntimeConfig
from orca_auto.orca.queue.adapter import enqueue, list_queue, queue_entry_metadata
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.submission import submit_reaction_dir_to_queue


def _make_cfg(tmp: str, *, max_cores: int = 8, max_memory_gb: int = 32) -> AppConfig:
    root = Path(tmp)
    fake_orca = root / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = AppConfig(
        runtime=RetryRuntimeConfig(allowed_root=tmp),
        paths=PathsConfig(orca_executable=str(fake_orca)),
        resources=CommonResourceConfig(
            max_cores_per_task=max_cores,
            max_memory_gb_per_task=max_memory_gb,
        ),
    )
    cfg.runtime.max_concurrent = 1
    return cfg


def _write_inp(reaction_dir: Path, content: str | None = None) -> None:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    (reaction_dir / "rxn.inp").write_text(
        content or "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )


def _make_args(root: Path, reaction_dir: Path, **overrides) -> SimpleNamespace:
    defaults = {
        "config": str(root / "orca_auto.yaml"),
        "reaction_dir": str(reaction_dir),
        "force": False,
        "priority": 10,
    }
    defaults.update(overrides)
    workflow_task_kind = defaults.get("workflow_task_kind")
    if isinstance(workflow_task_kind, str) and "bound_selected_validator" not in defaults:

        def validate_bound_selected(bound_inp: Path, payload: bytes) -> None:
            validate_workflow_orca_input_bytes(
                task_kind=workflow_task_kind,
                inp_path=bound_inp,
                input_bytes=payload,
            )

        defaults["bound_selected_validator"] = validate_bound_selected
    return SimpleNamespace(**defaults)


class TestRunInpSubmit(unittest.TestCase):
    @patch("orca_auto.orca.commands.run_inp._emit_queued_submission")
    @patch("orca_auto.orca.commands.run_inp.submission.submit_reaction_dir_to_queue")
    def test_submit_always_enqueues_without_attempting_direct_execution(
        self,
        mock_submit_to_queue: MagicMock,
        _mock_emit_queued: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)
            mock_submit_to_queue.return_value = SimpleNamespace(
                status="submitted",
                reason="",
                stderr="",
                context=SimpleNamespace(reaction_dir=reaction_dir),
                queued_result=SimpleNamespace(
                    entry=object(),
                    worker_info=SimpleNamespace(status=None, pid=None, log_file=None, detail=None),
                ),
            )

            rc = cmd_run_inp(_make_args(root, reaction_dir))

        self.assertEqual(rc, 0)
        mock_submit_to_queue.assert_called_once()

    @patch("orca_auto.orca.commands.run_inp.submission.queue_adapter.queue_entry_force")
    @patch("orca_auto.orca.commands.run_inp.submission.queue_adapter.queue_entry_priority")
    @patch("orca_auto.orca.commands.run_inp.submission.queue_adapter.queue_entry_task_id")
    @patch("orca_auto.orca.commands.run_inp.submission.queue_adapter.queue_entry_id")
    @patch("orca_auto.orca.commands.run_inp.submission.submit_reaction_dir_to_queue")
    def test_json_submission_emits_one_parseable_document(
        self,
        mock_submit_to_queue: MagicMock,
        mock_entry_id: MagicMock,
        mock_task_id: MagicMock,
        mock_priority: MagicMock,
        mock_force: MagicMock,
    ) -> None:
        reaction_dir = Path("/tmp/orca-json-job")
        mock_entry_id.return_value = "q-json"
        mock_task_id.return_value = "orca-json"
        mock_priority.return_value = 7
        mock_force.return_value = False
        mock_submit_to_queue.return_value = SimpleNamespace(
            status="submitted",
            reason="",
            stderr="",
            context=SimpleNamespace(reaction_dir=reaction_dir),
            queued_result=SimpleNamespace(
                entry=object(),
                worker_info=SimpleNamespace(
                    status="inactive",
                    pid=None,
                    log_file="/tmp/q-json.log",
                    detail=None,
                ),
            ),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            rc = cmd_run_inp(SimpleNamespace(config="/tmp/orca.yaml", priority=7, json=True))

        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "queued",
                "job_dir": str(reaction_dir),
                "queue_id": "q-json",
                "job_id": "orca-json",
                "priority": 7,
                "worker": "inactive",
                "worker_log": "/tmp/q-json.log",
            },
        )

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.create_queued_submission")
    def test_submit_rejects_when_active_queue_entry_exists_for_same_reaction_dir(
        self,
        mock_create_queued_submission: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)
            enqueue(root, str(reaction_dir))

            rc = cmd_run_inp(_make_args(root, reaction_dir))

        self.assertEqual(rc, 1)
        mock_create_queued_submission.assert_not_called()

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.create_queued_submission")
    def test_submit_rejects_when_same_reaction_dir_is_already_running_directly(
        self,
        mock_create_queued_submission: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)
            with acquire_run_lock(reaction_dir):
                rc = cmd_run_inp(_make_args(root, reaction_dir))

        self.assertEqual(rc, 1)
        mock_create_queued_submission.assert_not_called()

    @patch("orca_auto.orca.commands.run_inp._emit_queued_submission")
    @patch("orca_auto.orca.commands.run_inp.submission.submit_reaction_dir_to_queue")
    def test_submit_queues_completed_output_for_worker_reconciliation(
        self,
        mock_submit_to_queue: MagicMock,
        _mock_emit_queued: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)
            (reaction_dir / "rxn.out").write_text(
                "****ORCA TERMINATED NORMALLY****\n", encoding="utf-8"
            )
            mock_submit_to_queue.return_value = SimpleNamespace(
                status="submitted",
                reason="",
                stderr="",
                context=SimpleNamespace(reaction_dir=reaction_dir),
                queued_result=SimpleNamespace(
                    entry=object(),
                    worker_info=SimpleNamespace(status=None, pid=None, log_file=None, detail=None),
                ),
            )

            rc = cmd_run_inp(_make_args(root, reaction_dir))

        self.assertEqual(rc, 0)
        mock_submit_to_queue.assert_called_once()

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.submission.read_worker_pid", return_value=None)
    def test_submit_reaction_dir_to_queue_reports_inactive_worker_without_autostart(
        self,
        mock_read_worker_pid: MagicMock,
        mock_notify_queue: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            submission = submit_reaction_dir_to_queue(_make_args(root, reaction_dir, priority=3))

            entries = list_queue(root)

            self.assertEqual(submission.status, "submitted")
            result = submission.queued_result
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            metadata = queue_entry_metadata(entry)
            self.assertEqual(entry.priority, 3)
            self.assertEqual(entry.app_name, "orca_auto_orca")
            self.assertTrue(entry.task_id.startswith("orca_"))
            self.assertEqual(metadata["source_selected_inp"], str(reaction_dir / "rxn.inp"))
            execution_dir = Path(metadata["execution_snapshot"]["execution_dir"])
            self.assertEqual(execution_dir.parent, reaction_dir.resolve())
            self.assertEqual(Path(metadata["selected_inp"]), execution_dir / "rxn.inp")
            self.assertFalse((reaction_dir / ".orca_auto_orca_executions").exists())
            self.assertFalse((reaction_dir / ".orca_auto_input_snapshots").exists())
            self.assertEqual(
                metadata["execution_snapshot"]["selected_inp"],
                metadata["selected_inp"],
            )
            self.assertEqual(metadata["selected_input_path"], str(reaction_dir / "rxn.inp"))
            self.assertEqual(metadata["selected_input_xyz"], "")
            self.assertEqual(metadata["max_retries"], 0)
            self.assertEqual(metadata["submitted_via"], "run_inp")
            self.assertEqual(metadata["job_type"], "opt")
            self.assertEqual(
                metadata["worker_log"],
                str((root / "logs" / f"{entry.queue_id}.log").resolve()),
            )
            self.assertTrue(str(metadata["molecule_key"]).strip())
            self.assertEqual(metadata["resource_request"]["max_cores"], 8)
            self.assertEqual(metadata["resource_request"]["max_memory_gb"], 32)
            self.assertEqual(metadata["resource_actual"]["max_cores"], 8)
            self.assertEqual(metadata["resource_actual"]["max_memory_gb"], 32)
            source_text = (reaction_dir / "rxn.inp").read_text(encoding="utf-8")
            self.assertNotIn("%pal", source_text)
            self.assertNotIn("%maxcore", source_text)
            private_text = Path(metadata["selected_inp"]).read_text(encoding="utf-8")
            self.assertIn("%pal", private_text)
            self.assertIn("nprocs 8", private_text)
            self.assertIn("%maxcore 4096", private_text)
            tracking_records = json.loads((root / "job_locations.json").read_text(encoding="utf-8"))
            self.assertEqual(len(tracking_records), 1)
            self.assertEqual(tracking_records[0]["job_id"], entry.task_id)
            self.assertEqual(tracking_records[0]["status"], "queued")
            self.assertEqual(tracking_records[0]["original_run_dir"], str(reaction_dir.resolve()))
            self.assertEqual(
                tracking_records[0]["selected_input_xyz"], str((reaction_dir / "rxn.inp").resolve())
            )
            self.assertEqual(result.worker_info.status, "inactive")
            self.assertIsNone(result.worker_info.pid)
            self.assertEqual(result.worker_info.log_file, metadata["worker_log"])
            mock_read_worker_pid.assert_called_once()
            mock_notify_queue.assert_called_once()

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.submission.read_worker_pid", return_value=None)
    def test_workflow_submission_binds_valid_rewritten_snapshot_bytes(
        self,
        _mock_read_worker_pid: MagicMock,
        _mock_notify_queue: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content="! OptTS NumFreq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            )
            durable_inp = (reaction_dir / "rxn.inp").resolve()

            submission = submit_reaction_dir_to_queue(
                _make_args(
                    root,
                    reaction_dir,
                    expected_selected_inp=str(durable_inp),
                    workflow_task_kind="optts_freq",
                )
            )

            self.assertEqual(submission.status, "submitted")
            [entry] = list_queue(root)
            snapshot_inp = Path(queue_entry_metadata(entry)["selected_inp"])
            snapshot_text = snapshot_inp.read_text(encoding="utf-8")
            self.assertIn("OptTS NumFreq", snapshot_text)
            self.assertIn("%pal", snapshot_text)

    @patch("orca_auto.orca.submission.load_config")
    def test_workflow_submission_requires_upper_layer_bound_payload_validator(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content="! OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            )
            durable_inp = (reaction_dir / "rxn.inp").resolve()

            submission = submit_reaction_dir_to_queue(
                _make_args(
                    root,
                    reaction_dir,
                    expected_selected_inp=str(durable_inp),
                    workflow_task_kind="optts_freq",
                    bound_selected_validator=None,
                )
            )

            self.assertEqual(submission.status, "failed")
            self.assertEqual(submission.reason, "invalid_submission_input")
            self.assertIn("upper-layer bound-payload validator", submission.stderr)
            self.assertEqual(list_queue(root), [])

    @patch("orca_auto.orca.submission.load_config")
    def test_workflow_submission_rejects_newer_input_than_durable_selection(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content="! OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            )
            durable_inp = (reaction_dir / "rxn.inp").resolve()
            newer_inp = reaction_dir / "newer.inp"
            newer_inp.write_text(
                "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                encoding="utf-8",
            )
            durable_stat = durable_inp.stat()
            newer_stat = newer_inp.stat()
            os.utime(
                newer_inp,
                ns=(newer_stat.st_atime_ns, durable_stat.st_mtime_ns + 1_000_000),
            )

            submission = submit_reaction_dir_to_queue(
                _make_args(
                    root,
                    reaction_dir,
                    expected_selected_inp=str(durable_inp),
                    workflow_task_kind="optts_freq",
                )
            )

            self.assertEqual(submission.status, "failed")
            self.assertEqual(submission.reason, "invalid_submission_input")
            self.assertIn("durable selected input", submission.stderr)
            self.assertEqual(list_queue(root), [])

    @patch("orca_auto.orca.submission.load_config")
    def test_workflow_submission_validates_snapshot_bound_bytes_after_source_replacement(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content="! OptTS Freq\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            )
            durable_inp = (reaction_dir / "rxn.inp").resolve()

            def replace_after_selection(_allowed_root: Path, _reaction_dir: Path) -> None:
                durable_inp.write_text(
                    "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                    encoding="utf-8",
                )
                return None

            with patch(
                "orca_auto.orca.submission.find_submission_conflict",
                side_effect=replace_after_selection,
            ):
                submission = submit_reaction_dir_to_queue(
                    _make_args(
                        root,
                        reaction_dir,
                        expected_selected_inp=str(durable_inp),
                        workflow_task_kind="optts_freq",
                    )
                )

            self.assertEqual(submission.status, "failed")
            self.assertEqual(submission.reason, "invalid_submission_input")
            self.assertIn("route-role mismatch", submission.stderr)
            self.assertEqual(list_queue(root), [])
            self.assertFalse((reaction_dir / ".orca_auto_orca_executions").exists())

    @patch("orca_auto.orca.submission.load_config")
    def test_workflow_sp_snapshot_callback_rejects_replaced_optimization_input(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content="! HF TightSCF\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            )
            durable_inp = (reaction_dir / "rxn.inp").resolve()

            def replace_after_selection(_allowed_root: Path, _reaction_dir: Path) -> None:
                durable_inp.write_text(
                    "! HF Opt TightSCF\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
                    encoding="utf-8",
                )
                return None

            with patch(
                "orca_auto.orca.submission.find_submission_conflict",
                side_effect=replace_after_selection,
            ):
                submission = submit_reaction_dir_to_queue(
                    _make_args(
                        root,
                        reaction_dir,
                        expected_selected_inp=str(durable_inp),
                        workflow_task_kind="sp",
                    )
                )

            self.assertEqual(submission.status, "failed")
            self.assertEqual(submission.reason, "invalid_submission_input")
            self.assertIn("single-point", submission.stderr)
            self.assertEqual(list_queue(root), [])
            self.assertFalse((reaction_dir / ".orca_auto_orca_executions").exists())

    @patch("orca_auto.orca.submission.load_config")
    def test_workflow_snapshot_callback_rejects_unknown_task_kind(
        self,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)
            durable_inp = (reaction_dir / "rxn.inp").resolve()

            submission = submit_reaction_dir_to_queue(
                _make_args(
                    root,
                    reaction_dir,
                    expected_selected_inp=str(durable_inp),
                    workflow_task_kind="geometry_opt",
                )
            )

            self.assertEqual(submission.status, "failed")
            self.assertEqual(submission.reason, "invalid_submission_input")
            self.assertIn("unsupported workflow ORCA task_kind", submission.stderr)
            self.assertEqual(list_queue(root), [])
            self.assertFalse((reaction_dir / ".orca_auto_orca_executions").exists())

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.submission.read_worker_pid", return_value=4321)
    def test_submit_reaction_dir_to_queue_reports_running_worker_pid(
        self,
        mock_read_worker_pid: MagicMock,
        mock_notify_queue: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            submission = submit_reaction_dir_to_queue(_make_args(root, reaction_dir))

            self.assertEqual(submission.status, "submitted")
            result = submission.queued_result
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(len(list_queue(root)), 1)
            [entry] = list_queue(root)
            metadata = queue_entry_metadata(entry)
            self.assertEqual(result.worker_info.status, "running")
            self.assertEqual(result.worker_info.pid, 4321)
            self.assertEqual(result.worker_info.log_file, metadata["worker_log"])
            mock_read_worker_pid.assert_called_once()
            mock_notify_queue.assert_called_once()

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.submission.read_worker_pid", return_value=None)
    def test_submit_reaction_dir_to_queue_separates_inp_and_xyzfile_artifacts(
        self,
        _mock_read_worker_pid: MagicMock,
        _mock_notify_queue: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content="! Opt\n* xyzfile 0 1 geom.xyz\n",
            )
            (reaction_dir / "geom.xyz").write_text(
                "2\ncomment\nH 0 0 0\nH 0 0 0.74\n",
                encoding="utf-8",
            )

            submission = submit_reaction_dir_to_queue(_make_args(root, reaction_dir))

            self.assertEqual(submission.status, "submitted")
            entry = list_queue(root)[0]
            metadata = queue_entry_metadata(entry)
            xyz_path = str((reaction_dir / "geom.xyz").resolve())
            self.assertEqual(metadata["source_selected_inp"], str(reaction_dir / "rxn.inp"))
            execution_dir = Path(metadata["execution_snapshot"]["execution_dir"])
            self.assertEqual(execution_dir.parent, reaction_dir.resolve())
            self.assertEqual(Path(metadata["selected_inp"]), execution_dir / "rxn.inp")
            self.assertEqual(
                (execution_dir / "geom.xyz").read_bytes(), (reaction_dir / "geom.xyz").read_bytes()
            )
            self.assertEqual(metadata["selected_input_xyz"], xyz_path)
            self.assertEqual(metadata["selected_input_path"], xyz_path)
            self.assertEqual(metadata["job_type"], "opt")

            tracking_records = json.loads((root / "job_locations.json").read_text(encoding="utf-8"))
            self.assertEqual(tracking_records[0]["selected_input_xyz"], xyz_path)

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.submission.read_worker_pid", return_value=None)
    @patch(
        "orca_auto.orca.submission.upsert_queued_job_record",
        side_effect=RuntimeError("index write failed"),
    )
    def test_submit_reaction_dir_to_queue_succeeds_when_tracking_side_effect_fails(
        self,
        mock_upsert: MagicMock,
        mock_read_worker_pid: MagicMock,
        mock_notify_queue: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)

            submission = submit_reaction_dir_to_queue(_make_args(root, reaction_dir, priority=3))

            entries = list_queue(root)

            self.assertEqual(submission.status, "submitted")
            self.assertEqual(len(entries), 1)
            self.assertFalse((root / "job_locations.json").exists())
            result = submission.queued_result
            self.assertIsNotNone(result)
            assert result is not None
            self.assertIn("queue submission succeeded", result.worker_info.detail or "")
            mock_upsert.assert_called_once()
            mock_read_worker_pid.assert_called_once()
            mock_notify_queue.assert_called_once()

    @patch("orca_auto.orca.submission.load_config")
    @patch("orca_auto.orca.submission.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.submission.read_worker_pid", return_value=None)
    def test_submit_reaction_dir_to_queue_reads_metadata_from_input_even_when_flags_are_present(
        self,
        mock_read_worker_pid: MagicMock,
        mock_notify_queue: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            mock_load_config.return_value = cfg
            reaction_dir = root / "rxn"
            _write_inp(
                reaction_dir,
                content=(
                    "! Opt\n"
                    "%pal\n"
                    "  nprocs 12\n"
                    "end\n"
                    "%maxcore 2048\n"
                    "* xyz 0 1\n"
                    "H 0 0 0\n"
                    "H 0 0 0.74\n"
                    "*\n"
                ),
            )

            submission = submit_reaction_dir_to_queue(
                _make_args(root, reaction_dir, max_cores=20, max_memory_gb=80)
            )

            entries = list_queue(root)

            self.assertEqual(submission.status, "submitted")
            self.assertEqual(len(entries), 1)
            metadata = queue_entry_metadata(entries[0])
            self.assertEqual(metadata["resource_request"]["max_cores"], 12)
            self.assertEqual(metadata["resource_request"]["max_memory_gb"], 24)
            self.assertEqual(metadata["resource_actual"]["max_cores"], 12)
            self.assertEqual(metadata["resource_actual"]["max_memory_gb"], 24)
            inp_text = (reaction_dir / "rxn.inp").read_text(encoding="utf-8")
            self.assertIn("nprocs 12", inp_text)
            self.assertIn("%maxcore 2048", inp_text)
            mock_read_worker_pid.assert_called_once()
            mock_notify_queue.assert_called_once()
