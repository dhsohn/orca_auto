from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from orca_auto.core.queue import processes as process_helpers
from orca_auto.core.queue import worker as worker_common
from orca_auto.core.queue.child import process as child_process_helpers
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.worker import pid_file
from orca_auto.core.queue.worker.models import ReserveStatus
from tests.process_helpers import FakeManagedProcess, recording_killpg


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


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
        status=SimpleNamespace(value=status),
        priority=priority,
        enqueued_at=enqueued_at,
        queue_id=queue_id,
        cancel_requested=cancel_requested,
        metadata=entry_metadata,
    )


def _select(entries: list[Any], accept_entry_fn: Any = None) -> Any:
    return worker_common.select_next_claimable_entry(entries, accept_entry_fn=accept_entry_fn)


def test_resolve_admission_root_reads_the_runtime_property() -> None:
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(admission_root="/configured", resolved_admission_root="/resolved")
    )

    # The config owns the resolution; this adapter only exposes it as a callable.
    assert worker_common.resolve_admission_root(cfg) == "/resolved"


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


def test_reserve_dequeued_entry_releases_slot_when_dequeue_raises() -> None:
    released: list[str] = []

    with pytest.raises(RuntimeError, match="queue corrupt"):
        worker_common.reserve_dequeued_entry(
            has_capacity_fn=lambda: True,
            peek_next_fn=lambda: (Path("/allowed"), _entry("q-1")),
            reserve_slot_fn=lambda: "slot-1",
            dequeue_next_fn=lambda: (_ for _ in ()).throw(RuntimeError("queue corrupt")),
            release_slot_fn=released.append,
        )

    assert released == ["slot-1"]


def test_reserve_dequeued_entry_writes_nothing_to_admission_when_nothing_is_claimable() -> None:
    def reserve_slot() -> str:
        raise AssertionError("an idle poll must not reserve an admission slot")

    def release_slot(_token: str) -> None:
        raise AssertionError("an idle poll has no slot to release")

    def dequeue_next() -> None:
        raise AssertionError("an idle poll must not attempt a dequeue")

    assert worker_common.reserve_dequeued_entry(
        has_capacity_fn=lambda: True,
        peek_next_fn=lambda: None,
        reserve_slot_fn=reserve_slot,
        dequeue_next_fn=dequeue_next,
        release_slot_fn=release_slot,
    ) == ("idle", None)


def test_reserve_dequeued_entry_is_blocked_before_any_queue_read_when_the_pool_is_full() -> None:
    def peek_next() -> None:
        raise AssertionError("a full pool must not list any queue root")

    def reserve_slot() -> str:
        raise AssertionError("a full pool must not attempt a reservation")

    assert worker_common.reserve_dequeued_entry(
        has_capacity_fn=lambda: False,
        peek_next_fn=peek_next,
        reserve_slot_fn=reserve_slot,
        dequeue_next_fn=lambda: None,
        release_slot_fn=lambda _token: None,
    ) == ("blocked", None)


def test_reserve_dequeued_entry_reserves_only_after_a_claimable_preview() -> None:
    calls: list[str] = []
    entry = _entry("q-1")

    def reserve_slot() -> str:
        calls.append("reserve")
        return "slot-1"

    def dequeue_next() -> tuple[Path, Any]:
        calls.append("dequeue")
        return Path("/allowed"), entry

    status, reserved = worker_common.reserve_dequeued_entry(
        has_capacity_fn=lambda: _append_and_return(calls, "capacity", True),
        peek_next_fn=lambda: _append_and_return(calls, "peek", (Path("/allowed"), entry)),
        reserve_slot_fn=reserve_slot,
        dequeue_next_fn=dequeue_next,
        release_slot_fn=lambda _token: calls.append("release"),
    )

    assert status == "processed"
    assert reserved is not None
    assert reserved.entry is entry
    assert reserved.admission_token == "slot-1"
    assert calls == ["capacity", "peek", "reserve", "dequeue"]


def test_reserve_dequeued_entry_releases_slot_when_previewed_row_is_lost() -> None:
    released: list[str] = []

    assert worker_common.reserve_dequeued_entry(
        has_capacity_fn=lambda: True,
        peek_next_fn=lambda: (Path("/allowed"), _entry("q-1")),
        reserve_slot_fn=lambda: "slot-1",
        dequeue_next_fn=lambda: None,
        release_slot_fn=released.append,
    ) == ("idle", None)
    assert released == ["slot-1"]


def test_start_background_process_uses_detached_devnull_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    expected = object()

    def fake_popen(command: list[str], **kwargs: object) -> object:
        calls.append({"command": command, **kwargs})
        return expected

    monkeypatch.setattr(child_process_helpers.subprocess, "Popen", fake_popen)

    assert worker_common.start_background_process(("python", "-m", "worker")) is expected
    assert calls == [
        {
            "command": ["python", "-m", "worker"],
            "stdout": child_process_helpers.subprocess.DEVNULL,
            "stderr": child_process_helpers.subprocess.DEVNULL,
            "stdin": child_process_helpers.subprocess.DEVNULL,
            "start_new_session": True,
            "text": True,
        }
    ]


def test_start_background_process_redirects_output_to_log_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    expected = object()
    log_path = tmp_path / "logs" / "queue-1.log"

    def fake_popen(command: list[str], **kwargs: object) -> object:
        calls.append({"command": command, **kwargs})
        stdout = kwargs["stdout"]
        descriptor = getattr(stdout, "name", None)
        assert isinstance(descriptor, int)
        assert Path(f"/proc/self/fd/{descriptor}").resolve() == log_path.resolve()
        assert not bool(getattr(stdout, "closed", True))
        return expected

    monkeypatch.setattr(child_process_helpers.subprocess, "Popen", fake_popen)

    assert (
        worker_common.start_background_process(("python", "-m", "worker"), log_path=log_path)
        is expected
    )
    assert log_path.parent.exists()
    assert calls[0]["stderr"] == child_process_helpers.subprocess.STDOUT
    assert calls[0]["stdin"] == child_process_helpers.subprocess.DEVNULL
    assert calls[0]["start_new_session"] is True
    assert calls[0]["text"] is True


def test_fill_worker_slots_starts_until_capacity_and_reports_processed() -> None:
    running: list[str] = []
    reservations: Iterator[tuple[ReserveStatus, str | None]] = iter(
        [
            ("processed", "slot-1"),
            ("processed", "slot-2"),
            ("idle", None),
        ]
    )

    result = worker_common.fill_worker_slots(
        running_count=lambda: len(running),
        max_concurrent=2,
        reserve_next=lambda: next(reservations),
        start_reserved=lambda reserved: running.append(reserved),
    )

    assert result.status == "processed"
    assert result.started == 2
    assert running == ["slot-1", "slot-2"]


def test_fill_worker_slots_preserves_blocked_status_before_starting() -> None:
    result = worker_common.fill_worker_slots(
        running_count=lambda: 0,
        max_concurrent=2,
        reserve_next=lambda: ("blocked", None),
        start_reserved=lambda _reserved: pytest.fail("start should not run"),
    )

    assert result.status == "blocked"
    assert result.started == 0


def test_fill_worker_slots_respects_max_new_jobs() -> None:
    running: list[str] = []

    result = worker_common.fill_worker_slots(
        running_count=lambda: len(running),
        max_concurrent=5,
        reserve_next=lambda: ("processed", "slot"),
        start_reserved=lambda reserved: running.append(reserved),
        max_new_jobs=1,
    )

    assert result.status == "processed"
    assert result.started == 1
    assert running == ["slot"]


def test_fill_worker_slots_does_not_count_handled_start_failure() -> None:
    reservations: Iterator[tuple[ReserveStatus, str | None]] = iter(
        [
            ("processed", "slot-1"),
            ("processed", "slot-2"),
        ]
    )
    reserve_calls = 0

    def reserve_next() -> tuple[ReserveStatus, str | None]:
        nonlocal reserve_calls
        reserve_calls += 1
        return next(reservations)

    result = worker_common.fill_worker_slots(
        running_count=lambda: 0,
        max_concurrent=2,
        reserve_next=reserve_next,
        start_reserved=lambda _reserved: False,
    )

    assert result.status == "processed"
    assert result.started == 0
    assert reserve_calls == 1


def test_pop_completed_worker_jobs_finalizes_and_removes_finished_jobs() -> None:
    running = {
        "q-running": SimpleNamespace(rc=None),
        "q-done": SimpleNamespace(rc=0),
        "q-failed": SimpleNamespace(rc=2),
    }
    finalized: list[tuple[str, int]] = []

    count = worker_common.pop_completed_worker_jobs(
        running,
        poll_job=lambda job: job.rc,
        finalize_finished=lambda queue_id, _job, rc: finalized.append((queue_id, rc)),
    )

    assert count == 2
    assert finalized == [("q-done", 0), ("q-failed", 2)]
    assert list(running) == ["q-running"]


def test_pop_completed_worker_jobs_keeps_finished_job_when_finalizer_raises() -> None:
    running = {"q-done": SimpleNamespace(rc=0)}

    with pytest.raises(RuntimeError, match="finalizer failed"):
        worker_common.pop_completed_worker_jobs(
            running,
            poll_job=lambda job: job.rc,
            finalize_finished=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("finalizer failed")
            ),
        )

    assert list(running) == ["q-done"]


def test_pop_completed_worker_jobs_isolates_finalize_error_when_handler_supplied() -> None:
    running = {
        "q-bad": SimpleNamespace(rc=1),
        "q-good": SimpleNamespace(rc=0),
    }
    finalized: list[str] = []
    errors: list[tuple[str, int, str]] = []

    def finalize(queue_id: str, _job: object, rc: int) -> None:
        if queue_id == "q-bad":
            raise RuntimeError("boom")
        finalized.append(queue_id)

    count = worker_common.pop_completed_worker_jobs(
        running,
        poll_job=lambda job: job.rc,
        finalize_finished=finalize,
        on_finalize_error=lambda queue_id, _job, rc, exc: errors.append((queue_id, rc, str(exc))),
    )

    assert count == 2
    assert finalized == ["q-good"]
    assert errors == [("q-bad", 1, "boom")]
    assert list(running) == ["q-bad"]


def test_pop_completed_worker_jobs_removes_failed_finalize_when_handler_recovers() -> None:
    running = {
        "q-bad": SimpleNamespace(rc=1),
        "q-good": SimpleNamespace(rc=0),
    }
    recovered: list[str] = []

    def finalize(queue_id: str, _job: object, _rc: int) -> None:
        if queue_id == "q-bad":
            raise RuntimeError("boom")

    def recover(queue_id: str, _job: object, _rc: int, _exc: Exception) -> bool:
        recovered.append(queue_id)
        return True

    count = worker_common.pop_completed_worker_jobs(
        running,
        poll_job=lambda job: job.rc,
        finalize_finished=finalize,
        on_finalize_error=recover,
    )

    assert count == 2
    assert recovered == ["q-bad"]
    assert running == {}


def test_queue_worker_loop_survives_finalize_error_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Loop(worker_common.QueueWorkerLoop):
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


def test_terminate_process_group_handles_finished_process() -> None:
    assert worker_common.terminate_process_group(SimpleNamespace(poll=lambda: 0))


def test_terminate_process_group_rejects_invalid_active_process_group_id() -> None:
    proc = MagicMock()
    proc.pid = MagicMock(name="invalid_pid")
    proc.poll.return_value = None
    killpg, killpg_calls = recording_killpg()

    assert not worker_common.terminate_process_group(
        proc,
        killpg_fn=killpg,
        deps=process_helpers.ProcessGroupTerminationDeps(
            process_group_exists=lambda _pgid: True,
        ),
    )

    assert killpg_calls == []
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_terminate_process_group_does_not_signal_reused_reaped_pid() -> None:
    proc = FakeManagedProcess(pid=321, poll_result=0)
    killpg, killpg_calls = recording_killpg()

    assert worker_common.terminate_process_group(
        proc,
        killpg_fn=killpg,
        deps=process_helpers.ProcessGroupTerminationDeps(
            process_group_exists=lambda _pgid: True,
            pid_exists=lambda _pid: True,
        ),
    )

    assert killpg_calls == []
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0


def test_terminate_process_group_refuses_unknown_reaped_pid_identity() -> None:
    proc = FakeManagedProcess(pid=322, poll_result=0)
    killpg, killpg_calls = recording_killpg()

    def unknown(_pid: int) -> bool:
        raise OSError("unknown pid probe failure")

    assert not worker_common.terminate_process_group(
        proc,
        killpg_fn=killpg,
        deps=process_helpers.ProcessGroupTerminationDeps(
            process_group_exists=lambda _pgid: True,
            pid_exists=unknown,
        ),
    )

    assert killpg_calls == []
    assert proc.terminate_calls == 0
    assert proc.kill_calls == 0


def test_terminate_process_group_falls_back_to_proc_methods() -> None:
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

    assert not worker_common.terminate_process_group(
        proc,
        graceful_timeout=1,
        kill_timeout=2,
        killpg_fn=killpg,
        sigterm=15,
        sigkill=9,
        deps=process_helpers.ProcessGroupTerminationDeps(
            process_group_exists=lambda _pgid: True,
            monotonic=lambda: 0.0,
        ),
    )

    assert killpg_calls == [(123, 15), (123, 9)]
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
    # A fixed clock makes the remaining-timeout computation deterministic, so the
    # two waits use exactly the graceful and kill timeouts instead of depending
    # on wall-clock elapsed between reading the deadline and the remaining time.
    assert proc.wait_calls == [1, 2]


def test_terminate_process_group_returns_true_after_forced_exit() -> None:
    proc = FakeManagedProcess(
        pid=124,
        wait_side_effects=[
            subprocess.TimeoutExpired(cmd="worker", timeout=1),
            0,
        ],
    )
    killpg, killpg_calls = recording_killpg()

    assert worker_common.terminate_process_group(
        proc,
        graceful_timeout=1,
        kill_timeout=2,
        killpg_fn=killpg,
        sigterm=15,
        sigkill=9,
        deps=process_helpers.ProcessGroupTerminationDeps(
            process_group_exists=lambda _pgid: False,
        ),
    )

    assert killpg_calls == [(124, 15), (124, 9)]
    assert proc.wait_calls == pytest.approx([1, 2], rel=1e-4)


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

    worker_common.install_shutdown_signal_handlers(lambda: requested.append(True))
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
        worker_common.install_shutdown_signal_handlers(lambda: pytest.fail("should not be called"))

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
    assert not pid_path.exists()

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(pid_file.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 111)
    monkeypatch.setattr(
        pid_file.process_utils.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert pid_file.read_worker_pid_file(tmp_path) is None
    assert not pid_path.exists()

    assert pid_file.read_worker_pid_file(tmp_path) is None

    pid_path.write_text("not-a-pid\n", encoding="utf-8")
    assert pid_file.read_worker_pid_file(tmp_path) is None


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
