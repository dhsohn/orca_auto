from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from orca_auto.core.queue import processes as process_helpers
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.store import QueueLockTimeoutError, QueueStoreCorruptError
from orca_auto.core.queue.types import QueueStatus
from orca_auto.core.queue.worker import loop as loop_mod
from orca_auto.core.queue.worker import pid_file
from orca_auto.core.queue.worker.admission import select_next_claimable_entry
from orca_auto.core.queue.worker.models import ReservedQueueEntry, ReserveStatus
from tests.process_helpers import FakeManagedProcess, recording_killpg


def _entry(
    queue_id: str,
    *,
    status: str = "pending",
    priority: int = 10,
    enqueued_at: str = "2026-01-01T00:00:00Z",
    cancel_requested: bool = False,
    metadata: dict[str, object] | None = None,
) -> Any:
    entry_metadata: dict[str, object] = {QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_COMPLETE}
    entry_metadata.update(metadata or {})
    return SimpleNamespace(
        status=QueueStatus(status),
        priority=priority,
        enqueued_at=enqueued_at,
        queue_id=queue_id,
        cancel_requested=cancel_requested,
        metadata=entry_metadata,
    )


def _select(entries: list[Any], accept_entry_fn: Any = None) -> Any:
    return select_next_claimable_entry(entries, accept_entry_fn=accept_entry_fn)


def test_select_next_claimable_entry_handles_empty_and_single_pending_listing() -> None:
    entry = _entry("q-1")

    assert _select([]) is None
    assert _select([entry]) is entry


def test_select_next_claimable_entry_skips_running_cancelled_and_lower_priority_rows() -> None:
    winner = _entry("winner", priority=1)
    entries = [
        _entry("running", status="running", priority=0),
        _entry("cancelled", priority=0, cancel_requested=True),
        _entry("later", priority=5),
        winner,
    ]

    assert _select(entries) is winner


def test_select_next_claimable_entry_preserves_zero_priority() -> None:
    zero_priority = _entry("zero", priority=0)

    assert _select([_entry("one", priority=1), zero_priority]) is zero_priority


def test_select_next_claimable_entry_skips_entry_with_live_publisher() -> None:
    publishing = _entry(
        "publishing",
        priority=1,
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: os.getpid(),
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )
    ready = _entry("ready", priority=9)

    assert _select([publishing, ready]) is ready


def test_select_next_claimable_entry_accept_entry_fn_skips_other_engine_entries() -> None:
    # Engine workers share the runs root. Without the identity filter the
    # selection would claim the higher-priority foreign job and mis-run it;
    # the accept_entry_fn must skip it and pick this engine's own entry, and
    # rows behind a skipped one stay eligible.
    orca_entry = _entry("q_orca", priority=1)
    orca_entry.app_name = "orca_auto_orca"
    crest_entry = _entry("q_crest", priority=9)
    crest_entry.app_name = "orca_auto_crest"

    accepted = _select(
        [orca_entry, crest_entry],
        accept_entry_fn=lambda entry: getattr(entry, "app_name", "") == "orca_auto_crest",
    )

    assert accepted is crest_entry


def test_select_next_claimable_entry_keeps_fifo_when_the_clock_steps_backwards() -> None:
    # Regression: within one root the row position is the arrival order. A
    # WSL2 skew correction between two enqueues can stamp the first arrival
    # with a later enqueued_at than the second; the old sort key then
    # dispatched the second arrival first
    # (tests/test_queue_worker.py::TestFillSlots flake, 2026-07-16).
    first_arrival = _entry("q-first", priority=1, enqueued_at="2026-07-16T14:18:22.5+00:00")
    second_arrival = _entry("q-second", priority=1, enqueued_at="2026-07-16T14:18:19.4+00:00")

    assert _select([first_arrival, second_arrival]) is first_arrival


def test_select_next_claimable_entry_returns_none_when_only_ineligible_rows_remain() -> None:
    foreign = _entry("foreign")
    unpublished = _entry(
        "unpublished", metadata={QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING}
    )
    cancelled = _entry("cancelled", cancel_requested=True)
    running = _entry("running", status="running")
    later = _entry("later")

    def accept(entry: Any) -> bool:
        return entry.queue_id != "foreign"

    assert _select([foreign, unpublished, cancelled, running, later], accept) is later
    assert _select([foreign, unpublished, cancelled, running], accept) is None


def _reserved(token: str) -> ReservedQueueEntry:
    return ReservedQueueEntry(queue_root=Path("/runs"), entry=_entry(token), admission_token=token)


class _ScriptedLoop(loop_mod.QueueWorkerLoop):
    """Admits from a script; a started job joins ``_running`` until its rc is set."""

    def __init__(
        self,
        admissions: Iterable[tuple[ReserveStatus, ReservedQueueEntry | None]],
        *,
        max_concurrent: int = 2,
        start_succeeds: bool = True,
    ) -> None:
        super().__init__(max_concurrent=max_concurrent, poll_interval_seconds=0.0)
        self.admissions = iter(admissions)
        self.admit_calls = 0
        self.start_succeeds = start_succeeds
        self.finalized: list[tuple[str, int]] = []
        self.failing_finalize: set[str] = set()

    def _admit_next(self) -> tuple[ReserveStatus, ReservedQueueEntry | None]:
        self.admit_calls += 1
        return next(self.admissions)

    def _start_reserved(self, reserved: ReservedQueueEntry) -> bool:
        if self.start_succeeds:
            self._running[reserved.admission_token] = SimpleNamespace(rc=None)
        return self.start_succeeds

    def _poll_job(self, job: Any) -> int | None:
        return cast(int | None, job.rc)

    def _finalize_completed_job(self, queue_id: str, job: Any, rc: int) -> None:
        if queue_id in self.failing_finalize:
            raise RuntimeError("boom")
        self.finalized.append((queue_id, rc))


def test_fill_slots_starts_until_capacity_and_reports_processed() -> None:
    loop = _ScriptedLoop(
        [("processed", _reserved("slot-1")), ("processed", _reserved("slot-2")), ("idle", None)]
    )

    assert loop._fill_slots() == "processed"
    assert list(loop._running) == ["slot-1", "slot-2"]
    assert loop.admit_calls == 2


def test_fill_slots_preserves_blocked_status_before_starting() -> None:
    loop = _ScriptedLoop([("blocked", None)])

    assert loop._fill_slots() == "blocked"
    assert loop._running == {}


def test_fill_slots_respects_max_new_jobs() -> None:
    loop = _ScriptedLoop(
        [("processed", _reserved("slot-1")), ("processed", _reserved("slot-2"))],
        max_concurrent=5,
    )

    assert loop._fill_slots(max_new_jobs=1) == "processed"
    assert list(loop._running) == ["slot-1"]
    assert loop.admit_calls == 1


def test_fill_slots_does_not_count_a_failed_start() -> None:
    loop = _ScriptedLoop(
        [("processed", _reserved("slot-1")), ("processed", _reserved("slot-2"))],
        start_succeeds=False,
    )

    assert loop._fill_slots() == "processed"
    assert loop._running == {}
    assert loop.admit_calls == 1


def test_check_completed_jobs_finalizes_and_removes_finished_jobs() -> None:
    loop = _ScriptedLoop([])
    loop._running.update(
        {
            "q-running": SimpleNamespace(rc=None),
            "q-done": SimpleNamespace(rc=0),
            "q-failed": SimpleNamespace(rc=2),
        }
    )

    loop._check_completed_jobs()

    assert loop.finalized == [("q-done", 0), ("q-failed", 2)]
    assert list(loop._running) == ["q-running"]


def test_check_completed_jobs_keeps_a_job_whose_finalize_failed() -> None:
    loop = _ScriptedLoop([])
    loop.failing_finalize = {"q-bad"}
    loop._running.update({"q-bad": SimpleNamespace(rc=1), "q-good": SimpleNamespace(rc=0)})
    retained: set[str] = set()

    loop._check_completed_jobs(retained_completed_ids=retained)

    assert loop.finalized == [("q-good", 0)]
    assert list(loop._running) == ["q-bad"]
    assert retained == {"q-bad"}


def test_check_completed_jobs_drops_a_failed_finalize_the_handler_recovered() -> None:
    class _RecoveringLoop(_ScriptedLoop):
        def _on_finalize_error(self, queue_id: str, job: Any, rc: int, exc: Exception) -> bool:
            self.finalized.append((f"recovered {queue_id}", rc))
            return True

    loop = _RecoveringLoop([])
    loop.failing_finalize = {"q-bad"}
    loop._running.update({"q-bad": SimpleNamespace(rc=1), "q-good": SimpleNamespace(rc=0)})

    loop._check_completed_jobs()

    assert loop.finalized == [("recovered q-bad", 1), ("q-good", 0)]
    assert loop._running == {}


def test_queue_worker_loop_survives_finalize_error_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Loop(loop_mod.QueueWorkerLoop):
        def _poll_job(self, job: Any) -> int | None:
            return job.rc

        def _finalize_completed_job(self, queue_id: str, job: Any, rc: int) -> None:
            raise RuntimeError("finalize exploded")

    loop = _Loop(max_concurrent=1, poll_interval_seconds=0.0)
    loop._running["q-1"] = SimpleNamespace(rc=0)

    with caplog.at_level(logging.ERROR):
        loop._check_completed_jobs()  # must not raise: a finalize failure cannot kill the worker

    assert list(loop._running) == ["q-1"]
    assert any("finalize failed" in record.getMessage() for record in caplog.records)


class _FailingPassLoop(loop_mod.QueueWorkerLoop):
    """Admits one job, fails the first cancel pass that sees it running, then lets it exit."""

    def __init__(self, cancel_error: BaseException) -> None:
        super().__init__(
            max_concurrent=1,
            poll_interval_seconds=5.0,
            sleep_fn=lambda seconds: self.events.append(f"plain sleep {seconds}"),
        )
        self.cancel_error = cancel_error
        self.events: list[str] = []
        self.job = SimpleNamespace(rc=None)

    def _fill_slots(self, *, max_new_jobs: int | None = None) -> str:
        if self.events:
            return "idle"
        self.events.append("admit q-1")
        self._running["q-1"] = self.job
        return "processed"

    def _poll_job(self, job: Any) -> int | None:
        return job.rc

    def _check_cancel_requests(self) -> None:
        if self._running and "cancel pass failed" not in self.events:
            self.events.append("cancel pass failed")
            raise self.cancel_error

    def _sleep(self) -> None:
        self.events.append("poll sleep")
        if "cancel pass failed" in self.events:
            self.job.rc = 0

    def _finalize_completed_job(self, queue_id: str, job: Any, rc: int) -> None:
        self.events.append(f"finalize {queue_id} rc={rc}")
        self._shutdown_requested = True

    def _shutdown_running_job(self, queue_id: str, job: Any) -> None:
        self.events.append(f"shutdown sweep {queue_id}")


@pytest.mark.parametrize(
    ("entry_point", "expected_events"),
    [
        (
            "run",
            [
                "admit q-1",
                "poll sleep",
                "cancel pass failed",
                "plain sleep 5.0",
                "poll sleep",
                "finalize q-1 rc=0",
            ],
        ),
        (
            "run_once",
            [
                "admit q-1",
                "cancel pass failed",
                "plain sleep 5.0",
                "poll sleep",
                "finalize q-1 rc=0",
            ],
        ),
    ],
)
def test_queue_worker_loop_keeps_supervising_after_a_failed_poll_pass(
    entry_point: str,
    expected_events: list[str],
    preserved_signals: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An unreadable queue fails one pass. Ending the loop would run the shutdown
    # sweep, which stops the running child and restarts its calculation.
    del preserved_signals
    loop = _FailingPassLoop(QueueStoreCorruptError("Queue file is not valid JSON"))

    with caplog.at_level(logging.ERROR, logger=loop_mod.LOGGER.name):
        assert getattr(loop, entry_point)() == 0

    # The retry waits on the plain sleep: the subclass poll sleep may be the failing pass.
    assert loop.events == expected_events
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], QueueStoreCorruptError)
    assert loop._running == {}


def test_queue_worker_loop_system_exit_still_ends_supervision(preserved_signals: None) -> None:
    del preserved_signals
    loop = _FailingPassLoop(SystemExit(3))

    with pytest.raises(SystemExit):
        loop.run()

    assert loop.events == ["admit q-1", "poll sleep", "cancel pass failed", "shutdown sweep q-1"]


def test_queue_worker_loop_failed_pass_skips_the_retry_sleep_after_a_stop_request(
    preserved_signals: None,
) -> None:
    # The stop budget allows one poll sleep; a SIGTERM that lands while the
    # pass waits on the queue lock goes straight to the shutdown sweep.
    del preserved_signals

    class _StopDuringFailedPass(_FailingPassLoop):
        def _check_cancel_requests(self) -> None:
            if self._running:
                self._shutdown_requested = True
            super()._check_cancel_requests()

    loop = _StopDuringFailedPass(QueueLockTimeoutError("queue lock held past its deadline"))

    assert loop.run() == 0

    assert loop.events == ["admit q-1", "poll sleep", "cancel pass failed", "shutdown sweep q-1"]


def test_terminate_process_group_handles_finished_process() -> None:
    assert process_helpers.terminate_process_group(SimpleNamespace(poll=lambda: 0))


def _patch_group_host(
    monkeypatch: pytest.MonkeyPatch,
    killpg: Any,
    *,
    group_exists: bool,
    pid_exists: Any = None,
) -> None:
    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(
        process_helpers.process_utils, "process_group_exists", lambda _pgid: group_exists
    )
    if pid_exists is not None:
        monkeypatch.setattr(process_helpers, "_pid_exists", pid_exists)


def test_terminate_process_group_rejects_invalid_active_process_group_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = MagicMock()
    proc.pid = MagicMock(name="invalid_pid")
    proc.poll.return_value = None
    killpg, killpg_calls = recording_killpg()
    _patch_group_host(monkeypatch, killpg, group_exists=True)

    assert not process_helpers.terminate_process_group(proc)

    assert killpg_calls == []
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_terminate_process_group_does_not_signal_reused_reaped_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeManagedProcess(pid=321, poll_result=0)
    killpg, killpg_calls = recording_killpg()
    _patch_group_host(monkeypatch, killpg, group_exists=True, pid_exists=lambda _pid: True)

    assert process_helpers.terminate_process_group(proc)

    assert killpg_calls == []
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0


def test_terminate_process_group_refuses_unknown_reaped_pid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeManagedProcess(pid=322, poll_result=0)
    killpg, killpg_calls = recording_killpg()

    def unknown(_pid: int) -> bool:
        raise OSError("unknown pid probe failure")

    _patch_group_host(monkeypatch, killpg, group_exists=True, pid_exists=unknown)

    assert not process_helpers.terminate_process_group(proc)

    assert killpg_calls == []
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0


def test_terminate_process_group_falls_back_to_proc_methods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeManagedProcess(
        pid=123,
        wait_side_effects=[
            subprocess.TimeoutExpired(cmd="worker", timeout=1),
            subprocess.TimeoutExpired(cmd="worker", timeout=2),
        ],
    )
    killpg, killpg_calls = recording_killpg(
        side_effects=[
            ProcessLookupError("missing"),
            ProcessLookupError("missing"),
        ],
    )
    _patch_group_host(monkeypatch, killpg, group_exists=True)
    monkeypatch.setattr(
        process_helpers, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _s: None)
    )

    assert not process_helpers.terminate_process_group(proc, graceful_timeout=1, kill_timeout=2)

    assert killpg_calls == [(123, signal.SIGTERM), (123, signal.SIGKILL)]
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    # A fixed clock makes the remaining-timeout computation deterministic, so the
    # two waits use exactly the graceful and kill timeouts instead of depending
    # on wall-clock elapsed between reading the deadline and the remaining time.
    assert proc.wait_calls == [1, 2]


def test_terminate_process_group_returns_true_after_forced_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeManagedProcess(
        pid=124,
        wait_side_effects=[
            subprocess.TimeoutExpired(cmd="worker", timeout=1),
            0,
        ],
    )
    killpg, killpg_calls = recording_killpg()
    _patch_group_host(monkeypatch, killpg, group_exists=False)

    assert process_helpers.terminate_process_group(proc, graceful_timeout=1, kill_timeout=2)

    assert killpg_calls == [(124, signal.SIGTERM), (124, signal.SIGKILL)]
    assert proc.wait_calls == pytest.approx([1, 2], rel=1e-4)


@pytest.mark.parametrize("termination", ["unconfirmed", "raises"])
def test_retain_process_ownership_retries_termination_then_waits_for_exit(
    termination: str,
) -> None:
    class Process:
        exited = False

        def poll(self) -> int | None:
            return 0 if self.exited else None

    process = Process()
    terminate_calls: list[Process] = []
    sleeps: list[float] = []

    def terminate(actual: Process) -> bool:
        terminate_calls.append(actual)
        if termination == "raises":
            raise RuntimeError("termination callback failed")
        return False

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            process.exited = True

    process_helpers.retain_process_ownership_until_exit(
        process,
        terminate_process=terminate,
        sleep=sleep,
        retry_attempts=2,
        poll_interval_seconds=0.25,
    )

    # Two bounded termination attempts, then polling only until the process exits.
    assert terminate_calls == [process, process]
    assert sleeps == [0.25, 0.25, 0.25, 0.25]
    assert process.exited is True


def test_install_shutdown_signal_handlers_invokes_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: list[Any] = []
    requested: list[bool] = []

    monkeypatch.setattr(
        signal,
        "signal",
        lambda _signum, handler: handlers.append(handler),
    )

    process_helpers.install_shutdown_signal_handlers(lambda: requested.append(True))
    handlers[0](0, None)

    assert requested == [True]
    assert len(handlers) == 2


def test_install_shutdown_signal_handlers_ignores_non_main_thread_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempted: list[int] = []

    def refuse(signum: int, _handler: object) -> None:
        attempted.append(signum)
        raise ValueError("not main thread")

    monkeypatch.setattr(signal, "signal", refuse)

    with caplog.at_level(logging.DEBUG, logger="orca_auto.core.queue.processes"):
        process_helpers.install_shutdown_signal_handlers(
            lambda: pytest.fail("should not be called")
        )

    # The first refusal ends installation; no handler is left half-installed.
    assert attempted == [signal.SIGTERM]
    assert "only be installed from the main thread" in caplog.text


def test_worker_pid_file_handles_live_stale_dead_missing_and_invalid_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = pid_file.worker_pid_file_path(tmp_path)
    boot_id = pid_file.process_utils.linux_boot_id()
    assert boot_id is not None
    payload = json.dumps({"pid": 123, "process_start_ticks": 111, "boot_id": boot_id})

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(pid_file.process_utils.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(pid_file.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 111)
    assert pid_file.read_worker_pid_file(tmp_path) == 123

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(pid_file.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 222)
    assert pid_file.read_worker_pid_file(tmp_path) is None
    assert pid_path.read_text(encoding="utf-8") == payload

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(pid_file.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 111)
    monkeypatch.setattr(
        pid_file.process_utils.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert pid_file.read_worker_pid_file(tmp_path) is None
    assert pid_path.read_text(encoding="utf-8") == payload

    pid_path.unlink()
    assert pid_file.read_worker_pid_file(tmp_path) is None

    pid_path.write_text("not-a-pid\n", encoding="utf-8")
    assert pid_file.read_worker_pid_file(tmp_path) is None
    assert pid_path.read_text(encoding="utf-8") == "not-a-pid\n"


def test_run_once_reports_startup_failure_and_removes_pid_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from orca_auto.core.queue.worker import loop as loop_mod

    events: list[str] = []

    class _Loop(loop_mod.QueueWorkerLoop):
        def _before_run(self) -> None:
            events.append("before")
            raise TimeoutError("admission lock held by service restart")

        def _after_run(self) -> None:
            events.append("after")

        def _fill_slots(self, *, max_new_jobs: int | None = None) -> str:
            events.append("fill")
            return "idle"

    with caplog.at_level("ERROR", logger=loop_mod.LOGGER.name):
        assert _Loop(max_concurrent=1, poll_interval_seconds=0).run_once() == 1
    assert events == ["before", "after"]
    assert [r.getMessage() for r in caplog.records] == [
        "Queue worker startup failed: admission lock held by service restart"
    ]


@pytest.mark.parametrize(
    "old_contents",
    [
        json.dumps({"pid": 123, "process_start_ticks": 111, "boot_id": "previous-boot"}),
        "not-a-pid",
    ],
)
def test_pid_lookup_preserves_worker_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    old_contents: str,
) -> None:
    pid_path = pid_file.worker_pid_file_path(tmp_path)
    pid_path.write_text(old_contents, encoding="utf-8")
    read_payload = pid_file.process_utils.read_pid_payload
    replacement = b""

    def read_then_publish(path: Path) -> tuple[int | None, int | None, str | None]:
        nonlocal replacement
        old_payload = read_payload(path)
        # Deterministically schedule worker startup after the reader captured A.
        # Use the production atomic writer and the current process identity for B.
        pid_file.write_worker_pid_file(tmp_path)
        replacement = path.read_bytes()
        return old_payload

    with monkeypatch.context() as patch:
        patch.setattr(pid_file.process_utils, "read_pid_payload", read_then_publish)
        assert pid_file.read_worker_pid_file(tmp_path) is None

    assert pid_path.exists(), "PID lookup deleted the new worker's file"
    assert pid_path.read_bytes() == replacement
    assert pid_file.read_worker_pid_file(tmp_path) == os.getpid()
