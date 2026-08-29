"""Tests for orca_auto.orca.queue.worker foreground worker job execution helpers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from orca_auto.core.admission import (
    active_slot_count,
    clear_slot_engine_process,
    get_slot,
    list_slots,
    prepare_slot_engine_process,
    release_slot,
    reserve_slot,
    set_slot_engine_process,
)
from orca_auto.core.config import DiscordConfig, MessengerConfig
from orca_auto.core.queue.lifecycle import TerminalProcessQueueMarkResult
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED
from orca_auto.orca.config import AppConfig, RetryRuntimeConfig
from orca_auto.orca.engine import read_worker_pid
from orca_auto.orca.queue import cancellation as cancellation_mod
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue import worker_tracking as worker_tracking_mod
from orca_auto.orca.queue.adapter import (
    DuplicateEntryError,
    cancel,
    dequeue_next,
    enqueue,
    list_queue,
    mark_failed,
    queue_entry_reaction_dir,
)
from orca_auto.orca.queue.worker import (
    DEFAULT_MAX_CONCURRENT,
    QueueWorker,
    _RunningJob,
    _terminate_process,
)
from orca_auto.orca.queue.worker_tracking import (
    notify_terminal_job_from_state as _notify_terminal_job_from_state,
)
from orca_auto.orca.state import (
    finalize_state,
    new_state,
    save_state,
)
from orca_auto.orca.state_reading import load_state, report_json_path
from tests.engine_artifact_helpers import orca_artifact_payload
from tests.process_helpers import patch_missing_process_group, preserved_signal_handlers
from tests.queue_worker_helpers import (
    current_orca_queue_metadata as _current_orca_queue_metadata,
)
from tests.queue_worker_helpers import make_queue_worker_cfg as _make_cfg
from tests.queue_worker_helpers import reconcile_statuses as _reconcile_statuses
from tests.queue_worker_helpers import run_terminal_replay as _run_terminal_replay
from tests.queue_worker_helpers import (
    write_completed_run_state as _write_completed_run_state,
)


def _command_arg(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


class TestTerminateProcess(unittest.TestCase):
    def setUp(self) -> None:
        self._killpg_patcher = patch_missing_process_group(
            "orca_auto.core.queue.processes.os.killpg"
        )
        self._pid_exists_patcher = patch(
            "orca_auto.core.queue.processes._pid_exists",
            return_value=False,
        )
        self._killpg_patcher.start()
        self._pid_exists_patcher.start()

    def tearDown(self) -> None:
        self._pid_exists_patcher.stop()
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

    def test_terminate_does_not_signal_reused_pid(self) -> None:
        proc = MagicMock()
        proc.pid = 1234
        proc.poll.side_effect = [None, 0]

        with patch(
            "orca_auto.core.queue.processes._pid_exists",
            return_value=True,
        ):
            assert _terminate_process(proc)

        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_escalate_to_kill(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 1234
        proc.poll.side_effect = [None] * 8
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="worker", timeout=10),
            subprocess.TimeoutExpired(cmd="worker", timeout=5),
        ]
        _terminate_process(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()


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
            self.assertFalse(worker._shutdown_requested)
            self.assertEqual(len(worker._running), 0)


class TestQueueWorkerMethods(unittest.TestCase):
    def setUp(self) -> None:
        self._ticks_patcher = patch(
            "orca_auto.core.admission.store._process_start_ticks",
            side_effect=lambda pid: max(1, int(pid)),
        )
        self._ticks_patcher.start()
        self._signal_guard = preserved_signal_handlers(signal.SIGTERM, signal.SIGINT)
        self._signal_guard.__enter__()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.cfg = _make_cfg(self._tmpdir.name)
        self.worker = QueueWorker(self.cfg, str(self.root / "config.yaml"), max_concurrent=2)

    def tearDown(self) -> None:
        self._signal_guard.__exit__(None, None, None)
        self._tmpdir.cleanup()
        self._ticks_patcher.stop()

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

    @patch("orca_auto.orca.queue.worker.start_background_process")
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

    @patch("orca_auto.orca.queue.worker_tracking.upsert_job_record")
    @patch(
        "orca_auto.orca.queue.worker_tracking.resolve_job_metadata",
        side_effect=AssertionError("should use queue metadata"),
    )
    @patch("orca_auto.orca.queue.worker.start_background_process")
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
        "orca_auto.orca.queue.worker.start_background_process",
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
        "orca_auto.orca.queue.replay.update_slot_metadata",
        side_effect=RuntimeError("metadata store down"),
    )
    @patch("orca_auto.orca.queue.worker.start_background_process")
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

    def test_completed_job_retries_when_engine_recovery_raises(self) -> None:
        rxn = self.root / "mol_recovery_retry"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-recovery")
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
        process = MagicMock()
        process.poll.return_value = 1
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )

        with (
            patch.object(
                replay_mod,
                "recover_slot_engine_process",
                side_effect=[RuntimeError("engine recovery failed"), True],
            ) as recover,
            patch.object(
                replay_mod,
                "mark_terminal_process_queue_entry_with_result",
                wraps=replay_mod.mark_terminal_process_queue_entry_with_result,
            ) as mark_terminal,
            patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
            patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
        ):
            self.worker._check_completed_jobs()
            self.assertIn(entry.queue_id, self.worker._running)
            self.assertEqual(active_slot_count(self.root), 1)
            [still_running] = list_queue(self.root)
            self.assertEqual(still_running.status, QueueStatus.RUNNING)
            mark_terminal.assert_not_called()

            self.worker._check_completed_jobs()

        self.assertNotIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 0)
        [failed] = list_queue(self.root)
        self.assertEqual(failed.status, QueueStatus.FAILED)
        self.assertEqual(recover.call_count, 2)
        mark_terminal.assert_called_once()

    def test_failed_state_write_leaves_durable_replay_for_worker_restart(self) -> None:
        rxn = self.root / "mol_durable_restart"
        rxn.mkdir()
        old_state = new_state(rxn, rxn / "task-a.inp", max_retries=0)
        old_state["job_id"] = "task-a"
        finalize_state(
            rxn,
            old_state,
            status=STATUS_COMPLETED,
            final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
        )
        entry = enqueue(self.root, str(rxn), force=True, task_id="task-b")
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
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=MagicMock(),
            admission_token=token or "",
            task_id=entry.task_id,
        )

        with (
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(
                replay_mod,
                "record_failed_run_state",
                side_effect=OSError("state write failed"),
            ),
            patch.object(self.worker, "_release_admission_slot") as release,
        ):
            with self.assertRaisesRegex(OSError, "state write failed"):
                self.worker._finalize_finished_job(entry.queue_id, job, rc=1)

        release.assert_not_called()
        self.assertEqual(active_slot_count(self.root), 1)
        [terminal] = list_queue(self.root)
        self.assertEqual(terminal.status, QueueStatus.FAILED)
        marker = terminal.metadata.get("orca_terminal_replay")
        assert isinstance(marker, dict)
        self.assertEqual(marker["task_id"], "task-b")
        self.assertEqual(marker["observed_state"]["job_id"], "task-a")

        restarted = QueueWorker(
            self.cfg,
            str(self.root / "config.yaml"),
            max_concurrent=2,
        )
        with (
            patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
            patch.object(
                worker_tracking_mod,
                "notify_terminal_job_from_state",
                return_value=False,
            ),
        ):
            _run_terminal_replay(restarted, self.root, terminal)

        written = load_state(rxn)
        assert written is not None
        self.assertEqual(written["job_id"], "task-b")
        self.assertEqual(written["status"], STATUS_FAILED)
        [replayed] = list_queue(self.root)
        self.assertIsNone(replayed.metadata.get("orca_terminal_replay"))

    def test_terminal_side_effect_failure_blocks_forced_successor_until_replay(self) -> None:
        rxn = self.root / "mol_terminal_replay_barrier"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-a")
        dequeue_next(self.root)
        token = reserve_slot(
            self.root,
            2,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        process = MagicMock()
        process.poll.return_value = 1
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )
        self.worker.max_concurrent = 2
        self.worker._running[entry.queue_id] = job

        with (
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(
                worker_tracking_mod,
                "upsert_terminal_job_record",
                side_effect=[False, True],
            ) as upsert,
            patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
        ):
            self.worker._check_completed_jobs()

            self.assertIn(entry.queue_id, self.worker._running)
            self.assertEqual(active_slot_count(self.root), 1)
            [pending_replay] = list_queue(self.root)
            self.assertIsInstance(
                pending_replay.metadata.get("orca_terminal_replay"),
                dict,
            )
            # Capacity remains at max_concurrent=2, so this is the explicit replay
            # admission barrier rather than ordinary slot exhaustion.
            self.assertEqual(self.worker._fill_slots(), "blocked")
            with self.assertRaises(DuplicateEntryError):
                enqueue(self.root, str(rxn), force=True, task_id="task-b")

            self.worker._check_completed_jobs()

        self.assertEqual(upsert.call_count, 2)
        self.assertNotIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 0)
        [replayed] = list_queue(self.root)
        self.assertIsNone(replayed.metadata.get("orca_terminal_replay"))
        successor = enqueue(self.root, str(rxn), force=True, task_id="task-b")
        self.assertEqual(successor.status, QueueStatus.PENDING)

    def test_finalize_clears_active_engine_record_before_mark_and_release(self) -> None:
        rxn = self.root / "mol_active_engine_finalize"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-active-engine")
        dequeue_next(self.root)
        token = reserve_slot(
            self.root,
            self.worker.max_concurrent,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
            engine_process_state="idle",
        )
        self.assertIsNotNone(token)
        prepare_slot_engine_process(self.root, token or "")
        set_slot_engine_process(
            self.root,
            token or "",
            pid=424242,
            pgid=424242,
            process_start_ticks=10101,
        )
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=MagicMock(),
            admission_token=token or "",
            task_id=entry.task_id,
        )
        events: list[str] = []

        def recover(root: Path, current_token: str) -> bool:
            active = get_slot(root, current_token)
            assert active is not None
            assert active.engine_process_state == "active"
            clear_slot_engine_process(
                root,
                current_token,
                expected_pid=active.engine_pid,
                expected_process_start_ticks=active.engine_process_start_ticks,
                expected_process_boot_id=active.engine_process_boot_id,
                next_state="idle",
            )
            events.append("recover")
            return True

        real_mark = replay_mod.mark_terminal_process_queue_entry_with_result

        def mark(*args: Any, **kwargs: Any) -> TerminalProcessQueueMarkResult:
            current = get_slot(self.root, token or "")
            assert current is not None
            assert current.engine_process_state == "idle"
            events.append("mark")
            return real_mark(*args, **kwargs)

        def release(current_token: str) -> None:
            current = get_slot(self.root, current_token)
            assert current is not None
            assert current.engine_process_state == "idle"
            events.append("release")
            release_slot(self.root, current_token)

        with (
            patch.object(replay_mod, "recover_slot_engine_process", side_effect=recover),
            patch.object(
                replay_mod,
                "mark_terminal_process_queue_entry_with_result",
                side_effect=mark,
            ),
            patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
            patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
            patch.object(
                self.worker,
                "_release_admission_slot",
                side_effect=release,
            ),
        ):
            self.worker._finalize_finished_job(entry.queue_id, job, rc=0)

        self.assertEqual(events, ["recover", "mark", "release"])
        self.assertEqual(active_slot_count(self.root), 0)
        [completed] = list_queue(self.root)
        self.assertEqual(completed.status, QueueStatus.COMPLETED)

    def test_finalize_does_not_publish_without_persisted_marker_after_mark(self) -> None:
        rxn = self.root / "mol_mark_snapshot"
        rxn.mkdir()
        selected_inp = rxn / "task-b.inp"
        snapshot = QueueEntry(
            queue_id="queue-snapshot",
            app_name="orca_auto_orca",
            task_id="task-b",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.RUNNING,
            metadata={
                "reaction_dir": str(rxn),
                "selected_inp": str(selected_inp),
            },
        )
        result = TerminalProcessQueueMarkResult(
            marked=True,
            status=STATUS_FAILED,
            expected_job_id="task-b",
            current_entry=snapshot,
            queue_root=self.root,
            run_id=None,
        )
        job = _RunningJob(
            queue_id=snapshot.queue_id,
            reaction_dir=str(rxn),
            process=MagicMock(),
            admission_token="slot-snapshot",
            task_id="task-stale",
        )
        events: list[str] = []

        def mark(*_args: object, **_kwargs: object) -> TerminalProcessQueueMarkResult:
            events.append("mark")
            return result

        with (
            patch.object(
                replay_mod,
                "recover_slot_engine_process",
                side_effect=lambda *_args: events.append("recover"),
            ),
            patch.object(
                replay_mod,
                "mark_terminal_process_queue_entry_with_result",
                side_effect=mark,
            ),
            patch.object(
                replay_mod,
                "record_failed_run_state",
            ) as record_failed,
            patch.object(
                replay_mod,
                "update_terminal",
            ) as update,
            patch.object(
                replay_mod,
                "_run_terminal_replay_side_effects",
                side_effect=lambda *_args, **_kwargs: events.append("side-effects"),
            ) as side_effects,
            patch.object(
                replay_mod,
                "_clear_terminal_replay_marker_or_confirm_absent",
                side_effect=lambda *_args: events.append("clear"),
            ),
            patch.object(
                replay_mod,
                "queue_entry_by_id",
                return_value=None,
            ),
            patch.object(
                self.worker,
                "_release_admission_slot",
                side_effect=lambda _token: events.append("release"),
            ),
        ):
            self.worker._finalize_finished_job(snapshot.queue_id, job, rc=1)

        record_failed.assert_not_called()
        update.assert_not_called()
        side_effects.assert_not_called()
        self.assertEqual(events, ["recover", "mark", "release"])

    def test_stale_finalizer_does_not_resurrect_cleared_terminal_marker(self) -> None:
        rxn = self.root / "mol_stale_finalizer"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-stale-finalizer")
        running = dequeue_next(self.root)
        assert running is not None
        self.assertTrue(
            mark_failed(
                self.root,
                entry.queue_id,
                error="first_owner",
                expected_entry=running,
            )
        )
        self.assertTrue(
            replay_mod.update_queue_metadata(
                self.root,
                entry.queue_id,
                {"orca_terminal_replay": None},
            )
        )
        [closed] = list_queue(self.root)
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=MagicMock(),
            admission_token="slot-stale-finalizer",
            task_id=entry.task_id,
        )

        with (
            patch.object(replay_mod, "recover_slot_engine_process"),
            patch.object(replay_mod, "record_failed_run_state") as record_failed,
            patch.object(self.worker, "_release_admission_slot") as release,
        ):
            self.worker._finalize_finished_job(entry.queue_id, job, rc=1)

        record_failed.assert_not_called()
        release.assert_called_once_with(job.admission_token)
        self.assertEqual(list_queue(self.root), [closed])
        self.assertIsNone(closed.metadata.get("orca_terminal_replay"))

    def test_finalize_child_exit_recovers_once_and_releases_on_benign_mark_noop(
        self,
    ) -> None:
        result = TerminalProcessQueueMarkResult(
            marked=False,
            status=None,
            expected_job_id="task-moved",
            current_entry=None,
            queue_root=self.root,
        )
        job = _RunningJob(
            queue_id="queue-moved",
            reaction_dir=str(self.root / "moved"),
            process=MagicMock(),
            admission_token="slot-moved",
            task_id="task-moved",
        )
        with (
            patch.object(replay_mod, "recover_slot_engine_process") as recover,
            patch.object(
                replay_mod,
                "mark_terminal_process_queue_entry_with_result",
                return_value=result,
            ),
            patch.object(
                replay_mod,
                "_run_terminal_replay_side_effects",
            ) as side_effects,
            patch.object(self.worker, "_release_admission_slot") as release,
        ):
            replay_mod.finalize_child_exit(self.worker, job, rc=1)

        recover.assert_called_once_with(self.worker.admission_root, job.admission_token)
        side_effects.assert_not_called()
        release.assert_called_once_with(job.admission_token)

    @patch("orca_auto.orca.queue.worker_tracking.upsert_terminal_job_record")
    def test_finalize_finished_job_marks_completed_and_releases_slot(
        self,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        rxn = self.root / "mol_completed"
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
                task_id=entry.task_id,
            ),
            rc=0,
        )

        queue_entries = list_queue(self.root)
        self.assertEqual(queue_entries[0].status.value, "completed")
        mock_upsert_terminal.assert_called_once()
        self.assertEqual(active_slot_count(self.root), 0)

    @patch("orca_auto.orca.queue.worker_tracking.upsert_terminal_job_record")
    @patch("orca_auto.orca.queue.worker_tracking.notify_run_finished_event", return_value=True)
    def test_finalize_finished_job_sends_parent_terminal_notification_when_unmarked(
        self,
        mock_notify: MagicMock,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        cfg = AppConfig(
            runtime=RetryRuntimeConfig(allowed_root=str(self.root)),
            messenger=MessengerConfig(
                discord=DiscordConfig(bot_token="token", default_channel_id="123")
            ),
        )
        worker = QueueWorker(cfg, str(self.root / "config.yaml"), max_concurrent=2)
        rxn = self.root / "mol_terminal_notify"
        rxn.mkdir()
        _write_completed_run_state(rxn)
        entry = enqueue(self.root, str(rxn), task_id="task_terminal_123")
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
                task_id=entry.task_id,
            ),
            rc=0,
        )

        mock_upsert_terminal.assert_called_once()
        mock_notify.assert_called_once()
        saved = load_state(rxn)
        assert saved is not None
        final_result = saved["final_result"]
        assert final_result is not None
        self.assertIn("finished_notification_sent_at", final_result)

    @patch("orca_auto.orca.queue.worker_tracking.notify_run_finished_event", return_value=True)
    def test_terminal_notification_skips_when_state_already_marked(
        self,
        mock_notify: MagicMock,
    ) -> None:
        cfg = AppConfig(
            runtime=RetryRuntimeConfig(allowed_root=str(self.root)),
            messenger=MessengerConfig(
                discord=DiscordConfig(bot_token="token", default_channel_id="123")
            ),
        )
        rxn = self.root / "mol_terminal_already_marked"
        rxn.mkdir()
        _write_completed_run_state(rxn)
        state = load_state(rxn)
        assert state is not None
        final_result = state["final_result"]
        assert final_result is not None
        final_result["finished_notification_sent_at"] = "2026-05-29T12:02:00+00:00"
        finalize_state(rxn, state, status="completed", final_result=final_result)

        self.assertFalse(_notify_terminal_job_from_state(cfg, str(rxn)))
        mock_notify.assert_not_called()

    @patch("orca_auto.orca.queue.worker_tracking.notify_run_finished_event", return_value=True)
    def test_terminal_notification_rejects_previous_generation_state(
        self,
        mock_notify: MagicMock,
    ) -> None:
        cfg = AppConfig(
            runtime=RetryRuntimeConfig(allowed_root=str(self.root)),
            messenger=MessengerConfig(
                discord=DiscordConfig(bot_token="token", default_channel_id="123")
            ),
        )
        rxn = self.root / "mol_terminal_stale_generation"
        rxn.mkdir()
        _write_completed_run_state(rxn)

        self.assertFalse(
            _notify_terminal_job_from_state(
                cfg,
                str(rxn),
                expected_job_id="task-b",
            )
        )
        mock_notify.assert_not_called()

    @patch("orca_auto.orca.queue.worker_tracking.upsert_terminal_job_record")
    def test_finalize_finished_job_marks_failed_run(
        self,
        mock_upsert_terminal: MagicMock,
    ) -> None:
        rxn = self.root / "mol_failed"
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

        queue_entries = list_queue(self.root)
        self.assertEqual(queue_entries[0].status.value, "failed")
        mock_upsert_terminal.assert_called_once()

    def test_finalize_finished_job_synthesizes_current_generation_failure_state(
        self,
    ) -> None:
        rxn = self.root / "mol_failed_before_current_state"
        rxn.mkdir()
        previous = new_state(rxn, rxn / "task-a.inp", max_retries=0)
        previous["job_id"] = "task-a"
        previous_run_id = previous["run_id"]
        finalize_state(
            rxn,
            previous,
            status=STATUS_COMPLETED,
            final_result={
                "status": STATUS_COMPLETED,
                "reason": "normal_termination",
                "completed_at": "2026-07-10T00:00:00+00:00",
            },
        )
        entry = enqueue(
            self.root,
            str(rxn),
            force=True,
            task_id="task-b",
        )
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
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=MagicMock(),
            admission_token=token or "",
            task_id=entry.task_id,
        )
        self.worker.cfg.messenger = MessengerConfig(
            discord=DiscordConfig(bot_token="token", default_channel_id="123")
        )

        with patch.object(
            worker_tracking_mod,
            "notify_run_finished_event",
            return_value=True,
        ) as notify:
            self.worker._finalize_finished_job(entry.queue_id, job, rc=1)

        terminal = {item.queue_id: item for item in list_queue(self.root)}[entry.queue_id]
        self.assertEqual(terminal.status, QueueStatus.FAILED)
        self.assertTrue(terminal.metadata.get("run_id"))
        self.assertNotEqual(terminal.metadata.get("run_id"), previous_run_id)
        written = load_state(rxn)
        assert written is not None
        self.assertEqual(written["job_id"], "task-b")
        self.assertEqual(written["run_id"], terminal.metadata["run_id"])
        self.assertEqual(written["status"], "failed")
        final_result = written["final_result"]
        assert final_result is not None
        self.assertEqual(final_result["status"], "failed")
        self.assertEqual(final_result["reason"], "exit_code=1")
        records = json.loads((self.root / "job_locations.json").read_text(encoding="utf-8"))
        current_record = next(record for record in records if record["job_id"] == "task-b")
        self.assertEqual(current_record["status"], "failed")
        self.assertNotEqual(current_record["job_id"], "task-a")
        notify.assert_called_once()

        _run_terminal_replay(self.worker, self.root, terminal)
        _run_terminal_replay(self.worker, self.root, terminal)

        notify.assert_called_once()
        key = (str(self.root.resolve()), entry.queue_id)
        reconcile_statuses = _reconcile_statuses(self.worker)
        self.assertEqual(reconcile_statuses[key], "failed")

    @patch("orca_auto.orca.queue.worker_tracking.upsert_terminal_job_record")
    def test_finalize_finished_job_marks_cancelled_when_cancel_requested(
        self,
        mock_upsert_terminal: MagicMock,
    ) -> None:
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

        queue_entries = list_queue(self.root)
        self.assertEqual(queue_entries[0].status.value, "cancelled")
        self.assertFalse(queue_entries[0].cancel_requested)
        self.assertTrue(queue_entries[0].metadata.get("run_id"))
        written = load_state(rxn)
        assert written is not None
        self.assertEqual(written["job_id"], entry.task_id)
        self.assertEqual(written["run_id"], queue_entries[0].metadata["run_id"])
        self.assertEqual(written["status"], STATUS_CANCELLED)
        final_result = written["final_result"]
        assert final_result is not None
        self.assertEqual(final_result["status"], STATUS_CANCELLED)
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

    @patch(
        "orca_auto.orca.queue.replay.mark_cancelled",
        wraps=replay_mod.mark_cancelled,
    )
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

        def terminate(process: MagicMock) -> bool:
            process.poll.return_value = 0
            return True

        with patch("orca_auto.orca.queue.replay.terminate_process", side_effect=terminate):
            self.worker._check_cancel_requests()
        self.assertNotIn(entry.queue_id, self.worker._running)
        mock_mark_cancelled.assert_called_once()
        self.assertEqual(mock_mark_cancelled.call_args.args, (self.root, entry.queue_id))
        self.assertNotIn("metadata_update", mock_mark_cancelled.call_args.kwargs)
        [cancelled] = list_queue(self.root)
        self.assertEqual(cancelled.status, QueueStatus.CANCELLED)
        self.assertIsNone(cancelled.metadata.get("orca_terminal_replay"))

    @patch("orca_auto.orca.queue.replay.mark_cancelled", return_value=True)
    def test_check_cancel_requests_retains_live_job_when_termination_fails(
        self,
        mock_mark_cancelled: MagicMock,
    ) -> None:
        rxn = self.root / "mol_cancel_live"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        cancel(self.root, entry.queue_id)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token="slot_cancel_live",
        )

        with patch("orca_auto.orca.queue.replay.terminate_process", return_value=False):
            self.worker._check_cancel_requests()

        self.assertIn(entry.queue_id, self.worker._running)
        mock_mark_cancelled.assert_not_called()

    @patch("orca_auto.orca.queue.replay.mark_cancelled", return_value=True)
    def test_check_cancel_requests_ignores_replacement_generation(
        self,
        mock_mark_cancelled: MagicMock,
    ) -> None:
        rxn = self.root / "mol_cancel_replacement"
        rxn.mkdir()
        selected = enqueue(self.root, str(rxn), task_id="task-a")
        running = dequeue_next(self.root)
        assert running is not None
        replacement = replace(
            running,
            task_id="task-b",
            cancel_requested=True,
        )
        save_entries_core(self.root, [replacement])
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        self.worker._running[selected.queue_id] = _RunningJob(
            queue_id=selected.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token="slot-a",
            task_id="task-a",
        )

        with patch("orca_auto.orca.queue.replay.terminate_process") as terminate:
            self.worker._check_cancel_requests()

        terminate.assert_not_called()
        mock_mark_cancelled.assert_not_called()
        self.assertIn(selected.queue_id, self.worker._running)
        [durable] = list_queue(self.root)
        self.assertEqual(durable.task_id, "task-b")
        self.assertTrue(durable.cancel_requested)

    def test_start_error_does_not_fail_replacement_generation(self) -> None:
        rxn = self.root / "mol_start_error_replacement"
        rxn.mkdir()
        selected = enqueue(self.root, str(rxn), task_id="task-a")
        running = dequeue_next(self.root)
        assert running is not None
        token = reserve_slot(
            self.root,
            2,
            work_dir=str(rxn),
            queue_id=selected.queue_id,
            source="queue_worker",
            state="reserved",
        )
        assert token is not None
        replacement = replace(running, task_id="task-b")
        save_entries_core(self.root, [replacement])

        self.worker._mark_entry_failed_and_release(
            self.root,
            running,
            token,
            error="worker start failed",
            mark_failed_fn=queue_worker_mod.mark_failed,
        )

        [durable] = list_queue(self.root)
        self.assertEqual(durable.task_id, "task-b")
        self.assertEqual(durable.status, QueueStatus.RUNNING)
        self.assertEqual(active_slot_count(self.root), 0)

    def test_cancel_finalizes_state_before_deferred_slot_release(self) -> None:
        rxn = self.root / "mol_cancel_order"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-order")
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
        process = MagicMock()
        process.poll.return_value = None
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )
        events: list[str] = []

        def terminate(current: MagicMock) -> bool:
            events.append("terminate")
            current.poll.return_value = 0
            return True

        def finalize(*_args: object, **_kwargs: object) -> bool:
            events.append("finalize")
            [cancelled_entry] = list_queue(self.root)
            self.assertEqual(cancelled_entry.status, QueueStatus.CANCELLED)
            self.assertEqual(active_slot_count(self.root), 1)
            return True

        def release(token_to_release: str) -> None:
            events.append("release")
            release_slot(self.root, token_to_release)

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(
                replay_mod,
                "recover_slot_engine_process",
                side_effect=lambda *_args: events.append("recover"),
            ),
            patch.object(
                cancellation_mod,
                "cancel_running_process_job",
                wraps=cancellation_mod.cancel_running_process_job,
            ) as cancel_core,
            patch.object(
                replay_mod,
                "_run_terminal_replay_side_effects",
                side_effect=finalize,
            ) as finalize_cancelled,
            patch.object(
                self.worker,
                "_release_admission_slot",
                side_effect=release,
            ),
        ):
            self.assertTrue(
                cancellation_mod.cancel_running_job(
                    self.worker,
                    entry.queue_id,
                    job,
                )
            )

        self.assertFalse(cancel_core.call_args.kwargs["release_admission_slot"])
        self.assertEqual(events, ["terminate", "recover", "finalize", "release"])
        self.assertEqual(active_slot_count(self.root), 0)
        replay_item = finalize_cancelled.call_args.args[1]
        self.assertEqual(replay_item.queue_id, entry.queue_id)
        self.assertEqual(replay_item.task_id, entry.task_id)
        self.assertTrue(replay_item.state_prepared)

    def test_cancel_mark_failure_retains_queue_slot_and_skips_finalization(self) -> None:
        rxn = self.root / "mol_cancel_mark_failure"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-mark-failure")
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
        process = MagicMock()
        process.poll.return_value = None
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )

        def terminate(current: MagicMock) -> bool:
            current.poll.return_value = 0
            return True

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(replay_mod, "mark_cancelled", return_value=False),
            patch.object(self.worker, "_release_admission_slot") as release,
        ):
            self.assertFalse(
                cancellation_mod.cancel_running_job(
                    self.worker,
                    entry.queue_id,
                    job,
                )
            )

        release.assert_not_called()
        self.assertEqual(active_slot_count(self.root), 1)
        [still_running] = list_queue(self.root)
        self.assertEqual(still_running.status, QueueStatus.RUNNING)

    def test_cancel_mark_false_completion_retry_keeps_running_entry_slot(self) -> None:
        rxn = self.root / "mol_cancel_mark_false_retry"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-mark-false-retry")
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
        process = MagicMock()
        process.poll.return_value = None
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )

        def terminate(current: MagicMock) -> bool:
            current.poll.return_value = 0
            return True

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(replay_mod, "mark_cancelled", return_value=False),
        ):
            self.worker._check_cancel_requests()
            self.assertIn(entry.queue_id, self.worker._running)
            self.assertEqual(active_slot_count(self.root), 1)

            self.worker._check_completed_jobs()

        self.assertIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 1)
        [still_running] = list_queue(self.root)
        self.assertEqual(still_running.status, QueueStatus.RUNNING)
        self.assertTrue(still_running.cancel_requested)

    def test_cancel_mark_false_releases_after_concurrent_terminal_transition(self) -> None:
        rxn = self.root / "mol_cancel_mark_false_terminal_race"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-terminal-race")
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
        process = MagicMock()
        process.poll.return_value = 0
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )
        real_mark_cancelled = replay_mod.mark_cancelled

        def terminalize_then_report_false(*args: Any, **kwargs: Any) -> bool:
            self.assertTrue(real_mark_cancelled(*args, **kwargs))
            return False

        with (
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(
                replay_mod,
                "mark_cancelled",
                side_effect=terminalize_then_report_false,
            ),
            patch.object(
                replay_mod,
                "record_cancelled_run_state",
                wraps=replay_mod.record_cancelled_run_state,
            ) as record_state,
            patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
            patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
        ):
            self.worker._check_completed_jobs()

        record_state.assert_called_once()
        self.assertNotIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 0)
        [cancelled_entry] = list_queue(self.root)
        self.assertEqual(cancelled_entry.status, QueueStatus.CANCELLED)

    def test_cancel_mark_exception_isolated_and_retried_by_completion(self) -> None:
        rxn = self.root / "mol_cancel_mark_exception_retry"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-mark-exception-retry")
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
        process = MagicMock()
        process.poll.return_value = None
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )
        real_mark_cancelled = replay_mod.mark_cancelled
        mark_attempts = 0

        def terminate(current: MagicMock) -> bool:
            current.poll.return_value = 0
            return True

        def flaky_mark_cancelled(*args: Any, **kwargs: Any) -> bool:
            nonlocal mark_attempts
            mark_attempts += 1
            if mark_attempts == 1:
                raise OSError("queue write failed")
            return real_mark_cancelled(*args, **kwargs)

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(replay_mod, "mark_cancelled", side_effect=flaky_mark_cancelled),
            patch.object(worker_tracking_mod, "upsert_terminal_job_record", return_value=True),
            patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
        ):
            self.worker._check_cancel_requests()
            self.assertIn(entry.queue_id, self.worker._running)
            self.assertEqual(active_slot_count(self.root), 1)
            [still_running] = list_queue(self.root)
            self.assertEqual(still_running.status, QueueStatus.RUNNING)

            self.worker._check_completed_jobs()

        self.assertEqual(mark_attempts, 2)
        self.assertNotIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 0)
        [cancelled_entry] = list_queue(self.root)
        self.assertEqual(cancelled_entry.status, QueueStatus.CANCELLED)

    def test_cancel_state_failure_retains_slot_after_terminal_mark(self) -> None:
        rxn = self.root / "mol_cancel_state_failure"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-state-failure")
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
        process = MagicMock()
        process.poll.return_value = None
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )
        self.worker._running[entry.queue_id] = job
        real_record_cancelled = replay_mod.record_cancelled_run_state
        record_attempts = 0
        released: list[str] = []

        def terminate(current: MagicMock) -> bool:
            current.poll.return_value = 0
            return True

        def record_cancelled(*args: Any, **kwargs: Any) -> tuple[str | None, str | None]:
            nonlocal record_attempts
            record_attempts += 1
            if record_attempts == 1:
                raise OSError("state store unavailable")
            return real_record_cancelled(*args, **kwargs)

        def release(current_token: str) -> None:
            released.append(current_token)
            release_slot(self.root, current_token)

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(
                replay_mod,
                "record_cancelled_run_state",
                side_effect=record_cancelled,
            ),
            patch.object(
                worker_tracking_mod,
                "upsert_terminal_job_record",
                return_value=True,
            ) as upsert,
            patch.object(
                worker_tracking_mod,
                "notify_terminal_job_from_state",
                return_value=False,
            ) as notify,
            patch.object(
                self.worker,
                "_release_admission_slot",
                side_effect=release,
            ),
        ):
            self.assertFalse(
                cancellation_mod.cancel_running_job(
                    self.worker,
                    entry.queue_id,
                    job,
                )
            )
            self.assertEqual(released, [])
            upsert.assert_not_called()
            notify.assert_not_called()
            self.assertEqual(active_slot_count(self.root), 1)
            [cancelled_entry] = list_queue(self.root)
            self.assertEqual(cancelled_entry.status, QueueStatus.CANCELLED)
            self.assertIn("_orca_terminal_replay_item", job.__dict__)

            self.worker._check_completed_jobs()

        self.assertEqual(record_attempts, 2)
        self.assertEqual(released, [token])
        self.assertEqual(active_slot_count(self.root), 0)
        self.assertNotIn(entry.queue_id, self.worker._running)
        written = load_state(rxn)
        assert written is not None
        self.assertEqual(written["job_id"], entry.task_id)
        self.assertEqual(written["status"], STATUS_CANCELLED)
        upsert.assert_called_once()
        notify.assert_called_once()

    def test_cancel_side_effect_failure_blocks_admission_until_strict_replay(self) -> None:
        rxn = self.root / "mol_cancel_replay_barrier"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-a")
        dequeue_next(self.root)
        cancel(self.root, entry.queue_id)
        token = reserve_slot(
            self.root,
            2,
            work_dir=str(rxn),
            queue_id=entry.queue_id,
            source="queue_worker",
            state="reserved",
        )
        self.assertIsNotNone(token)
        process = MagicMock()
        process.poll.return_value = None
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )
        self.worker.max_concurrent = 2
        self.worker._running[entry.queue_id] = job

        def terminate(current: MagicMock) -> bool:
            current.poll.return_value = 0
            return True

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(replay_mod, "recover_slot_engine_process", return_value=True),
            patch.object(
                worker_tracking_mod,
                "upsert_terminal_job_record",
                side_effect=[False, True],
            ) as upsert,
            patch.object(worker_tracking_mod, "notify_terminal_job_from_state", return_value=False),
        ):
            self.worker._check_cancel_requests()

            self.assertIn(entry.queue_id, self.worker._running)
            self.assertEqual(active_slot_count(self.root), 1)
            [pending_replay] = list_queue(self.root)
            self.assertEqual(pending_replay.status, QueueStatus.CANCELLED)
            self.assertIsInstance(
                pending_replay.metadata.get("orca_terminal_replay"),
                dict,
            )
            self.assertEqual(self.worker._fill_slots(), "blocked")

            self.worker._check_completed_jobs()

        self.assertEqual(upsert.call_count, 2)
        self.assertNotIn(entry.queue_id, self.worker._running)
        self.assertEqual(active_slot_count(self.root), 0)
        [replayed] = list_queue(self.root)
        self.assertIsNone(replayed.metadata.get("orca_terminal_replay"))

    def test_cancel_recovery_failure_retains_queue_slot_and_skips_mark(self) -> None:
        rxn = self.root / "mol_cancel_recovery_failure"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn), task_id="task-cancel-recovery-failure")
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
        process = MagicMock()
        process.poll.return_value = None
        job = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=process,
            admission_token=token or "",
            task_id=entry.task_id,
        )

        def terminate(current: MagicMock) -> bool:
            current.poll.return_value = 0
            return True

        with (
            patch.object(replay_mod, "terminate_process", side_effect=terminate),
            patch.object(
                replay_mod,
                "recover_slot_engine_process",
                side_effect=RuntimeError("engine recovery failed"),
            ),
            patch.object(replay_mod, "mark_cancelled") as mark_cancelled_entry,
            patch.object(self.worker, "_release_admission_slot") as release,
        ):
            self.assertFalse(
                cancellation_mod.cancel_running_job(
                    self.worker,
                    entry.queue_id,
                    job,
                )
            )

        mark_cancelled_entry.assert_not_called()
        release.assert_not_called()
        self.assertEqual(active_slot_count(self.root), 1)
        [still_running] = list_queue(self.root)
        self.assertEqual(still_running.status, QueueStatus.RUNNING)

    def test_shutdown_all_empty(self) -> None:
        self.worker._shutdown_all()
        self.assertEqual(len(self.worker._running), 0)

    @patch("orca_auto.orca.queue.replay.requeue_running_entry", return_value=True)
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
        with patch("orca_auto.orca.queue.replay.terminate_process", return_value=True):
            self.worker._shutdown_all()
        self.assertEqual(len(self.worker._running), 0)
        mock_requeue.assert_called_once()
        self.assertEqual(mock_requeue.call_args.args, (self.root, entry.queue_id))
        self.assertEqual(mock_requeue.call_args.kwargs["expected_entry"].task_id, entry.task_id)

    def test_shutdown_finalizes_cancel_requested_job(self) -> None:
        # A cancel pending when the worker shuts down must be finalized (terminal +
        # run state written), not requeued for resume: the shutdown path routes it
        # through the same cancel finalization as the proactive loop.
        rxn = self.root / "mol_shut_cancel"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        cancel(self.root, entry.queue_id)

        state = new_state(rxn, rxn / "job.inp", max_retries=3)
        state["job_id"] = entry.task_id
        state["status"] = "running"
        save_state(rxn, state)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        self.worker._running[entry.queue_id] = _RunningJob(
            queue_id=entry.queue_id,
            reaction_dir=str(rxn),
            process=mock_proc,
            admission_token="slot_shut_cancel",
        )

        def terminate_process(process: MagicMock) -> bool:
            process.poll.return_value = 0
            return True

        with (
            patch(
                "orca_auto.orca.queue.replay.terminate_process",
                side_effect=terminate_process,
            ),
            patch("orca_auto.orca.queue.replay.requeue_running_entry") as mock_requeue,
        ):
            self.worker._shutdown_all()

        mock_requeue.assert_not_called()
        self.assertNotIn(entry.queue_id, self.worker._running)
        queue_entries = {e.queue_id: e for e in list_queue(self.root)}
        self.assertEqual(queue_entries[entry.queue_id].status.value, "cancelled")
        written = load_state(rxn)
        assert written is not None
        assert written["final_result"] is not None
        self.assertEqual(written["final_result"]["status"], "cancelled")

    @patch("orca_auto.core.queue.worker.signal.signal")
    @patch("orca_auto.orca.queue.worker.time.sleep", side_effect=KeyboardInterrupt)
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
    @patch("orca_auto.orca.queue.worker.time.sleep")
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

    def test_reconcile_orphaned_running_ignores_root_report_even_with_worker_pid_file(
        self,
    ) -> None:
        rxn = self.root / "mol_done"
        rxn.mkdir()
        entry = enqueue(self.root, str(rxn))
        dequeue_next(self.root)
        self.worker._write_pid_file()

        report_json_path(rxn).write_text(
            json.dumps(
                orca_artifact_payload(
                    job_id=entry.task_id,
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
        self.assertEqual(found["status"], "pending")
        self.assertNotIn("run_id", found["metadata"])


class TestFillSlots(unittest.TestCase):
    def setUp(self) -> None:
        self._ticks_patcher = patch(
            "orca_auto.core.admission.store._process_start_ticks",
            side_effect=lambda pid: max(1, int(pid)),
        )
        self._ticks_patcher.start()

    def tearDown(self) -> None:
        self._ticks_patcher.stop()

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
            enqueue(root, str(rxn), metadata=_current_orca_queue_metadata(rxn))

            with patch(
                "orca_auto.orca.queue.worker.start_background_process"
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
            entry = enqueue(
                root,
                str(rxn),
                metadata=_current_orca_queue_metadata(rxn),
            )

            with patch(
                "orca_auto.orca.queue.worker.start_background_process"
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
            entry = enqueue(
                root,
                str(rxn),
                task_id="orca_task_preserved_123",
                metadata=_current_orca_queue_metadata(rxn),
            )
            self.assertNotEqual(entry.queue_id, entry.task_id)

            with patch(
                "orca_auto.orca.queue.worker.start_background_process"
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
                enqueue(root, str(d), metadata=_current_orca_queue_metadata(d))

            with patch(
                "orca_auto.orca.queue.worker.start_background_process"
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
                enqueue(
                    root,
                    str(reaction_dir),
                    metadata=_current_orca_queue_metadata(reaction_dir),
                )

            with patch(
                "orca_auto.orca.queue.worker.start_background_process"
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

            completed_entry = enqueue(
                root,
                str(first_dir),
                task_id="task_terminal_123",
                metadata=_current_orca_queue_metadata(first_dir),
            )
            pending_entry = enqueue(
                root,
                str(second_dir),
                metadata=_current_orca_queue_metadata(second_dir),
            )
            dequeue_next(root)
            _write_completed_run_state(first_dir)

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
                "orca_auto.orca.queue.worker.start_background_process"
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
            enqueue(
                root,
                str(queued),
                metadata=_current_orca_queue_metadata(queued),
            )

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
                    "orca_auto.orca.queue.worker.start_background_process"
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
            enqueue(
                root,
                str(queued),
                metadata=_current_orca_queue_metadata(queued),
            )

            with patch(
                "orca_auto.orca.queue.worker.start_background_process"
            ) as mock_start_background_process:
                mock_proc = MagicMock()
                mock_proc.pid = 4108
                mock_start_background_process.return_value = mock_proc
                worker._fill_slots()

            self.assertEqual(len(worker._running), 2)
            mock_start_background_process.assert_called_once()
