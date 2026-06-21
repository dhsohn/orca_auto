"""Tests for orca_auto.orca.queue_worker foreground worker job execution helpers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_auto.core.admission import (
    active_slot_count,
    list_slots,
    release_slot,
    reserve_slot,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca import queue_worker as queue_worker_mod
from orca_auto.orca.config import AppConfig, RuntimeConfig, TelegramConfig
from orca_auto.orca.queue_adapter import (
    cancel,
    dequeue_next,
    enqueue,
    list_queue,
    mark_cancelled,
    queue_entry_reaction_dir,
    requeue_running_entry,
)
from orca_auto.orca.queue_worker import (
    DEFAULT_MAX_CONCURRENT,
    QueueWorker,
    _get_run_id_from_state,
    _notify_terminal_job_from_state,
    _record_cancelled_run_state,
    _RunningJob,
    _terminate_process,
    read_worker_pid,
)
from orca_auto.orca.state import (
    finalize_state,
    load_state,
    new_state,
    report_json_path,
    save_state,
)
from tests.engine_artifact_helpers import orca_artifact_payload
from tests.process_helpers import patch_missing_process_group, preserved_signal_handlers
from tests.queue_worker_helpers import (
    make_queue_worker_cfg as _make_cfg,
)
from tests.queue_worker_helpers import (
    write_completed_run_state as _write_completed_run_state,
)


def _command_arg(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_tracking_callbacks_use_current_queue_worker_symbols() -> None:
    def notify_run_finished_event(*args: object, **kwargs: object) -> bool:
        return True

    with patch(
        "orca_auto.orca.queue_worker.notify_run_finished_event",
        new=notify_run_finished_event,
    ):
        callbacks = queue_worker_mod._tracking_callbacks()

    assert callbacks.notify_run_finished_event is notify_run_finished_event
    assert callbacks.upsert_job_record is queue_worker_mod.upsert_job_record
    assert callbacks.queue_entry_task_id is queue_worker_mod.queue_entry_task_id


def test_lifecycle_callbacks_use_current_queue_worker_symbols() -> None:
    def terminate_process(proc: object) -> None:
        del proc

    with patch("orca_auto.orca.queue_worker._terminate_process", new=terminate_process):
        callbacks = queue_worker_mod._lifecycle_callbacks()

    worker = MagicMock()
    job = object()
    assert callbacks.terminate_process is terminate_process
    assert callbacks.requeue_running_entry is queue_worker_mod.requeue_running_entry
    assert callbacks.on_completed is not None
    callbacks.on_completed(worker, job)
    worker._auto_organize_terminal_job.assert_called_once_with(job)


def test_record_cancelled_run_state_writes_terminal_cancelled(tmp_path: Path) -> None:
    # A cancelled run is stopped by a signal and never writes its own terminal
    # result, so the worker records a cancelled outcome on its behalf. Without it
    # the run state lingers as "running" (job never leaves the list, no notify).
    state = new_state(tmp_path, tmp_path / "job.inp", max_retries=3)
    state["status"] = "running"
    save_state(tmp_path, state)

    run_id = _record_cancelled_run_state(tmp_path)

    assert run_id == state["run_id"]
    written = load_state(tmp_path)
    assert written is not None
    assert written["status"] == "cancelled"
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "cancelled"
    assert written["final_result"]["reason"] == "cancel_requested"


def test_record_cancelled_run_state_keeps_existing_terminal_result(tmp_path: Path) -> None:
    # If a real terminal outcome landed just before cancellation, don't clobber it.
    state = new_state(tmp_path, tmp_path / "job.inp", max_retries=3)
    finalize_state(
        tmp_path,
        state,
        status="completed",
        final_result={
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "t",
            "last_out_path": None,
        },
    )

    _record_cancelled_run_state(tmp_path)

    written = load_state(tmp_path)
    assert written is not None
    assert written["final_result"]["status"] == "completed"


class TestTerminateProcess(unittest.TestCase):
    def setUp(self) -> None:
        self._killpg_patcher = patch_missing_process_group(
            "orca_auto.core.queue.processes.os.killpg"
        )
        self._killpg_patcher.start()

    def tearDown(self) -> None:
        self._killpg_patcher.stop()

    def test_already_terminated(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 0
        _terminate_process(proc)
        proc.terminate.assert_not_called()

    def test_terminate_success(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1234
        proc.poll.side_effect = [None, 0, 0]
        _terminate_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    def test_terminate_ignores_errors(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1234
        proc.terminate.side_effect = RuntimeError("nope")
        proc.poll.side_effect = [None, 0, 0]
        _terminate_process(proc)
        proc.terminate.assert_called_once()

    def test_escalate_to_kill(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1234
        proc.poll.side_effect = [None, None, None, None]
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="worker", timeout=10),
            subprocess.TimeoutExpired(cmd="worker", timeout=5),
        ]
        _terminate_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


class TestGetRunIdFromState(unittest.TestCase):
    def test_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = _get_run_id_from_state(tmp)
            self.assertIsNone(result)

    def test_with_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            save_state(
                Path(tmp),
                {
                    "run_id": "test_run_123",
                    "reaction_dir": str(tmp),
                    "selected_inp": "",
                    "max_retries": 0,
                    "status": "completed",
                    "attempts": [],
                    "final_result": {},
                },
            )
            result = _get_run_id_from_state(tmp)
            self.assertEqual(result, "test_run_123")


class TestReadWorkerPid(unittest.TestCase):
    def test_no_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_worker_pid(Path(tmp)))

    def test_stale_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path = root / "queue_worker.pid"
            pid_path.write_text("999999999")  # non-existent pid
            result = read_worker_pid(root)
            self.assertIsNone(result)
            # PID file should be cleaned up
            self.assertFalse(pid_path.exists())

    def test_invalid_pid_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_path = root / "queue_worker.pid"
            pid_path.write_text("not_a_number")
            self.assertIsNone(read_worker_pid(root))


class TestQueueWorkerInit(unittest.TestCase):
    def test_max_concurrent_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(Path(tmp) / "config.yaml"), max_concurrent=0)
            self.assertEqual(worker.max_concurrent, 1)

    def test_default_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(Path(tmp) / "config.yaml"))
            self.assertEqual(worker.max_concurrent, DEFAULT_MAX_CONCURRENT)
            self.assertFalse(worker.auto_organize)
            self.assertFalse(worker._shutdown_requested)
            self.assertEqual(len(worker._running), 0)


class TestQueueWorkerMethods(unittest.TestCase):
    def setUp(self) -> None:
        self._signal_guard = preserved_signal_handlers(signal.SIGTERM, signal.SIGINT)
        self._signal_guard.__enter__()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.cfg = _make_cfg(self._tmpdir.name)
        self.worker = QueueWorker(self.cfg, str(self.root / "config.yaml"), max_concurrent=2)

    def tearDown(self) -> None:
        self._signal_guard.__exit__(None, None, None)
        self._tmpdir.cleanup()

    def test_pid_file_write_and_remove(self) -> None:
        self.worker._write_pid_file()
        pid_path = self.worker._pid_file_path()
        self.assertTrue(pid_path.exists())
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        self.assertIsInstance(payload.get("pid"), int)
        self.worker._remove_pid_file()
        self.assertFalse(pid_path.exists())

    def test_remove_pid_file_missing(self) -> None:
        # Should not raise
        self.worker._remove_pid_file()

    def test_pid_file_path(self) -> None:
        path = self.worker._pid_file_path()
        self.assertEqual(path.name, "queue_worker.pid")
        self.assertEqual(path.parent, self.root)

    def test_install_signal_handlers(self) -> None:
        with patch("orca_auto.core.queue.worker.signal.signal"):
            self.worker._install_signal_handlers()

    def test_fill_slots_empty_queue(self) -> None:
        self.worker._fill_slots()
        self.assertEqual(len(self.worker._running), 0)

    @patch("orca_auto.orca.queue_worker.start_background_process")
    def test_start_job(self, mock_start_background_process: MagicMock) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_start_background_process.return_value = mock_proc
        entry = QueueEntry(
            queue_id="q_test",
            app_name="orca_auto_orca",
            task_id="task_test_123",
            task_kind="orca_run_inp",
            engine="orca",
            metadata={
                "reaction_dir": str(self.root / "mol_A"),
                "force": False,
                "worker_log": "/tmp/unsafe-worker.log",
            },
        )
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        self.worker._start_job(self.root, entry, admission_token=token or "")
        self.assertIn("q_test", self.worker._running)
        mock_start_background_process.assert_called_once()
        command = mock_start_background_process.call_args.args[0]
        self.assertEqual(
            mock_start_background_process.call_args.kwargs["log_path"],
            str((self.root / "logs" / "q_test.log").resolve()),
        )
        self.assertIn("orca_auto.core.engines.worker_child", command)
        self.assertEqual(_command_arg(command, "--engine"), "orca")
        self.assertEqual(_command_arg(command, "--queue-root"), str(self.root))
        self.assertEqual(_command_arg(command, "--queue-id"), "q_test")
        self.assertEqual(_command_arg(command, "--admission-token"), token or "")
        self.assertNotIn("--reaction-dir", command)

    @patch("orca_auto.orca.queue_worker.upsert_job_record")
    @patch(
        "orca_auto.orca.queue_worker.resolve_job_metadata",
        side_effect=AssertionError("should use queue metadata"),
    )
    @patch("orca_auto.orca.queue_worker.start_background_process")
    def test_start_job_prefers_queue_metadata_for_tracking(
        self,
        mock_start_background_process: MagicMock,
        mock_resolve_job_metadata: MagicMock,
        mock_upsert_job_record: MagicMock,
    ) -> None:
        reaction_dir = self.root / "mol_meta"
        reaction_dir.mkdir()
        selected_inp = reaction_dir / "rxn.inp"
        selected_inp.write_text("! Opt\n", encoding="utf-8")
        mock_proc = MagicMock()
        mock_proc.pid = 4322
        mock_start_background_process.return_value = mock_proc
        entry = QueueEntry(
            queue_id="q_meta",
            app_name="orca_auto_orca",
            task_id="task_meta_123",
            task_kind="orca_run_inp",
            engine="orca",
            metadata={
                "reaction_dir": str(reaction_dir),
                "force": False,
                "selected_inp": str(selected_inp),
                "selected_input_xyz": str(selected_inp),
                "job_type": "opt",
                "molecule_key": "H2",
                "resource_request": {"max_cores": 4, "max_memory_gb": 12},
                "resource_actual": {"max_cores": 4, "max_memory_gb": 12},
            },
        )

        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        self.worker._start_job(self.root, entry, admission_token=token or "")

        mock_resolve_job_metadata.assert_not_called()
        mock_upsert_job_record.assert_called_once_with(
            self.worker.cfg,
            job_id="task_meta_123",
            status="running",
            job_dir=reaction_dir.resolve(),
            job_type="opt",
            selected_input_xyz=str(selected_inp),
            molecule_key="H2",
            resource_request={"max_cores": 4, "max_memory_gb": 12},
            resource_actual={"max_cores": 4, "max_memory_gb": 12},
        )

    @patch(
        "orca_auto.orca.queue_worker.start_background_process",
        side_effect=OSError("spawn failed"),
    )
    def test_start_job_oserror(self, mock_start_background_process: MagicMock) -> None:
        rxn = self.root / "mol_err"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        dequeue_next(self.root)
        self.worker._start_job(self.root, entry, admission_token=token or "")
        self.assertNotIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 0)

    @patch(
        "orca_auto.orca.queue_worker.update_slot_metadata",
        side_effect=RuntimeError("metadata store down"),
    )
    @patch("orca_auto.orca.queue_worker.start_background_process")
    def test_start_job_attach_error_releases_slot_and_terminates_process(
        self,
        mock_start_background_process: MagicMock,
        _mock_update_slot_metadata: MagicMock,
    ) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 4323
        mock_start_background_process.return_value = mock_proc
        rxn = self.root / "mol_attach_err"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        dequeue_next(self.root)

        self.assertFalse(self.worker._start_job(self.root, entry, admission_token=token or ""))

        self.assertNotIn(entry.queue_id, self.worker._running)
        mock_proc.terminate.assert_called_once()
        self.assertEqual(active_slot_count(self.root), 0)
        [updated] = list_queue(self.root)
        self.assertEqual(updated.status.value, "failed")

    def test_check_completed_jobs_success(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        rxn = self.root / "mol_done"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        dequeue_next(self.root)
        self.worker._running["q_done"] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token=token or "",
        )
        self.worker._check_completed_jobs()
        self.assertEqual(len(self.worker._running), 0)
        self.assertEqual(active_slot_count(self.root), 0)

    def test_check_completed_jobs_failure(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        rxn = self.root / "mol_fail"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        self.worker._running["q_fail"] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token="slot_fail",
        )
        self.worker._check_completed_jobs()
        self.assertEqual(len(self.worker._running), 0)

    @patch("orca_auto.orca.queue_worker._upsert_terminal_job_record")
    @patch(
        "orca_auto.orca.commands.organize.organize_reaction_dir",
        return_value={"action": "organized", "target_dir": "/tmp/out"},
    )
    def test_finalize_finished_job_auto_organizes_when_enabled(
        self,
        mock_organize: MagicMock,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        self.worker.auto_organize = True
        rxn = self.root / "mol_auto_organize"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)

        self.worker._finalize_finished_job(
            entry.queue_id,
            _RunningJob(
                queue_id=entry.queue_id,
                reaction_dir=str(rxn),
                process=MagicMock(),
                admission_token=token or "",
            ),
            rc=0,
        )

        mock_organize.assert_called_once_with(
            self.worker.cfg,
            rxn,
            notify_summary=False,
        )
        queue_entries = list_queue(self.root)
        self.assertEqual(queue_entries[0].status.value, "completed")
        mock_upsert_terminal.assert_called_once()
        self.assertEqual(active_slot_count(self.root), 0)

    @patch("orca_auto.orca.queue_worker._upsert_terminal_job_record")
    @patch("orca_auto.orca.commands.organize.organize_reaction_dir")
    def test_finalize_finished_job_skips_auto_organize_when_disabled(
        self,
        mock_organize: MagicMock,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        rxn = self.root / "mol_no_auto_organize"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)

        self.worker._finalize_finished_job(
            entry.queue_id,
            _RunningJob(
                queue_id=entry.queue_id,
                reaction_dir=str(rxn),
                process=MagicMock(),
                admission_token=token or "",
            ),
            rc=0,
        )

        mock_organize.assert_not_called()
        mock_upsert_terminal.assert_called_once()

    @patch("orca_auto.orca.queue_worker._upsert_terminal_job_record")
    @patch("orca_auto.orca.queue_worker.notify_run_finished_event", return_value=True)
    def test_finalize_finished_job_sends_parent_terminal_notification_when_unmarked(
        self,
        mock_notify: MagicMock,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        cfg = AppConfig(
            runtime=RuntimeConfig(allowed_root=str(self.root)),
            telegram=TelegramConfig(bot_token="token", chat_id="chat"),
        )
        worker = QueueWorker(cfg, str(self.root / "config.yaml"), max_concurrent=2)
        rxn = self.root / "mol_terminal_notify"
        rxn.mkdir()
        _write_completed_run_state(rxn)
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        token = reserve_slot(
            self.root,
            worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)

        worker._finalize_finished_job(
            entry.queue_id,
            _RunningJob(
                queue_id=entry.queue_id,
                reaction_dir=str(rxn),
                process=MagicMock(),
                admission_token=token or "",
            ),
            rc=0,
        )

        mock_upsert_terminal.assert_called_once()
        mock_notify.assert_called_once()
        saved = load_state(rxn)
        assert saved is not None
        final_result = saved["final_result"]
        assert final_result is not None
        self.assertIn("telegram_finished_notification_sent_at", final_result)

    @patch("orca_auto.orca.queue_worker.notify_run_finished_event", return_value=True)
    def test_terminal_notification_skips_when_state_already_marked(
        self,
        mock_notify: MagicMock,
    ) -> None:
        cfg = AppConfig(
            runtime=RuntimeConfig(allowed_root=str(self.root)),
            telegram=TelegramConfig(bot_token="token", chat_id="chat"),
        )
        rxn = self.root / "mol_terminal_already_marked"
        rxn.mkdir()
        _write_completed_run_state(rxn)
        state = load_state(rxn)
        assert state is not None
        final_result = state["final_result"]
        assert final_result is not None
        final_result["telegram_finished_notification_sent_at"] = "2026-05-29T12:02:00+00:00"
        finalize_state(rxn, state, status="completed", final_result=final_result)

        self.assertFalse(_notify_terminal_job_from_state(cfg, str(rxn)))
        mock_notify.assert_not_called()

    @patch("orca_auto.orca.queue_worker._upsert_terminal_job_record")
    @patch("orca_auto.orca.commands.organize.organize_reaction_dir")
    def test_finalize_finished_job_does_not_auto_organize_failed_run(
        self,
        mock_organize: MagicMock,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        self.worker.auto_organize = True
        rxn = self.root / "mol_failed_no_organize"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)

        self.worker._finalize_finished_job(
            entry.queue_id,
            _RunningJob(
                queue_id=entry.queue_id,
                reaction_dir=str(rxn),
                process=MagicMock(),
                admission_token=token or "",
            ),
            rc=2,
        )

        mock_organize.assert_not_called()
        queue_entries = list_queue(self.root)
        self.assertEqual(queue_entries[0].status.value, "failed")
        mock_upsert_terminal.assert_called_once()

    @patch("orca_auto.orca.queue_worker._upsert_terminal_job_record")
    @patch("orca_auto.orca.commands.organize.organize_reaction_dir")
    def test_finalize_finished_job_marks_cancelled_when_cancel_requested(
        self,
        mock_organize: MagicMock,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        self.worker.auto_organize = True
        rxn = self.root / "mol_cancel_requested_before_exit"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        cancel(self.root, entry.queue_id)
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)

        self.worker._finalize_finished_job(
            entry.queue_id,
            _RunningJob(
                queue_id=entry.queue_id,
                reaction_dir=str(rxn),
                process=MagicMock(),
                admission_token=token or "",
            ),
            rc=143,
        )

        mock_organize.assert_not_called()
        queue_entries = list_queue(self.root)
        self.assertEqual(queue_entries[0].status.value, "cancelled")
        self.assertFalse(queue_entries[0].cancel_requested)
        mock_upsert_terminal.assert_called_once()
        self.assertEqual(active_slot_count(self.root), 0)

    def test_check_completed_jobs_still_running(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        self.worker._running["q_run"] = _RunningJob(
            queue_id="q_run", reaction_dir="/tmp/r", process=mock_proc, admission_token="slot_run"
        )
        self.worker._check_completed_jobs()
        self.assertEqual(len(self.worker._running), 1)

    @patch("orca_auto.orca.queue_worker.mark_cancelled", return_value=True)
    def test_check_cancel_requests(self, mock_mark_cancelled: MagicMock) -> None:
        rxn = self.root / "mol_cancel"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        cancel(self.root, entry.queue_id)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token="slot_cancel",
        )
        with patch("orca_auto.orca.queue_worker._terminate_process"):
            self.worker._check_cancel_requests()
        self.assertNotIn(entry.queue_id, self.worker._running)
        mock_mark_cancelled.assert_called_once_with(self.root, entry.queue_id)

    def test_shutdown_all_empty(self) -> None:
        self.worker._shutdown_all()
        self.assertEqual(len(self.worker._running), 0)

    @patch("orca_auto.orca.queue_worker.requeue_running_entry", return_value=True)
    def test_shutdown_all_with_running(self, mock_requeue: MagicMock) -> None:
        rxn = self.root / "mol_shut"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token="slot_shutdown",
        )
        with patch("orca_auto.orca.queue_worker._terminate_process"):
            self.worker._shutdown_all()
        self.assertEqual(len(self.worker._running), 0)
        mock_requeue.assert_called_once_with(self.root, entry.queue_id)

    @patch("orca_auto.core.queue.worker.signal.signal")
    @patch("orca_auto.orca.queue_worker.time.sleep", side_effect=KeyboardInterrupt)
    def test_run_keyboard_interrupt(
        self,
        mock_sleep: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        rc = self.worker.run()
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(mock_signal.call_count, 2)
        # PID file should be cleaned up
        self.assertFalse(self.worker._pid_file_path().exists())

    @patch("orca_auto.core.queue.worker.signal.signal")
    @patch("orca_auto.orca.queue_worker.time.sleep")
    def test_run_shutdown_flag(
        self,
        mock_sleep: MagicMock,
        mock_signal: MagicMock,
    ) -> None:
        def set_shutdown(*a):
            self.worker._shutdown_requested = True

        mock_sleep.side_effect = set_shutdown
        rc = self.worker.run()
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(mock_signal.call_count, 2)

    def test_reconcile_orphaned_running_uses_job_report_even_with_worker_pid_file(self) -> None:
        rxn = self.root / "mol_done"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        self.worker._write_pid_file()

        report_json_path(rxn).write_text(
            json.dumps(
                orca_artifact_payload(
                    job_id="run_done_1",
                    run_id="run_done_1",
                    reaction_dir=str(rxn),
                    status="completed",
                    final_result={
                        "status": "completed",
                        "completed_at": "2026-03-10T04:59:59+00:00",
                    },
                )
            ),
            encoding="utf-8",
        )

        self.worker._reconcile_orphaned_running()

        queue_data = json.loads((self.root / "queue.json").read_text(encoding="utf-8"))
        found = next(item for item in queue_data if item["queue_id"] == entry.queue_id)
        self.assertEqual(found["status"], "completed")
        self.assertEqual(found["metadata"]["run_id"], "run_done_1")


class TestFillSlots(unittest.TestCase):
    def test_queue_worker_does_not_mutate_config_max_concurrent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            original_max_concurrent = cfg.runtime.max_concurrent

            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=2)

            self.assertEqual(cfg.runtime.max_concurrent, original_max_concurrent)
            self.assertIsNot(worker.cfg, cfg)
            self.assertIsNot(worker.cfg.runtime, cfg.runtime)
            self.assertEqual(worker.cfg.runtime.max_concurrent, 2)
            self.assertEqual(worker.max_concurrent, 2)
            self.assertEqual(worker.admission_limit, 2)

    def test_queue_worker_does_not_mutate_config_with_explicit_admission_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            cfg.runtime.admission_limit = 5
            original_max_concurrent = cfg.runtime.max_concurrent

            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=2)

            self.assertIs(worker.cfg, cfg)
            self.assertEqual(cfg.runtime.max_concurrent, original_max_concurrent)
            self.assertEqual(worker.max_concurrent, 2)
            self.assertEqual(worker.admission_limit, 5)

    def test_fill_slots_starts_pending_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=2)

            rxn = root / "mol_A"
            rxn.mkdir()
            enqueue(root, str(rxn))

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_proc = MagicMock()
                mock_proc.pid = 4101
                mock_start_background_process.return_value = mock_proc
                worker._fill_slots()
                self.assertEqual(len(worker._running), 1)

    def test_fill_slots_attaches_queue_identity_to_reserved_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=1)

            rxn = root / "mol_identity"
            rxn.mkdir()
            entry = enqueue(root, str(rxn))

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_proc = MagicMock()
                mock_proc.pid = os.getpid()
                mock_start_background_process.return_value = mock_proc
                worker._fill_slots()

            slots = list_slots(root)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].queue_id, entry.queue_id)
            self.assertEqual(slots[0].app_name, entry.app_name)
            self.assertEqual(slots[0].task_id, entry.task_id)
            self.assertEqual(slots[0].state, "active")
            self.assertEqual(slots[0].owner_pid, os.getpid())
            self.assertEqual(slots[0].work_dir, str(rxn))

    def test_fill_slots_preserves_task_id_across_slot_and_worker_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=1)

            rxn = root / "mol_task_identity"
            rxn.mkdir()
            entry = enqueue(root, str(rxn), task_id="orca_task_preserved_123")
            self.assertNotEqual(entry.queue_id, entry.task_id)

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_proc = MagicMock()
                mock_proc.pid = os.getpid()
                mock_start_background_process.return_value = mock_proc
                worker._fill_slots()

            slots = list_slots(root)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].queue_id, entry.queue_id)
            self.assertEqual(slots[0].task_id, entry.task_id)
            self.assertNotEqual(slots[0].queue_id, slots[0].task_id)
            command = mock_start_background_process.call_args.args[0]
            self.assertEqual(
                mock_start_background_process.call_args.kwargs["log_path"],
                str((root / "logs" / f"{entry.queue_id}.log").resolve()),
            )
            self.assertEqual(
                _command_arg(command, "--admission-token"),
                slots[0].token,
            )
            self.assertEqual(
                _command_arg(command, "--queue-id"),
                entry.queue_id,
            )
            self.assertNotIn("--admission-task-id", command)
            self.assertNotIn("--admission-app-name", command)

    def test_fill_slots_respects_max_concurrent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=1)

            for name in ("a", "b"):
                d = root / name
                d.mkdir()
                enqueue(root, str(d))

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_proc = MagicMock()
                mock_proc.pid = 4102
                mock_start_background_process.return_value = mock_proc
                worker._fill_slots()
                self.assertEqual(len(worker._running), 1)

    def test_fill_slots_fills_all_available_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=3)

            for name in ("p1", "p2", "p3", "p4"):
                reaction_dir = root / name
                reaction_dir.mkdir()
                enqueue(root, str(reaction_dir))

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_start_background_process.side_effect = [
                    MagicMock(pid=4103),
                    MagicMock(pid=4104),
                    MagicMock(pid=4105),
                ]
                worker._fill_slots()

            queue_by_name = {
                Path(queue_entry_reaction_dir(entry)).name: entry.status.value
                for entry in list_queue(root)
            }
            self.assertEqual(len(worker._running), 3)
            self.assertEqual(mock_start_background_process.call_count, 3)
            self.assertEqual(
                queue_by_name,
                {
                    "p1": "running",
                    "p2": "running",
                    "p3": "running",
                    "p4": "pending",
                },
            )

    def test_fill_slots_refills_immediately_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=1)

            first_dir = root / "first"
            second_dir = root / "second"
            first_dir.mkdir()
            second_dir.mkdir()

            completed_entry = enqueue(root, str(first_dir))
            pending_entry = enqueue(root, str(second_dir))
            dequeue_next(root)

            completed_proc = MagicMock()
            completed_proc.poll.return_value = 0
            completion_token = reserve_slot(
                root,
                worker.max_concurrent,
                work_dir=str(first_dir),
                queue_id=completed_entry.queue_id,
                source="queue_worker",
                state="reserved",
            )
            self.assertIsNotNone(completion_token)
            worker._running[completed_entry.queue_id] = _RunningJob(
                queue_id=completed_entry.queue_id,
                reaction_dir=str(first_dir),
                process=completed_proc,
                admission_token=completion_token or "",
            )

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_start_background_process.return_value = MagicMock(pid=4106)
                worker._check_completed_jobs()
                worker._fill_slots()

            queue_by_name = {
                Path(queue_entry_reaction_dir(entry)).name: entry.status.value
                for entry in list_queue(root)
            }
            self.assertEqual(mock_start_background_process.call_count, 1)
            self.assertEqual(len(worker._running), 1)
            self.assertIn(pending_entry.queue_id, worker._running)
            self.assertNotIn(completed_entry.queue_id, worker._running)
            self.assertEqual(
                queue_by_name,
                {
                    "first": "completed",
                    "second": "running",
                },
            )

    def test_fill_slots_respects_admission_slots_without_run_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=1)

            queued = root / "queued_only"
            queued.mkdir()
            enqueue(root, str(queued))

            token = reserve_slot(
                worker.admission_root,
                1,
                work_dir=str(root / "reserved_hold"),
                source="queue_worker",
                state="reserved",
            )
            self.assertIsNotNone(token)
            try:
                with patch(
                    "orca_auto.orca.queue_worker.start_background_process"
                ) as mock_start_background_process:
                    worker._fill_slots()
            finally:
                release_slot(worker.admission_root, token or "")

            self.assertEqual(len(worker._running), 0)
            mock_start_background_process.assert_not_called()

    def test_fill_slots_counts_existing_worker_admission_slot_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _make_cfg(tmp)
            worker = QueueWorker(cfg, str(root / "config.yaml"), max_concurrent=2)

            active_dir = root / "already_running"
            token = reserve_slot(
                root,
                worker.max_concurrent,
                work_dir=str(active_dir),
                queue_id="q_existing",
                source="queue_worker",
                state="reserved",
            )
            self.assertIsNotNone(token)
            worker._running["q_existing"] = _RunningJob(
                queue_id="q_existing",
                reaction_dir=str(active_dir),
                process=MagicMock(),
                admission_token=token or "",
            )

            queued = root / "queued_only"
            queued.mkdir()
            enqueue(root, str(queued))

            with patch(
                "orca_auto.orca.queue_worker.start_background_process"
            ) as mock_start_background_process:
                mock_proc = MagicMock()
                mock_proc.pid = 4108
                mock_start_background_process.return_value = mock_proc
                worker._fill_slots()

            self.assertEqual(len(worker._running), 2)
            mock_start_background_process.assert_called_once()


class TestQueueStoreWorkerTransitions(unittest.TestCase):
    def test_mark_cancelled_updates_running_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "mol_cancelled"
            reaction_dir.mkdir()
            entry = enqueue(root, str(reaction_dir))
            dequeue_next(root)

            updated = mark_cancelled(root, entry.queue_id)

            self.assertTrue(updated)
            queue_entries = list_queue(root)
            self.assertEqual(queue_entries[0].status.value, "cancelled")
            self.assertFalse(queue_entries[0].cancel_requested)
            self.assertIsNotNone(queue_entries[0].finished_at)

    def test_requeue_running_entry_returns_job_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reaction_dir = root / "mol_pending_again"
            reaction_dir.mkdir()
            entry = enqueue(root, str(reaction_dir))
            dequeue_next(root)

            updated = requeue_running_entry(root, entry.queue_id)

            self.assertTrue(updated)
            queue_entries = list_queue(root)
            self.assertEqual(queue_entries[0].status.value, "pending")
            self.assertEqual(queue_entries[0].started_at, "")
            self.assertFalse(queue_entries[0].cancel_requested)
