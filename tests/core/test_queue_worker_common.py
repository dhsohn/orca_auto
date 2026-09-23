from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from orca_auto.core.queue import processes as process_helpers
from orca_auto.core.queue import worker as worker_common
from orca_auto.core.queue.child import process as child_process_helpers
from orca_auto.core.queue.dependencies import BackgroundJobProcessStarter, ChildQueueWorkerDeps
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import process as worker_process_helpers
from orca_auto.core.queue.worker.models import (
    BackgroundRunningJob,
    ReservedQueueEntry,
    ReserveStatus,
)
from tests.process_helpers import FakeManagedProcess, recording_killpg


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


def _cfg(**runtime_overrides: object) -> SimpleNamespace:
    runtime: dict[str, object] = {
        "allowed_root": "/allowed",
        "admission_root": "",
        "admission_limit": None,
        "max_concurrent": 3,
    }
    runtime.update(runtime_overrides)
    runtime.setdefault(
        "resolved_admission_root", runtime["admission_root"] or runtime["allowed_root"]
    )
    runtime.setdefault(
        "resolved_admission_limit", runtime["admission_limit"] or runtime["max_concurrent"]
    )
    return SimpleNamespace(runtime=SimpleNamespace(**runtime))


def _worker_deps(
    *,
    poll_interval_seconds: float = 1,
    sleep: Callable[[float], None] = lambda _seconds: None,
    start_background_job_process: BackgroundJobProcessStarter | None = None,
) -> ChildQueueWorkerDeps[SimpleNamespace]:
    return ChildQueueWorkerDeps(
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
        release_slot=lambda _root, _token: None,
        has_admission_capacity=lambda _cfg: True,
        peek_next_entry=lambda _cfg, **_kwargs: None,
        dequeue_next_entry=lambda _cfg, **_kwargs: None,
        start_background_job_process=start_background_job_process
        or (lambda **_kwargs: FakeManagedProcess()),
        try_reserve_admission_slot=lambda _cfg: None,
    )


def _queue_entry(queue_id: str, task_id: str = "task-1") -> QueueEntry:
    return QueueEntry(queue_id, "orca_auto_orca", task_id, "orca_run_inp", "orca")


class _RecordingWorker(
    worker_common.PidFileChildProcessQueueWorker[SimpleNamespace, BackgroundRunningJob]
):
    worker_pid_file_name = "engine.pid"

    def __init__(
        self, cfg: SimpleNamespace, calls: list[tuple[str, str]], *, fail_finalize: bool = False
    ) -> None:
        super().__init__(cfg, config_path="/tmp/config.yaml", max_concurrent=2, deps=_worker_deps())
        self.calls = calls
        self.fail_finalize = fail_finalize

    def _finalize_completed_job(self, queue_id: str, job: BackgroundRunningJob, rc: int) -> None:
        self.calls.append(("finalize", queue_id))
        if self.fail_finalize:
            raise RuntimeError("finalize failed once")

    def _shutdown_running_job(self, queue_id: str, job: BackgroundRunningJob) -> None:
        self.calls.append(("shutdown", queue_id))


def _entry(
    queue_id: str,
    *,
    status: str = "pending",
    priority: int = 10,
    enqueued_at: str = "2026-01-01T00:00:00Z",
    cancel_requested: bool = False,
    metadata: dict[str, object] | None = None,
) -> SimpleNamespace:
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


def test_resolve_admission_root_reads_the_runtime_property() -> None:
    cfg = _cfg(
        resolved_admission_root="/resolved",
        admission_root="/configured",
    )

    # The config owns the resolution; this adapter only exposes it as a callable
    # for the worker modules that inject it as a value.
    assert worker_common.resolve_admission_root(cfg) == "/resolved"


def test_dequeue_next_across_roots_handles_single_root_idle_and_selected_entry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue"
    entry = _entry("q-1")

    assert (
        worker_common.dequeue_next_across_roots(
            (root,),
            list_queue_fn=lambda _root: [],
            dequeue_next_fn=lambda _root: None,
        )
        is None
    )
    assert worker_common.dequeue_next_across_roots(
        (root,),
        list_queue_fn=lambda _root: [],
        dequeue_next_fn=lambda _root: entry,
    ) == (root, entry)


def test_dequeue_next_across_roots_filters_single_root_and_dequeues_accepted_entry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue"
    rejected = _entry("q-rejected", priority=1)
    rejected.app_name = "orca_auto_orca"
    accepted = _entry("q-accepted", priority=9)
    accepted.app_name = "orca_auto_crest"
    entries = [rejected, accepted]
    dequeued_ids: list[str] = []

    def dequeue_entry(
        _root: Path,
        queue_id: str,
        **_kwargs: object,
    ) -> SimpleNamespace | None:
        dequeued_ids.append(queue_id)
        for entry in entries:
            if entry.queue_id == queue_id:
                return entry
        return None

    result = worker_common.dequeue_next_across_roots(
        (root,),
        list_queue_fn=lambda _root: entries,
        dequeue_next_fn=lambda _root: pytest.fail("filtered dequeue should use selected id"),
        dequeue_entry_fn=dequeue_entry,
        accept_entry_fn=lambda entry: getattr(entry, "app_name", "") == "orca_auto_crest",
    )

    assert result == (root, accepted)
    assert dequeued_ids == ["q-accepted"]


def test_dequeue_next_across_roots_single_root_filter_without_id_dequeuer_does_not_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "queue"
    rejected = _entry("q-rejected", priority=1)
    rejected.app_name = "orca_auto_orca"
    accepted = _entry("q-accepted", priority=9)
    accepted.app_name = "orca_auto_crest"
    dequeue_next_calls = 0

    def dequeue_next(_root: Path) -> SimpleNamespace | None:
        nonlocal dequeue_next_calls
        dequeue_next_calls += 1
        return rejected

    result = worker_common.dequeue_next_across_roots(
        (root,),
        list_queue_fn=lambda _root: [rejected, accepted],
        dequeue_next_fn=dequeue_next,
        accept_entry_fn=lambda entry: getattr(entry, "app_name", "") == "orca_auto_crest",
    )

    assert result is None
    assert dequeue_next_calls == 0


def test_dequeue_next_across_roots_selects_best_pending_entry(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    queues = {
        first: [
            _entry("running", status="running", priority=1),
            _entry("cancelled", priority=1, cancel_requested=True),
            _entry("later", priority=5, enqueued_at="2026-01-02T00:00:00Z"),
        ],
        second: [_entry("winner", priority=1, enqueued_at="2026-01-01T00:00:00Z")],
    }

    result = worker_common.dequeue_next_across_roots(
        (first, second),
        list_queue_fn=lambda root: queues[root],
        dequeue_next_fn=lambda root: queues[root][0],
    )

    assert result == (second, queues[second][0])


def test_dequeue_next_across_roots_preserves_zero_priority(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    zero_priority = _entry("zero", priority=0)
    queues = {
        first: [zero_priority],
        second: [_entry("one", priority=1)],
    }

    result = worker_common.dequeue_next_across_roots(
        (first, second),
        list_queue_fn=lambda root: queues[root],
        dequeue_next_fn=lambda root: queues[root][0],
    )

    assert result == (first, zero_priority)


def test_dequeue_next_across_roots_skips_entry_with_live_publisher(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
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
    queues = {first: [publishing], second: [ready]}

    result = worker_common.dequeue_next_across_roots(
        (first, second),
        list_queue_fn=lambda root: queues[root],
        dequeue_next_fn=lambda root: queues[root][0],
    )

    assert result == (second, ready)


def test_dequeue_next_across_roots_accept_entry_fn_skips_other_engine_entries(
    tmp_path: Path,
) -> None:
    # After the single-runs-root collapse, an internal-engine worker's queue
    # roots include the standalone ORCA queue alongside its workflow stage
    # queues. Without an app filter the cross-root selection would claim the
    # higher-priority ORCA job (first root) and mis-run it as CREST; the
    # accept_entry_fn must skip it and pick this engine's own entry.
    orca_root = tmp_path / "orca_runs"
    crest_root = tmp_path / "orca_runs" / "wf_x" / "01_crest"
    orca_entry = _entry("q_orca", priority=1)
    orca_entry.app_name = "orca_auto_orca"
    crest_entry = _entry("q_crest", priority=9)
    crest_entry.app_name = "orca_auto_crest"
    queues = {orca_root: [orca_entry], crest_root: [crest_entry]}

    result = worker_common.dequeue_next_across_roots(
        (orca_root, crest_root),
        list_queue_fn=lambda root: queues[root],
        dequeue_next_fn=lambda root: queues[root][0],
        accept_entry_fn=lambda entry: getattr(entry, "app_name", "") == "orca_auto_crest",
    )

    assert result == (crest_root, crest_entry)


def test_dequeue_next_across_roots_keeps_root_fifo_when_the_clock_steps_backwards(
    tmp_path: Path,
) -> None:
    # Regression: within one root the row position is the arrival order. A
    # WSL2 skew correction between two enqueues can stamp the first arrival
    # with a later enqueued_at than the second; the old sort key then
    # dispatched the second arrival first
    # (tests/test_queue_worker.py::TestFillSlots flake, 2026-07-16).
    root = tmp_path / "queue"
    first_arrival = _entry("q-first", priority=1, enqueued_at="2026-07-16T14:18:22.5+00:00")
    second_arrival = _entry("q-second", priority=1, enqueued_at="2026-07-16T14:18:19.4+00:00")
    entries = [first_arrival, second_arrival]

    def dequeue_entry(
        _root: Path,
        queue_id: str,
        **_kwargs: object,
    ) -> SimpleNamespace | None:
        return next((entry for entry in entries if entry.queue_id == queue_id), None)

    result = worker_common.dequeue_next_across_roots(
        (root,),
        list_queue_fn=lambda _root: entries,
        dequeue_next_fn=lambda _root: pytest.fail("id dequeuer should claim the selection"),
        dequeue_entry_fn=dequeue_entry,
        accept_entry_fn=lambda _entry: True,
    )

    assert result == (root, first_arrival)


def test_dequeue_next_across_roots_dequeues_selected_queue_id(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    winner = _entry("winner", priority=1, enqueued_at="2026-01-01T00:00:00Z")
    queues = {
        first: [_entry("wrong-root-entry", priority=5)],
        second: [winner, _entry("same-root-later", priority=9)],
    }
    dequeued_calls: list[tuple[Path, str, dict[str, object]]] = []

    def dequeue_entry(
        root: Path,
        queue_id: str,
        **kwargs: object,
    ) -> SimpleNamespace | None:
        dequeued_calls.append((root, queue_id, kwargs))
        for entry in queues[root]:
            if entry.queue_id == queue_id:
                return entry
        return None

    def fail_dequeue_next(_root: Path) -> SimpleNamespace | None:
        pytest.fail("dequeue_next should not ignore selected id")

    result = worker_common.dequeue_next_across_roots(
        (first, second),
        list_queue_fn=lambda root: queues[root],
        dequeue_next_fn=fail_dequeue_next,
        dequeue_entry_fn=dequeue_entry,
    )

    assert result == (second, winner)
    assert dequeued_calls == [
        (second, "winner", {"expected_entry": winner}),
    ]


def test_dequeue_next_across_roots_returns_none_when_selected_entry_disappears(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    def fail_dequeue_next(_root: Path) -> SimpleNamespace | None:
        pytest.fail("dequeue_next should not run with id dequeuer")

    def missing_entry(
        _root: Path,
        _queue_id: str,
        **_kwargs: object,
    ) -> SimpleNamespace | None:
        return None

    result = worker_common.dequeue_next_across_roots(
        (first, second),
        list_queue_fn=lambda _root: [_entry("pending")],
        dequeue_next_fn=fail_dequeue_next,
        dequeue_entry_fn=missing_entry,
    )

    assert result is None


def test_dequeue_next_across_roots_returns_none_when_selected_root_dequeues_empty(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"

    assert (
        worker_common.dequeue_next_across_roots(
            (root, tmp_path / "other"),
            list_queue_fn=lambda _root: [_entry("pending")],
            dequeue_next_fn=lambda _root: None,
        )
        is None
    )


def test_reserve_dequeued_entry_releases_slot_when_dequeue_raises() -> None:
    released: list[tuple[str, str]] = []

    with pytest.raises(RuntimeError, match="queue corrupt"):
        worker_common.reserve_dequeued_entry(
            _cfg(admission_root="/tmp/admission"),
            admission_root="/tmp/admission",
            has_capacity_fn=lambda _cfg: True,
            peek_next_fn=lambda _cfg: (Path("/allowed"), _entry("q-1")),
            reserve_slot_fn=lambda _cfg: "slot-1",
            dequeue_next_fn=lambda _cfg: (_ for _ in ()).throw(RuntimeError("queue corrupt")),
            release_slot_fn=lambda root, token: released.append((str(root), token)),
        )

    assert released == [("/tmp/admission", "slot-1")]


def test_reserve_dequeued_entry_writes_nothing_to_admission_when_nothing_is_claimable() -> None:
    def reserve_slot(_cfg: Any) -> str:
        raise AssertionError("an idle poll must not reserve an admission slot")

    def release_slot(_root: Any, _token: str) -> None:
        raise AssertionError("an idle poll has no slot to release")

    def dequeue_next(_cfg: Any) -> None:
        raise AssertionError("an idle poll must not attempt a dequeue")

    assert worker_common.reserve_dequeued_entry(
        _cfg(admission_root="/tmp/admission"),
        admission_root="/tmp/admission",
        has_capacity_fn=lambda _cfg: True,
        peek_next_fn=lambda _cfg: None,
        reserve_slot_fn=reserve_slot,
        dequeue_next_fn=dequeue_next,
        release_slot_fn=release_slot,
    ) == ("idle", None)


def test_reserve_dequeued_entry_is_blocked_before_any_queue_read_when_the_pool_is_full() -> None:
    def peek_next(_cfg: Any) -> None:
        raise AssertionError("a full pool must not list any queue root")

    def reserve_slot(_cfg: Any) -> str:
        raise AssertionError("a full pool must not attempt a reservation")

    assert worker_common.reserve_dequeued_entry(
        _cfg(admission_root="/tmp/admission"),
        admission_root="/tmp/admission",
        has_capacity_fn=lambda _cfg: False,
        peek_next_fn=peek_next,
        reserve_slot_fn=reserve_slot,
        dequeue_next_fn=lambda _cfg: None,
        release_slot_fn=lambda _root, _token: None,
    ) == ("blocked", None)


def test_reserve_dequeued_entry_reserves_only_after_a_claimable_preview() -> None:
    calls: list[str] = []
    entry = _entry("q-1")

    def reserve_slot(_cfg: Any) -> str:
        calls.append("reserve")
        return "slot-1"

    def dequeue_next(_cfg: Any) -> tuple[Path, Any]:
        calls.append("dequeue")
        return Path("/allowed"), entry

    status, reserved = worker_common.reserve_dequeued_entry(
        _cfg(admission_root="/tmp/admission"),
        admission_root="/tmp/admission",
        has_capacity_fn=lambda _cfg: _append_and_return(calls, "capacity", True),
        peek_next_fn=lambda _cfg: _append_and_return(calls, "peek", (Path("/allowed"), entry)),
        reserve_slot_fn=reserve_slot,
        dequeue_next_fn=dequeue_next,
        release_slot_fn=lambda _root, _token: calls.append("release"),
    )

    assert status == "processed"
    assert reserved is not None
    assert reserved.entry is entry
    assert reserved.admission_token == "slot-1"
    assert calls == ["capacity", "peek", "reserve", "dequeue"]


def test_reserve_dequeued_entry_releases_slot_when_previewed_row_is_lost() -> None:
    released: list[tuple[str, str]] = []

    assert worker_common.reserve_dequeued_entry(
        _cfg(admission_root="/tmp/admission"),
        admission_root="/tmp/admission",
        has_capacity_fn=lambda _cfg: True,
        peek_next_fn=lambda _cfg: (Path("/allowed"), _entry("q-1")),
        reserve_slot_fn=lambda _cfg: "slot-1",
        dequeue_next_fn=lambda _cfg: None,
        release_slot_fn=lambda root, token: released.append((str(root), token)),
    ) == ("idle", None)
    assert released == [("/tmp/admission", "slot-1")]


def test_peek_next_across_roots_selects_exactly_what_the_dequeue_would_claim(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    foreign = _entry("foreign", enqueued_at="2026-01-01T00:00:00Z")
    unpublished = _entry(
        "unpublished",
        enqueued_at="2026-01-01T00:00:01Z",
        metadata={QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING},
    )
    cancelled = _entry("cancelled", enqueued_at="2026-01-01T00:00:02Z", cancel_requested=True)
    running = _entry("running", status="running", enqueued_at="2026-01-01T00:00:03Z")
    later = _entry("later", enqueued_at="2026-01-01T00:00:09Z")
    earlier = _entry("earlier", enqueued_at="2026-01-01T00:00:04Z")
    queues = {root_a: [foreign, unpublished, cancelled, running, later], root_b: [earlier]}
    dequeued: list[tuple[Path, str]] = []

    def accept(entry: Any) -> bool:
        return entry.queue_id != "foreign"

    def dequeue_entry(root: Path, queue_id: str, *, expected_entry: Any) -> Any:
        dequeued.append((root, queue_id))
        return expected_entry

    preview = worker_common.peek_next_across_roots(
        (root_a, root_b),
        list_queue_fn=lambda root: queues[root],
        select_all_rows=True,
        accept_entry_fn=accept,
    )
    claimed = worker_common.dequeue_next_across_roots(
        (root_a, root_b),
        list_queue_fn=lambda root: queues[root],
        dequeue_next_fn=lambda _root: None,
        dequeue_entry_fn=dequeue_entry,
        accept_entry_fn=accept,
    )

    assert preview == (root_b, earlier)
    assert claimed == (root_b, earlier)
    assert dequeued == [(root_b, "earlier")]

    assert (
        worker_common.peek_next_across_roots(
            (root_a,),
            list_queue_fn=lambda _root: [foreign, unpublished, cancelled, running],
            select_all_rows=True,
            accept_entry_fn=accept,
        )
        is None
    )


def test_peek_next_across_roots_defers_eligibility_to_the_single_root_dequeue() -> None:
    root = Path("/allowed")
    unpublished = _entry(
        "unpublished", metadata={QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING}
    )
    cancelled = _entry("cancelled", cancel_requested=True)

    # With one root and no acceptance filter the dequeue fast path hands the
    # whole eligibility rule to the store, so the preview may only demand a
    # pending, uncancelled row: it must never be stricter than that dequeue.
    assert worker_common.peek_next_across_roots(
        (root,),
        list_queue_fn=lambda _root: [cancelled, unpublished],
        select_all_rows=False,
    ) == (root, unpublished)
    assert (
        worker_common.peek_next_across_roots(
            (root,),
            list_queue_fn=lambda _root: [cancelled, _entry("done", status="completed")],
            select_all_rows=False,
        )
        is None
    )


def test_queue_entry_by_id_scans_queue_with_injected_lister(tmp_path: Path) -> None:
    entries = [
        QueueEntry(queue_id=qid, app_name="app", task_id=qid, task_kind="task", engine="orca")
        for qid in ("q-1", "q-2")
    ]

    assert (
        worker_common.queue_entry_by_id(
            tmp_path,
            "q-2",
            list_queue_fn=lambda root: entries if root == tmp_path else [],
        )
        is entries[1]
    )
    assert (
        worker_common.queue_entry_by_id(
            tmp_path,
            "missing",
            list_queue_fn=lambda _root: entries,
        )
        is None
    )


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


def test_pidfile_worker_reconciles_snapshots_and_finalizes_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _RecordingWorker(_cfg(allowed_root=str(tmp_path)), [])
    reconciled: list[tuple[Path, ...]] = []
    events: list[str] = []
    monkeypatch.setattr(
        worker_process_helpers, "snapshot_runtime_roots_for_cfg", lambda _cfg: (tmp_path,)
    )
    monkeypatch.setattr(
        worker_process_helpers,
        "reconcile_orphaned_snapshot_generations",
        lambda roots: _append_and_return(reconciled, roots, 0),
    )
    monkeypatch.setattr(
        worker_process_helpers,
        "finalize_queued_snapshot_intent",
        lambda *_args: events.append("intent"),
    )
    monkeypatch.setattr(
        worker, "_start_job", lambda *_args, **_kwargs: _append_and_return(events, "start", True)
    )
    entry = _queue_entry("q-1")
    assert worker._start_reserved(ReservedQueueEntry(tmp_path, entry, "slot"))
    assert events == ["intent", "start"]
    worker._reconcile_worker_state()
    worker._reconcile_worker_state()
    assert reconciled == [(tmp_path,)]


def test_child_process_worker_throttles_idle_state_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    reconcile_calls: list[float] = []
    sleep_calls: list[float] = []

    class _Worker(worker_process_helpers.ChildProcessQueueWorker):
        def _reconcile_worker_state(self) -> None:
            reconcile_calls.append(now[0])

    monkeypatch.setattr(worker_process_helpers.time, "monotonic", lambda: now[0])
    worker = _Worker(
        _cfg(),
        config_path="/tmp/config.yaml",
        deps=_worker_deps(poll_interval_seconds=5.0, sleep=sleep_calls.append),
    )

    worker._before_run()
    worker._sleep()
    now[0] = 59.0
    worker._sleep()
    now[0] = 60.0
    worker._sleep()

    assert reconcile_calls == [0.0, 60.0]
    assert sleep_calls == [5.0, 5.0, 5.0]


def test_shutdown_all_reaps_finished_job_before_requeuing(tmp_path: Path) -> None:
    # A child that finished during the final poll interval must be reaped through
    # the normal completion path at shutdown, not force-terminated and requeued
    # (which would needlessly re-run a completed job on the next worker start).
    cfg = _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission"))
    calls: list[tuple[str, str]] = []
    worker = _RecordingWorker(cfg, calls, fail_finalize=False)
    finished = BackgroundRunningJob(
        tmp_path, _queue_entry("done"), FakeManagedProcess(poll_result=0), "slot-done"
    )
    still_running = BackgroundRunningJob(
        tmp_path, _queue_entry("busy"), FakeManagedProcess(), "slot-busy"
    )
    worker._running = {"done": finished, "busy": still_running}

    worker._shutdown_all()

    # The finished job is finalized (completed), never requeued via shutdown.
    assert ("finalize", "done") in calls
    assert ("shutdown", "done") not in calls
    # The still-running job is shut down (terminated + requeued) as before.
    assert ("shutdown", "busy") in calls
    assert worker._running == {}


def test_shutdown_all_does_not_requeue_exited_job_after_finalize_failure(
    tmp_path: Path,
) -> None:
    cfg = _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission"))
    calls: list[tuple[str, str]] = []

    worker = _RecordingWorker(cfg, calls, fail_finalize=True)
    finished = BackgroundRunningJob(
        tmp_path, _queue_entry("done"), FakeManagedProcess(poll_result=0), "slot-done"
    )
    still_running = BackgroundRunningJob(
        tmp_path, _queue_entry("busy"), FakeManagedProcess(), "slot-busy"
    )
    worker._running = {"done": finished, "busy": still_running}

    worker._shutdown_all()

    assert ("finalize", "done") in calls
    assert ("shutdown", "done") not in calls
    assert ("shutdown", "busy") in calls
    assert worker._running == {"done": finished}


def test_pidfile_child_worker_run_once_returns_error_when_singleton_lock_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission"))
    worker = _RecordingWorker(cfg, [])
    lock_calls: list[tuple[Path, float]] = []

    def locked_file_lock(path: Path, *, timeout_seconds: float) -> object:
        lock_calls.append((path, timeout_seconds))
        raise TimeoutError("held")

    monkeypatch.setattr(
        worker_process_helpers,
        "file_lock",
        locked_file_lock,
    )

    assert worker.run_once(idle_message=None, blocked_message=None) == 1

    captured = capsys.readouterr()
    assert "queue worker already running" in captured.err
    assert lock_calls == [(tmp_path / "engine.pid.lock", 0.0)]
    assert not worker._pid_file_path().exists()


def test_child_worker_rejected_attach_terminates_and_marks_start_error(tmp_path: Path) -> None:
    terminate_calls = 0
    start_errors: list[tuple[Path, str, str]] = []

    class RejectingWorker(worker_common.ChildProcessQueueWorker):
        def _on_worker_process_started(
            self,
            queue_root: Path,
            entry: object,
            *,
            process: object,
            admission_token: str,
        ) -> bool:
            del queue_root, entry, process, admission_token
            return False

        def _handle_worker_start_error(
            self,
            queue_root: Path,
            entry: object,
            admission_token: str,
            exc: OSError,
        ) -> None:
            start_errors.append((queue_root, admission_token, str(exc)))

    class FakeProcess(FakeManagedProcess):
        def terminate(self) -> None:
            nonlocal terminate_calls
            terminate_calls += 1

    process = FakeProcess()
    deps = _worker_deps(start_background_job_process=lambda **_kwargs: process)
    worker = RejectingWorker(
        _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission")),
        config_path="/tmp/config.yaml",
        max_concurrent=1,
        deps=deps,
    )
    entry = _queue_entry("queue-reject")

    assert not worker._start_job(tmp_path / "queue", entry, admission_token="slot-1")
    assert terminate_calls == 1
    assert start_errors == [(tmp_path / "queue", "slot-1", "worker attach rejected")]
    assert worker._running == {}


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
        worker_common.signal,
        "signal",
        lambda _signum, handler: handlers.append(handler),
    )

    worker_common.install_shutdown_signal_handlers(lambda: requested.append(True))
    handlers[0](0, None)

    assert requested == [True]
    assert len(handlers) == 2


def test_install_shutdown_signal_handlers_ignores_non_main_thread_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_common.signal,
        "signal",
        lambda *_args: (_ for _ in ()).throw(ValueError("not main thread")),
    )

    worker_common.install_shutdown_signal_handlers(lambda: pytest.fail("should not be called"))


def test_worker_pid_file_handles_live_stale_dead_missing_and_invalid_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = process_helpers.worker_pid_file_path(tmp_path)
    boot_id = process_helpers.process_utils.linux_boot_id()
    assert boot_id is not None
    payload = json.dumps({"pid": 123, "process_start_ticks": 111, "boot_id": boot_id})

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(process_helpers.process_utils.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        process_helpers.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 111
    )
    assert process_helpers.read_worker_pid_file(tmp_path) == 123

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        process_helpers.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 222
    )
    assert process_helpers.read_worker_pid_file(tmp_path) is None
    assert not pid_path.exists()

    pid_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        process_helpers.process_utils, "process_start_ticks", lambda _pid, **_kwargs: 111
    )
    monkeypatch.setattr(
        process_helpers.process_utils.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert process_helpers.read_worker_pid_file(tmp_path) is None
    assert not pid_path.exists()

    assert process_helpers.read_worker_pid_file(tmp_path) is None

    pid_path.write_text("not-a-pid\n", encoding="utf-8")
    assert process_helpers.read_worker_pid_file(tmp_path) is None


def test_reconcile_orphaned_child_queue_entries_cancels_or_requeues_only_orphans(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    entries = [
        _entry("live", status="running"),
        _entry("cancelled", status="running", cancel_requested=True),
        _entry("orphaned", status="running"),
        _entry("pending", status="pending"),
    ]
    stale_reconciled: list[str] = []
    cancelled: list[tuple[str, str, str]] = []
    requeued: list[tuple[str, str]] = []
    recovery_pending: list[str] = []

    child_process_helpers.reconcile_orphaned_child_queue_entries(
        _cfg(),
        admission_root="/tmp/admission",
        queue_roots_fn=lambda _cfg: (queue_root,),
        list_queue_fn=lambda _queue_root: entries,
        list_slots_fn=lambda _admission_root: [SimpleNamespace(queue_id="live")],
        reconcile_stale_slots_fn=lambda admission_root: stale_reconciled.append(
            str(admission_root)
        ),
        running_status=SimpleNamespace(value="running"),
        mark_cancelled_fn=lambda root, queue_id, *, error, **_kwargs: cancelled.append(
            (str(root), queue_id, error)
        ),
        requeue_running_entry_fn=lambda root, queue_id, **_kwargs: _append_and_return(
            requeued,
            (str(root), queue_id),
            next(current for current in entries if current.queue_id == queue_id),
        ),
        mark_recovery_pending_fn=lambda _cfg, entry: recovery_pending.append(entry.queue_id),
    )

    assert stale_reconciled == ["/tmp/admission"]
    assert cancelled == [(str(queue_root), "cancelled", "cancel_requested")]
    assert requeued == [(str(queue_root), "orphaned")]
    assert recovery_pending == ["orphaned"]


def test_start_error_mark_is_fenced_to_selected_entry(tmp_path: Path) -> None:
    selected = _queue_entry("q-same", "task-a")
    replacement = SimpleNamespace(queue_id="q-same", task_id="task-b")
    durable = [replacement]
    released: list[str] = []
    worker = SimpleNamespace(
        _running_queue_id=lambda entry: entry.queue_id,
        _release_admission_slot=released.append,
    )

    def mark_failed(
        _root: Path,
        queue_id: str,
        *,
        expected_entry: object,
        **_kwargs: object,
    ) -> None:
        if durable[0] is expected_entry:
            durable.clear()
        assert queue_id == "q-same"

    worker_process_helpers.ChildProcessQueueWorker._mark_entry_failed_and_release(
        cast(Any, worker),
        tmp_path,
        selected,
        "slot-a",
        error="start failed",
        mark_failed_fn=mark_failed,
    )

    assert durable == [replacement]
    assert released == ["slot-a"]


def test_shutdown_child_process_with_grace_retains_live_child_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monotonic_values = iter([0.0, 0.05, 0.2])

    class Process:
        rc: int | None = None

        def poll(self) -> int | None:
            return self.rc

        def terminate(self) -> None:
            calls.append("terminate")

    process = Process()
    job = SimpleNamespace(process=process)
    finalized: list[tuple[object, int]] = []

    monkeypatch.setattr(child_process_helpers.time, "monotonic", lambda: next(monotonic_values))

    result = child_process_helpers.shutdown_child_process_with_grace(
        job,
        finalize_child_exit_fn=lambda job_arg, rc: finalized.append((job_arg, rc)),
        grace_seconds=0.1,
        sleep_fn=lambda seconds: calls.append(f"sleep:{seconds}"),
    )

    assert result is False
    assert calls == ["terminate", "sleep:0.1"]
    assert finalized == []


def test_shutdown_child_process_with_grace_does_not_sleep_past_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monotonic_values = iter([0.0, 0.2])

    class Process:
        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            calls.append("terminate")

    job = SimpleNamespace(process=Process())
    finalized: list[int] = []

    monkeypatch.setattr(child_process_helpers.time, "monotonic", lambda: next(monotonic_values))

    result = child_process_helpers.shutdown_child_process_with_grace(
        job,
        finalize_child_exit_fn=lambda _job, rc: finalized.append(rc),
        grace_seconds=0.1,
        sleep_fn=lambda seconds: calls.append(f"sleep:{seconds}"),
    )

    assert result is False
    assert calls == ["terminate"]
    assert finalized == []


def test_shutdown_child_process_with_grace_retains_when_terminate_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        rc: int | None = None

        def poll(self) -> int | None:
            return self.rc

        def terminate(self) -> None:
            raise RuntimeError("cannot terminate")

    process = Process()
    job = SimpleNamespace(process=process)
    finalized: list[int] = []

    monkeypatch.setattr(child_process_helpers.time, "monotonic", lambda: 0.0)

    result = child_process_helpers.shutdown_child_process_with_grace(
        job,
        finalize_child_exit_fn=lambda _job, rc: finalized.append(rc),
        grace_seconds=0.0,
        sleep_fn=lambda _seconds: pytest.fail("sleep should not run after deadline"),
    )

    assert result is False
    assert finalized == []
