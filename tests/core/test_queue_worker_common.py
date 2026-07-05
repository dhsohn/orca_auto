from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import lifecycle as lifecycle_helpers
from orca_auto.core.queue import processes as process_helpers
from orca_auto.core.queue import worker as worker_common
from orca_auto.core.queue.child import process as child_process_helpers
from orca_auto.core.queue.dependencies import (
    build_dependency_container,
    dependency_group,
    resolve_dependency_groups,
)
from orca_auto.core.queue.worker import process as worker_process_helpers
from tests.process_helpers import FakeManagedProcess, recording_killpg


def _cfg(**runtime_overrides: object) -> SimpleNamespace:
    runtime: dict[str, object] = {
        "allowed_root": "/allowed",
        "admission_root": "",
        "admission_limit": None,
        "max_concurrent": 3,
    }
    runtime.update(runtime_overrides)
    return SimpleNamespace(runtime=SimpleNamespace(**runtime))


def _entry(
    queue_id: str,
    *,
    status: str = "pending",
    priority: int = 10,
    enqueued_at: str = "2026-01-01T00:00:00Z",
    cancel_requested: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(value=status),
        priority=priority,
        enqueued_at=enqueued_at,
        queue_id=queue_id,
        cancel_requested=cancel_requested,
    )


def test_dependency_group_prefers_explicit_value_and_lazily_builds_default() -> None:
    calls = 0

    def default_factory() -> str:
        nonlocal calls
        calls += 1
        return "default"

    assert dependency_group("explicit", default_factory) == "explicit"
    assert calls == 0
    assert dependency_group(None, default_factory) == "default"
    assert calls == 1


def test_resolve_dependency_groups_prefers_overrides_and_lazily_builds_missing() -> None:
    calls: list[str] = []

    def default_a() -> str:
        calls.append("a")
        return "default-a"

    def default_b() -> str:
        calls.append("b")
        return "default-b"

    resolved = resolve_dependency_groups(
        {"a": "override-a", "b": None},
        {"a": default_a, "b": default_b},
    )

    assert resolved == {"a": "override-a", "b": "default-b"}
    assert calls == ["b"]


def test_build_dependency_container_resolves_groups_and_extra_fields() -> None:
    @dataclass(frozen=True)
    class Container:
        a: str
        b: str
        extra: str

    calls: list[str] = []

    def default_a() -> str:
        calls.append("a")
        return "default-a"

    def default_b() -> str:
        calls.append("b")
        return "default-b"

    container = build_dependency_container(
        Container,
        {"a": "override-a", "b": None},
        {"a": default_a, "b": default_b},
        extra_fields={"extra": "value"},
    )

    assert container == Container(a="override-a", b="default-b", extra="value")
    assert calls == ["b"]


def test_resolve_admission_root_and_limit_prefer_resolved_values() -> None:
    cfg = _cfg(
        resolved_admission_root="/resolved",
        resolved_admission_limit="5",
        admission_root="/configured",
        admission_limit=2,
    )

    assert worker_common.resolve_admission_root(cfg) == "/resolved"
    assert worker_common.resolve_admission_limit(cfg) == 5


def test_resolve_admission_limit_falls_back_and_handles_invalid_values() -> None:
    assert (
        worker_common.resolve_admission_limit(_cfg(resolved_admission_limit=0, max_concurrent=7))
        == 7
    )
    with pytest.raises(ValueError, match="admission_limit must be an integer >= 1"):
        worker_common.resolve_admission_limit(_cfg(resolved_admission_limit="bad"))


def test_reserve_queue_worker_slot_uses_common_resolved_values() -> None:
    calls: list[tuple[str, int, str, str]] = []

    def reserve_slot(root: str, limit: int, *, source: str, app_name: str) -> str:
        calls.append((root, limit, source, app_name))
        return "slot-1"

    result = worker_common.reserve_queue_worker_slot(
        _cfg(admission_root="/admission", admission_limit=4),
        source="source-name",
        app_name="app-name",
        reserve_slot_fn=reserve_slot,
    )

    assert result == "slot-1"
    assert calls == [("/admission", 4, "source-name", "app-name")]


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

    # Unlabeled entries stay claimable (malformed/legacy rows are not stranded).
    unlabeled = _entry("q_bare", priority=1)
    assert worker_common.dequeue_next_across_roots(
        (orca_root, crest_root),
        list_queue_fn=lambda root: {orca_root: [unlabeled], crest_root: []}.get(root, []),
        dequeue_next_fn=lambda root: unlabeled,
        accept_entry_fn=lambda entry: getattr(entry, "app_name", "") in ("", "orca_auto_crest"),
    ) == (orca_root, unlabeled)


def test_dequeue_next_across_roots_dequeues_selected_queue_id(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    winner = _entry("winner", priority=1, enqueued_at="2026-01-01T00:00:00Z")
    queues = {
        first: [_entry("wrong-root-entry", priority=5)],
        second: [winner, _entry("same-root-later", priority=9)],
    }
    dequeued_ids: list[tuple[Path, str]] = []

    def dequeue_entry(root: Path, queue_id: str) -> SimpleNamespace | None:
        dequeued_ids.append((root, queue_id))
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
    assert dequeued_ids == [(second, "winner")]


def test_dequeue_next_across_roots_returns_none_when_selected_entry_disappears(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    def fail_dequeue_next(_root: Path) -> SimpleNamespace | None:
        pytest.fail("dequeue_next should not run with id dequeuer")

    def missing_entry(_root: Path, _queue_id: str) -> SimpleNamespace | None:
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
            reserve_slot_fn=lambda _cfg: "slot-1",
            dequeue_next_fn=lambda _cfg: (_ for _ in ()).throw(RuntimeError("queue corrupt")),
            release_slot_fn=lambda root, token: released.append((str(root), token)),
        )

    assert released == [("/tmp/admission", "slot-1")]


def test_queue_entry_by_id_scans_queue_with_injected_lister(tmp_path: Path) -> None:
    entries = [_entry("q-1"), _entry("q-2")]

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
        assert getattr(stdout, "name", None) == str(log_path)
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


def test_child_worker_command_requires_admission_root_when_included() -> None:
    with pytest.raises(ValueError, match="admission_root is required"):
        child_process_helpers.build_background_worker_command(
            config_path="/tmp/orca_auto.yaml",
            queue_root="/tmp/queue",
            queue_id="queue-1",
            worker_job_module="orca_auto.worker",
        )

    assert child_process_helpers.build_background_worker_command(
        config_path="/tmp/orca_auto.yaml",
        queue_root="/tmp/queue",
        queue_id="queue-1",
        worker_job_module="orca_auto.worker",
        include_admission_root=False,
    ) == [
        child_process_helpers.sys.executable,
        "-m",
        "orca_auto.worker",
        "--config",
        "/tmp/orca_auto.yaml",
        "--queue-root",
        "/tmp/queue",
        "--queue-id",
        "queue-1",
    ]


def test_start_background_job_process_builds_child_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    expected = object()

    def fake_start_background_process(command: list[str]) -> object:
        commands.append(list(command))
        return expected

    monkeypatch.setattr(
        child_process_helpers,
        "start_background_process",
        fake_start_background_process,
    )

    result = child_process_helpers.start_background_job_process(
        config_path="/tmp/orca_auto.yaml",
        queue_root="/tmp/queue",
        entry=SimpleNamespace(queue_id="queue-1"),
        worker_job_module="orca_auto.worker",
        admission_root="/tmp/admission",
        admission_token="slot-1",
    )

    assert result is expected
    assert commands == [
        [
            child_process_helpers.sys.executable,
            "-m",
            "orca_auto.worker",
            "--config",
            "/tmp/orca_auto.yaml",
            "--queue-root",
            "/tmp/queue",
            "--queue-id",
            "queue-1",
            "--admission-root",
            "/tmp/admission",
            "--admission-token",
            "slot-1",
        ]
    ]


def test_hooked_pidfile_child_worker_runs_engine_hooks(tmp_path: Path) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    cfg = _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission"))

    def record_started(
        worker: object,
        root: object,
        entry: object,
        process: object,
        token: object,
    ) -> bool:
        calls.append(("started", (worker, root, entry, process, token)))
        return True

    deps = SimpleNamespace(
        poll_interval_seconds=1,
        time=SimpleNamespace(sleep=lambda _seconds: None),
        admission_root=lambda _cfg: str(tmp_path / "admission"),
        release_slot=lambda _root, _token: None,
        reserve_dequeued_entry=lambda *args, **kwargs: ("idle", None),
        dequeue_next_entry=lambda _cfg: None,
        start_background_job_process=lambda **_kwargs: None,
        try_reserve_admission_slot=lambda _cfg: None,
    )

    hooks = worker_common.PidFileChildProcessQueueWorkerHooks(
        handle_worker_start_error=lambda worker, root, entry, token, exc: calls.append(
            ("start_error", (worker, root, entry, token, str(exc)))
        ),
        on_worker_process_started=record_started,
        finalize_completed_job=lambda worker, queue_id, job, rc: calls.append(
            ("finalize", (worker, queue_id, job, rc))
        ),
        shutdown_running_job=lambda worker, queue_id, job: calls.append(
            ("shutdown", (worker, queue_id, job))
        ),
        reconcile_worker_state=lambda worker: calls.append(("reconcile", (worker,))),
        before_shutdown_all=lambda worker, running_count: calls.append(
            ("before_shutdown", (worker, running_count))
        ),
    )

    worker = worker_common.HookedPidFileChildProcessQueueWorker(
        cfg,
        config_path="/tmp/config.yaml",
        max_concurrent=1,
        deps=deps,
        hooks=hooks,
        worker_pid_file_name="engine.pid",
    )
    entry = _entry("queue-1")
    root = tmp_path / "queue"
    process = SimpleNamespace(pid=1234)
    job = SimpleNamespace()

    worker._handle_worker_start_error(root, entry, "slot-1", OSError("boom"))
    assert worker._on_worker_process_started(
        root,
        entry,
        process=process,
        admission_token="slot-1",
    )
    worker._finalize_completed_job("queue-1", job, 0)
    worker._before_shutdown_all(2)
    worker._shutdown_running_job("queue-1", job)
    worker._reconcile_worker_state()

    assert worker.worker_pid_file_name == "engine.pid"
    assert [name for name, _args in calls] == [
        "start_error",
        "started",
        "finalize",
        "before_shutdown",
        "shutdown",
        "reconcile",
    ]
    assert all(args[0] is worker for _name, args in calls)


def test_shutdown_all_reaps_finished_job_before_requeuing(tmp_path: Path) -> None:
    # A child that finished during the final poll interval must be reaped through
    # the normal completion path at shutdown, not force-terminated and requeued
    # (which would needlessly re-run a completed job on the next worker start).
    cfg = _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission"))
    calls: list[tuple[str, str]] = []
    hooks = worker_common.PidFileChildProcessQueueWorkerHooks(
        handle_worker_start_error=lambda *_args: None,
        on_worker_process_started=lambda *_args: True,
        finalize_completed_job=lambda _w, queue_id, _job, _rc: calls.append(("finalize", queue_id)),
        shutdown_running_job=lambda _w, queue_id, _job: calls.append(("shutdown", queue_id)),
        reconcile_worker_state=lambda _w: None,
    )
    worker = worker_common.HookedPidFileChildProcessQueueWorker(
        cfg,
        config_path="/tmp/config.yaml",
        max_concurrent=2,
        deps=SimpleNamespace(
            poll_interval_seconds=1,
            time=SimpleNamespace(sleep=lambda _seconds: None),
            admission_root=lambda _cfg: str(tmp_path / "admission"),
            release_slot=lambda _root, _token: None,
        ),
        hooks=hooks,
    )
    finished = SimpleNamespace(process=SimpleNamespace(poll=lambda: 0))
    still_running = SimpleNamespace(process=SimpleNamespace(poll=lambda: None))
    worker._running = {"done": finished, "busy": still_running}

    worker._shutdown_all()

    # The finished job is finalized (completed), never requeued via shutdown.
    assert ("finalize", "done") in calls
    assert ("shutdown", "done") not in calls
    # The still-running job is shut down (terminated + requeued) as before.
    assert ("shutdown", "busy") in calls
    assert worker._running == {}


def test_pidfile_child_worker_run_once_returns_error_when_singleton_lock_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission"))
    deps = SimpleNamespace(
        poll_interval_seconds=1,
        time=SimpleNamespace(sleep=lambda _seconds: None),
        admission_root=lambda _cfg: str(tmp_path / "admission"),
        release_slot=lambda _root, _token: None,
        reserve_dequeued_entry=lambda *args, **kwargs: ("idle", None),
        dequeue_next_entry=lambda _cfg: None,
        start_background_job_process=lambda **_kwargs: None,
        try_reserve_admission_slot=lambda _cfg: None,
    )
    hooks = worker_common.PidFileChildProcessQueueWorkerHooks(
        handle_worker_start_error=lambda *_args: None,
        on_worker_process_started=lambda *_args: True,
        finalize_completed_job=lambda *_args: None,
        shutdown_running_job=lambda *_args: None,
        reconcile_worker_state=lambda _worker: None,
    )
    worker = worker_common.HookedPidFileChildProcessQueueWorker(
        cfg,
        config_path="/tmp/config.yaml",
        max_concurrent=1,
        deps=deps,
        hooks=hooks,
        worker_pid_file_name="engine.pid",
    )
    lock_calls: list[tuple[Path, float]] = []

    def locked_file_lock(path: Path, *, timeout_seconds: float) -> object:
        lock_calls.append((path, timeout_seconds))
        raise TimeoutError("held")

    monkeypatch.setattr(worker_process_helpers, "file_lock", locked_file_lock)

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

    class FakeProcess:
        def terminate(self) -> None:
            nonlocal terminate_calls
            terminate_calls += 1

    process = FakeProcess()
    deps = SimpleNamespace(
        poll_interval_seconds=1,
        time=SimpleNamespace(sleep=lambda _seconds: None),
        admission_root=lambda _cfg: str(tmp_path / "admission"),
        release_slot=lambda _root, _token: None,
        reserve_dequeued_entry=lambda *args, **kwargs: ("idle", None),
        dequeue_next_entry=lambda _cfg: None,
        start_background_job_process=lambda **_kwargs: process,
        try_reserve_admission_slot=lambda _cfg: None,
    )
    worker = RejectingWorker(
        _cfg(allowed_root=str(tmp_path), admission_root=str(tmp_path / "admission")),
        config_path="/tmp/config.yaml",
        max_concurrent=1,
        deps=deps,
    )
    entry = _entry("queue-reject")

    assert not worker._start_job(tmp_path / "queue", entry, admission_token="slot-1")
    assert terminate_calls == 1
    assert start_errors == [(tmp_path / "queue", "slot-1", "worker attach rejected")]
    assert worker._running == {}


def test_fill_worker_slots_starts_until_capacity_and_reports_processed() -> None:
    running: list[str] = []
    reservations = iter(
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
    reservations = iter(
        [
            ("processed", "slot-1"),
            ("processed", "slot-2"),
        ]
    )
    reserve_calls = 0

    def reserve_next() -> tuple[str, str | None]:
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
    )

    assert killpg_calls == [(123, 15), (123, 9)]
    assert proc.terminate_calls == 1
    assert proc.kill_calls == 1
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
    )

    assert killpg_calls == [(124, 15), (124, 9)]
    assert proc.wait_calls == [1, 2]


def test_shutdown_running_process_job_keeps_running_state_when_process_survives(
    tmp_path: Path,
) -> None:
    worker = SimpleNamespace(
        _release_admission_slot=lambda token: released.append(token),
    )
    job = SimpleNamespace(
        process=object(),
        queue_root=tmp_path / "queue",
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    released: list[str] = []

    lifecycle_helpers.shutdown_running_process_job(
        worker,
        "queue-1",
        job,
        hooks=lifecycle_helpers.EngineQueueProcessShutdownHooks(
            terminate_process_fn=lambda _process: False,
            requeue_running_entry_fn=lambda root, queue_id: requeued.append((root, queue_id)),
        ),
    )

    assert requeued == []
    assert released == []


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


def test_pid_helpers_handle_alive_missing_and_stale_pids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert worker_common.pid_is_alive(0) is False
    monkeypatch.setattr(process_helpers.os, "kill", lambda _pid, _signal: None)
    assert worker_common.pid_is_alive(123) is True

    pid_path = tmp_path / "worker.pid"
    pid_path.write_text("123\n", encoding="utf-8")
    assert process_helpers.read_live_pid_file(pid_path) == 123

    json_pid_path = tmp_path / "json-worker.pid"
    json_pid_path.write_text(json.dumps({"pid": 123, "process_start_ticks": 111}), encoding="utf-8")
    monkeypatch.setattr(process_helpers, "_process_start_ticks", lambda _pid: 111)
    assert process_helpers.read_live_pid_file(json_pid_path) == 123

    reused_pid_path = tmp_path / "reused-worker.pid"
    reused_pid_path.write_text(
        json.dumps({"pid": 123, "process_start_ticks": 111}), encoding="utf-8"
    )
    monkeypatch.setattr(process_helpers, "_process_start_ticks", lambda _pid: 222)
    assert process_helpers.read_live_pid_file(reused_pid_path) is None
    assert not reused_pid_path.exists()

    monkeypatch.setattr(
        process_helpers.os,
        "kill",
        lambda _pid, _signal: (_ for _ in ()).throw(OSError()),
    )
    assert worker_common.pid_is_alive(123) is False
    assert process_helpers.read_live_pid_file(pid_path) is None
    assert not pid_path.exists()

    missing = tmp_path / "missing.pid"
    assert process_helpers.read_live_pid_file(missing) is None

    invalid = tmp_path / "invalid.pid"
    invalid.write_text("not-a-pid\n", encoding="utf-8")
    assert process_helpers.read_live_pid_file(invalid) is None


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
        mark_cancelled_fn=lambda root, queue_id, *, error: cancelled.append(
            (str(root), queue_id, error)
        ),
        requeue_running_entry_fn=lambda root, queue_id: requeued.append((str(root), queue_id)),
        mark_recovery_pending_fn=lambda _cfg, entry: recovery_pending.append(entry.queue_id),
    )

    assert stale_reconciled == ["/tmp/admission"]
    assert cancelled == [(str(queue_root), "cancelled", "cancel_requested")]
    assert requeued == [(str(queue_root), "orphaned")]
    assert recovery_pending == ["orphaned"]


def test_finalize_child_exit_with_policy_preserves_root_and_uses_recovery_entry(
    tmp_path: Path,
) -> None:
    cfg = object()
    current = _entry("current", status="running")
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=_entry("job-entry", status="running"),
        admission_token="slot-1",
    )
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[object, str, str]] = []
    released: list[str] = []

    lifecycle_helpers.finalize_child_exit_with_policy(
        cfg,
        job,
        policy=lifecycle_helpers.ChildExitPolicy(
            recovery_entry_fn=lambda _current, current_job: current_job.entry,
        ),
        find_queue_entry_fn=lambda _root, _queue_id: current,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=lambda root, queue_id: requeued.append((root, queue_id)),
        mark_recovery_pending_fn=lambda cfg_obj, entry, *, reason: recovery.append(
            (cfg_obj, entry.queue_id, reason)
        ),
        release_admission_slot_fn=released.append,
    )

    assert requeued == [(tmp_path / "queue", "current")]
    assert recovery == [(cfg, "job-entry", "worker_shutdown")]
    assert released == ["slot-1"]


def test_finalize_process_finished_job_skips_terminal_mark_when_entry_was_requeued(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    current = _entry("q-1", status="pending")
    completed: list[str] = []
    failed: list[str] = []
    side_effects: list[str] = []
    released: list[str] = []
    worker = SimpleNamespace(
        cfg=object(),
        allowed_root=queue_root,
        admission_root=tmp_path / "admission",
        _release_admission_slot=released.append,
    )
    job = SimpleNamespace(
        queue_root=queue_root,
        reaction_dir=str(tmp_path / "rxn"),
        admission_token="slot-1",
    )

    lifecycle_helpers.finalize_process_finished_job(
        worker,
        "q-1",
        job,
        rc=0,
        hooks=lifecycle_helpers.EngineQueueProcessLifecycleHooks(
            queue_entry_id_fn=lambda entry: entry.queue_id,
            queue_entry_app_name_fn=lambda _entry: "app",
            queue_entry_task_id_fn=lambda _entry: "task",
            update_slot_metadata_fn=lambda *_args, **_kwargs: None,
            terminate_process_fn=lambda _proc: None,
            mark_failed_fn=lambda _root, queue_id, **_kwargs: failed.append(queue_id),
            upsert_running_job_record_fn=lambda *_args, **_kwargs: None,
            get_run_id_from_state_fn=lambda _reaction_dir: "run-1",
            get_cancel_requested_fn=lambda *_args, **_kwargs: False,
            mark_cancelled_fn=lambda *_args, **_kwargs: None,
            mark_completed_fn=lambda _root, queue_id, **_kwargs: completed.append(queue_id),
            upsert_terminal_job_record_fn=lambda *_args, **_kwargs: side_effects.append("record"),
            notify_terminal_job_from_state_fn=lambda *_args, **_kwargs: True,
            find_queue_entry_fn=lambda _root, _queue_id: current,
            on_completed_fn=lambda *_args, **_kwargs: side_effects.append("completed"),
        ),
    )

    assert completed == []
    assert failed == []
    assert side_effects == []
    assert released == ["slot-1"]


def test_finalize_process_finished_job_keeps_slot_when_terminal_mark_raises(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    current = _entry("q-1", status="running")
    released: list[str] = []
    worker = SimpleNamespace(
        cfg=object(),
        allowed_root=queue_root,
        admission_root=tmp_path / "admission",
        _release_admission_slot=released.append,
    )
    job = SimpleNamespace(
        queue_root=queue_root,
        reaction_dir=str(tmp_path / "rxn"),
        admission_token="slot-1",
    )

    with pytest.raises(RuntimeError, match="queue write failed"):
        lifecycle_helpers.finalize_process_finished_job(
            worker,
            "q-1",
            job,
            rc=0,
            hooks=lifecycle_helpers.EngineQueueProcessLifecycleHooks(
                queue_entry_id_fn=lambda entry: entry.queue_id,
                queue_entry_app_name_fn=lambda _entry: "app",
                queue_entry_task_id_fn=lambda _entry: "task",
                update_slot_metadata_fn=lambda *_args, **_kwargs: None,
                terminate_process_fn=lambda _proc: None,
                mark_failed_fn=lambda *_args, **_kwargs: None,
                upsert_running_job_record_fn=lambda *_args, **_kwargs: None,
                get_run_id_from_state_fn=lambda _reaction_dir: "run-1",
                get_cancel_requested_fn=lambda *_args, **_kwargs: False,
                mark_cancelled_fn=lambda *_args, **_kwargs: None,
                mark_completed_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("queue write failed")
                ),
                upsert_terminal_job_record_fn=lambda *_args, **_kwargs: None,
                notify_terminal_job_from_state_fn=lambda *_args, **_kwargs: True,
                find_queue_entry_fn=lambda _root, _queue_id: current,
            ),
        )

    assert released == []


def test_finalize_process_finished_job_releases_slot_when_completed_side_effect_raises(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue_root = tmp_path / "queue"
    current = _entry("q-1", status="running")
    completed: list[str] = []
    side_effects: list[str] = []
    released: list[str] = []
    worker = SimpleNamespace(
        cfg=object(),
        allowed_root=queue_root,
        admission_root=tmp_path / "admission",
        _release_admission_slot=released.append,
    )
    job = SimpleNamespace(
        queue_root=queue_root,
        reaction_dir=str(tmp_path / "rxn"),
        admission_token="slot-1",
    )

    with caplog.at_level(logging.WARNING):
        lifecycle_helpers.finalize_process_finished_job(
            worker,
            "q-1",
            job,
            rc=0,
            hooks=lifecycle_helpers.EngineQueueProcessLifecycleHooks(
                queue_entry_id_fn=lambda entry: entry.queue_id,
                queue_entry_app_name_fn=lambda _entry: "app",
                queue_entry_task_id_fn=lambda _entry: "task",
                update_slot_metadata_fn=lambda *_args, **_kwargs: None,
                terminate_process_fn=lambda _proc: None,
                mark_failed_fn=lambda *_args, **_kwargs: None,
                upsert_running_job_record_fn=lambda *_args, **_kwargs: None,
                get_run_id_from_state_fn=lambda _reaction_dir: "run-1",
                get_cancel_requested_fn=lambda *_args, **_kwargs: False,
                mark_cancelled_fn=lambda *_args, **_kwargs: None,
                mark_completed_fn=lambda _root, queue_id, **_kwargs: completed.append(queue_id),
                upsert_terminal_job_record_fn=lambda *_args, **_kwargs: side_effects.append(
                    "record"
                ),
                notify_terminal_job_from_state_fn=lambda *_args, **_kwargs: True,
                find_queue_entry_fn=lambda _root, _queue_id: current,
                on_completed_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("organize failed")
                ),
            ),
        )

    assert completed == ["q-1"]
    assert side_effects == ["record"]
    assert released == ["slot-1"]
    assert any("completed-job side effect" in record.getMessage() for record in caplog.records)


def test_reconcile_orphaned_running_with_policy_preserves_roots_and_reason(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    entry = _entry("orphan", status="running")
    requeued: list[tuple[Path, str]] = []
    recovery: list[tuple[str, str]] = []

    lifecycle_helpers.reconcile_orphaned_running_with_policy(
        _cfg(),
        policy=lifecycle_helpers.OrphanedRunningPolicy(
            recovery_reason="custom_recovery",
        ),
        admission_root="/tmp/admission",
        queue_roots_fn=lambda _cfg: (queue_root,),
        list_queue_fn=lambda _root: [entry],
        list_slots_fn=lambda _root: [],
        reconcile_stale_slots_fn=lambda _root: None,
        mark_cancelled_fn=lambda *args, **kwargs: None,
        requeue_running_entry_fn=lambda root, queue_id: requeued.append((root, queue_id)),
        mark_recovery_pending_fn=lambda _cfg, current, *, reason: recovery.append(
            (current.queue_id, reason)
        ),
    )

    assert requeued == [(queue_root, "orphan")]
    assert recovery == [("orphan", "custom_recovery")]


def test_reconcile_orphaned_process_entries_passes_policy_kwargs(tmp_path: Path) -> None:
    queue_root = tmp_path / "queue"
    calls: list[tuple[str, object]] = []
    worker = SimpleNamespace(
        cfg=object(),
        admission_root="/tmp/admission",
    )

    lifecycle_helpers.reconcile_orphaned_process_entries(
        worker,
        hooks=lifecycle_helpers.EngineQueueProcessReconcileHooks(
            queue_roots_fn=lambda _cfg: (queue_root,),
            reconcile_stale_slots_fn=lambda admission_root: calls.append(("stale", admission_root)),
            reconcile_orphaned_running_entries_fn=lambda root, **kwargs: calls.append(
                ("orphans", (root, kwargs))
            ),
            reconcile_orphaned_running_entries_kwargs={"ignore_worker_pid": True},
        ),
    )

    assert calls == [
        ("stale", "/tmp/admission"),
        ("orphans", (queue_root, {"ignore_worker_pid": True})),
    ]


def test_run_terminal_process_side_effects_uses_standard_hooks() -> None:
    cfg = object()
    job = SimpleNamespace(reaction_dir="/tmp/job", task_id="task-1")
    worker = SimpleNamespace(cfg=cfg)
    calls: list[tuple[str, object, object]] = []

    def notify_terminal_job_from_state(cfg_obj: object, reaction_dir: str) -> bool:
        calls.append(("notify", cfg_obj, reaction_dir))
        return True

    lifecycle_helpers.run_terminal_process_side_effects(
        worker,
        "queue-1",
        job,
        hooks=lifecycle_helpers.EngineQueueTerminalSideEffectHooks(
            upsert_terminal_job_record_fn=lambda cfg_obj, reaction_dir, **kwargs: calls.append(
                ("upsert", cfg_obj, (reaction_dir, kwargs))
            ),
            notify_terminal_job_from_state_fn=notify_terminal_job_from_state,
        ),
    )

    assert calls == [
        ("upsert", cfg, ("/tmp/job", {"fallback_job_id": "task-1"})),
        ("notify", cfg, "/tmp/job"),
    ]


def test_shutdown_child_process_with_grace_forces_after_deadline(
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

    def force_terminate(proc: Process) -> None:
        calls.append("force")
        proc.rc = 9

    child_process_helpers.shutdown_child_process_with_grace(
        job,
        terminate_process_fn=force_terminate,
        finalize_child_exit_fn=lambda job_arg, rc: finalized.append((job_arg, rc)),
        grace_seconds=0.1,
        sleep_fn=lambda seconds: calls.append(f"sleep:{seconds}"),
    )

    assert calls == ["terminate", "sleep:0.1", "force"]
    assert finalized == [(job, 9)]


def test_shutdown_child_process_with_grace_skips_finalize_when_force_fails(
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

    def force_terminate(_proc: Process) -> bool:
        calls.append("force")
        return False

    child_process_helpers.shutdown_child_process_with_grace(
        job,
        terminate_process_fn=force_terminate,
        finalize_child_exit_fn=lambda _job, rc: finalized.append(rc),
        grace_seconds=0.1,
        sleep_fn=lambda seconds: calls.append(f"sleep:{seconds}"),
    )

    assert calls == ["terminate", "force"]
    assert finalized == []


def test_shutdown_child_process_with_grace_continues_when_terminate_raises(
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

    child_process_helpers.shutdown_child_process_with_grace(
        job,
        terminate_process_fn=lambda proc: setattr(proc, "rc", 9),
        finalize_child_exit_fn=lambda _job, rc: finalized.append(rc),
        grace_seconds=0.0,
        sleep_fn=lambda _seconds: pytest.fail("sleep should not run after deadline"),
    )

    assert finalized == [9]


def test_request_job_cancellation_uses_signal_then_kill_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent_signals: list[int] = []
    child_process_helpers.request_job_cancellation(
        SimpleNamespace(send_signal=lambda signal_value: sent_signals.append(signal_value)),
        cancel_signal=15,
        terminate_process_fn=lambda _proc: pytest.fail("send_signal should be enough"),
    )
    assert sent_signals == [15]

    proc = FakeManagedProcess(pid=4242)
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        child_process_helpers.os,
        "killpg",
        lambda pid, signal_value: killpg_calls.append((pid, signal_value)),
    )

    child_process_helpers.request_job_cancellation(
        proc,
        cancel_signal=15,
        terminate_process_fn=lambda _proc: pytest.fail("killpg should be enough"),
    )

    assert killpg_calls == [(4242, 15)]
    assert proc.sent_signals == []

    killed: list[tuple[int, int]] = []
    terminated: list[int] = []

    monkeypatch.setattr(
        child_process_helpers.os,
        "killpg",
        lambda _pid, _signal_value: (_ for _ in ()).throw(
            ProcessLookupError("missing process group")
        ),
    )

    def fake_kill(pid: int, signal_value: int) -> None:
        killed.append((pid, signal_value))
        raise ProcessLookupError("missing process")

    monkeypatch.setattr(child_process_helpers.os, "kill", fake_kill)

    child_process_helpers.request_job_cancellation(
        SimpleNamespace(pid=4242),
        cancel_signal=2,
        terminate_process_fn=lambda proc: terminated.append(proc.pid),
    )

    assert killed == [(4242, 2)]
    assert terminated == [4242]
