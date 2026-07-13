import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orca_auto.orca.commands.run_inp import cmd_run_inp, submit_reaction_dir_to_queue
from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RuntimeConfig
from orca_auto.orca.queue.adapter import enqueue, list_queue, queue_entry_metadata


def _make_cfg(tmp: str, *, max_cores: int = 8, max_memory_gb: int = 32) -> AppConfig:
    root = Path(tmp)
    fake_orca = root / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = AppConfig(
        runtime=RuntimeConfig(allowed_root=tmp),
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
    return SimpleNamespace(**defaults)


class TestRunInpSubmit(unittest.TestCase):
    @patch("orca_auto.orca.commands.run_inp._emit_queued_submission")
    @patch("orca_auto.orca.commands.run_inp.submit_reaction_dir_to_queue")
    @patch("orca_auto.orca.commands.run_inp._cmd_run_inp_execute", return_value=0)
    def test_submit_always_enqueues_without_attempting_direct_execution(
        self,
        mock_execute: MagicMock,
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
        mock_execute.assert_not_called()
        mock_submit_to_queue.assert_called_once()

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp._run_inp_submission.create_queued_submission")
    @patch("orca_auto.orca.commands.run_inp._cmd_run_inp_execute", return_value=0)
    def test_submit_rejects_when_active_queue_entry_exists_for_same_reaction_dir(
        self,
        mock_execute: MagicMock,
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
        mock_execute.assert_not_called()
        mock_create_queued_submission.assert_not_called()

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp._run_inp_submission.create_queued_submission")
    @patch("orca_auto.orca.commands.run_inp._cmd_run_inp_execute", return_value=0)
    def test_submit_rejects_when_same_reaction_dir_is_already_running_directly(
        self,
        mock_execute: MagicMock,
        mock_create_queued_submission: MagicMock,
        mock_load_config: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock_load_config.return_value = _make_cfg(tmp)
            reaction_dir = root / "rxn"
            _write_inp(reaction_dir)
            (reaction_dir / "run.lock").write_text(
                json.dumps({"pid": os.getpid(), "started_at": "2026-03-20T00:00:00+00:00"}),
                encoding="utf-8",
            )

            rc = cmd_run_inp(_make_args(root, reaction_dir))

        self.assertEqual(rc, 1)
        mock_execute.assert_not_called()
        mock_create_queued_submission.assert_not_called()

    @patch("orca_auto.orca.commands.run_inp._emit_queued_submission")
    @patch("orca_auto.orca.commands.run_inp.submit_reaction_dir_to_queue")
    @patch("orca_auto.orca.commands.run_inp._cmd_run_inp_execute", return_value=0)
    def test_submit_queues_completed_output_for_worker_reconciliation(
        self,
        mock_execute: MagicMock,
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
        mock_execute.assert_not_called()
        mock_submit_to_queue.assert_called_once()

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.queue.worker.read_worker_pid", return_value=None)
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
            self.assertTrue(
                Path(metadata["selected_inp"]).is_relative_to(
                    reaction_dir / ".orca_auto_orca_executions"
                )
            )
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
            inp_text = (reaction_dir / "rxn.inp").read_text(encoding="utf-8")
            self.assertIn("%pal", inp_text)
            self.assertIn("nprocs 8", inp_text)
            self.assertIn("%maxcore 4096", inp_text)
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

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.queue.worker.read_worker_pid", return_value=4321)
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

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.queue.worker.read_worker_pid", return_value=None)
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
            self.assertTrue(
                Path(metadata["selected_inp"]).is_relative_to(
                    reaction_dir / ".orca_auto_orca_executions"
                )
            )
            self.assertEqual(metadata["selected_input_xyz"], xyz_path)
            self.assertEqual(metadata["selected_input_path"], xyz_path)
            self.assertEqual(metadata["job_type"], "opt")

            tracking_records = json.loads((root / "job_locations.json").read_text(encoding="utf-8"))
            self.assertEqual(tracking_records[0]["selected_input_xyz"], xyz_path)

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.queue.worker.read_worker_pid", return_value=None)
    @patch(
        "orca_auto.orca.commands.run_inp._run_inp_submission.upsert_queued_job_record",
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

    @patch("orca_auto.orca.commands.run_inp.load_config")
    @patch("orca_auto.orca.commands.run_inp.notify_queue_enqueued_event", return_value=True)
    @patch("orca_auto.orca.queue.worker.read_worker_pid", return_value=None)
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
