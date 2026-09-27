"""``orca_auto.orca.queue.worker``: construction, pid file, signals, run loop and startup."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto import systemd_plan
from orca_auto.core.admission import reserve_slot
from orca_auto.core.admission import store as admission_store
from orca_auto.core.config.schema import SchedulerConfig
from orca_auto.core.queue.processes import (
    KILL_TIMEOUT_SECONDS,
    ManagedProcess,
    worker_shutdown_budget_seconds,
)
from orca_auto.core.queue.store import QueueLockTimeoutError
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.models import ReservedQueueEntry
from orca_auto.orca.config import AppConfig, load_config, load_orca_shared_config
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue.adapter import enqueue
from orca_auto.orca.queue.models import OrcaRunningJob
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import make_app_cfg, write_config_file
from tests.process_helpers import FakeManagedProcess
from tests.queue_worker_helpers import (
    WORKER_LOGGER,
    ChildStarter,
    FakeChildren,
    current_orca_queue_metadata,
    queue_statuses,
    running_job,
)

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_default_init(tmp_path: Path) -> None:
    worker = OrcaQueueWorker(make_app_cfg(tmp_path), str(tmp_path / "config.yaml"))
    assert worker.max_concurrent == SchedulerConfig.max_active_simulations
    assert worker.admission_root == tmp_path.resolve() / ".admission"
    assert not worker._shutdown_requested
    assert len(worker._running) == 0


@pytest.mark.parametrize("max_active", [None, 1, 3])
def test_worker_roots_and_limit_match_the_rendered_unit(
    tmp_path: Path, fake_orca: Path, max_active: int | None
) -> None:
    runs = tmp_path / "runs"
    cfg = make_app_cfg(runs, orca_executable=fake_orca, max_concurrent=max_active)
    config = write_config_file(tmp_path / "orca_auto.yaml", cfg)

    worker = OrcaQueueWorker(load_config(str(config)), str(config))
    _path, shared, _orca_sections = load_orca_shared_config(config)

    assert systemd_plan._configured_read_write_paths(shared) == (worker.queue_root,)
    assert worker.admission_root == worker.queue_root / ".admission"
    assert worker.max_concurrent == (max_active or SchedulerConfig.max_active_simulations)
    budget = worker_shutdown_budget_seconds(worker.max_concurrent)
    assert systemd_plan._configured_stop_timeout_seconds(shared) == (
        math.ceil(budget + KILL_TIMEOUT_SECONDS) + 1
    )


# ---------------------------------------------------------------------------
# PID file and signal handlers
# ---------------------------------------------------------------------------


def test_pid_file_write_and_remove(worker: OrcaQueueWorker) -> None:
    worker._write_pid_file()
    pid_path = worker._pid_file_path()
    assert pid_path.exists()
    payload = json.loads(pid_path.read_text(encoding="utf-8"))
    assert isinstance(payload.get("pid"), int)
    worker._remove_pid_file()
    assert not pid_path.exists()


def test_remove_pid_file_missing(worker: OrcaQueueWorker) -> None:
    pid_path = worker._pid_file_path()
    assert not pid_path.exists()

    worker._remove_pid_file()

    # A missing pid file is not an error and nothing is created in its place.
    assert not pid_path.exists()
    assert sorted(p.name for p in pid_path.parent.iterdir()) == []


def test_pid_file_path(worker: OrcaQueueWorker, queue_root: Path) -> None:
    path = worker._pid_file_path()
    assert path.name == "queue_worker.pid"
    assert path.parent == queue_root.resolve()


def test_install_signal_handlers(worker: OrcaQueueWorker) -> None:
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    worker._install_signal_handlers()

    installed = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    assert all(installed[sig] is not before[sig] for sig in installed)
    handler = installed[signal.SIGTERM]
    assert callable(handler)
    assert not worker._shutdown_requested
    handler(signal.SIGTERM, None)
    assert worker._shutdown_requested


def test_run_keyboard_interrupt(make_worker: Callable[..., OrcaQueueWorker]) -> None:
    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    worker = make_worker(sleep=interrupt)

    assert worker.run() == 0

    assert all(signal.getsignal(sig) is not before[sig] for sig in before)
    # PID file should be cleaned up
    assert not worker._pid_file_path().exists()


def test_run_shutdown_flag(make_worker: Callable[..., OrcaQueueWorker]) -> None:
    workers: list[OrcaQueueWorker] = []

    def request_stop(_seconds: float) -> None:
        workers[0]._shutdown_requested = True

    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    workers.append(make_worker(sleep=request_stop))

    assert workers[0].run() == 0

    assert all(signal.getsignal(sig) is not before[sig] for sig in before)


def test_run_keeps_supervising_a_running_child_after_a_failed_periodic_reconcile(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The periodic reconcile in the poll sleep meets a queue lock held past its
    # deadline. Ending the loop would run the shutdown sweep, which stops the
    # running child and restarts its calculation from scratch.
    monkeypatch.setattr(queue_worker_mod, "_WORKER_STATE_RECONCILE_INTERVAL_SECONDS", 0.0)
    workers: list[OrcaQueueWorker] = []
    reconciles: list[str] = []
    sleeps: list[float] = []
    while_retrying: list[tuple[list[str], dict[str, QueueStatus], list[tuple[int, int]]]] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            running = workers[0]._running
            while_retrying.append(
                (list(running), queue_statuses(queue_root), list(fake_children.signals))
            )
        else:
            workers[0]._shutdown_requested = True

    worker = make_worker(start=child_starter, sleep=sleep)
    workers.append(worker)
    real_reconcile = worker._reconcile_worker_state

    def reconcile() -> None:
        reconciles.append("reconcile")
        if len(reconciles) == 2:
            raise QueueLockTimeoutError("queue lock held past its deadline")
        real_reconcile()

    monkeypatch.setattr(worker, "_reconcile_worker_state", reconcile)
    rxn = queue_root / "mol_long_run"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))

    with caplog.at_level(logging.ERROR):
        assert worker.run() == 0

    # Startup, the failed poll-sleep reconcile, then the next pass's reconcile:
    # the retry sleep does not re-enter the failing reconcile.
    assert reconciles == ["reconcile"] * 3
    assert sleeps == [queue_worker_mod.POLL_INTERVAL_SECONDS] * 2
    assert while_retrying == [([entry.queue_id], {entry.queue_id: QueueStatus.RUNNING}, [])]
    [error] = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert error.exc_info is not None
    assert isinstance(error.exc_info[1], QueueLockTimeoutError)
    # Only the requested shutdown stops the child and requeues its row.
    [started] = child_starter.started
    assert fake_children.stopped(started.process)
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def _fail_first_call(call: Callable[..., Any], error: Exception) -> Callable[..., Any]:
    calls: list[None] = []

    def fail_first(*args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        if len(calls) == 1:
            raise error
        return call(*args, **kwargs)

    return fail_first


@pytest.mark.parametrize("failing_step", ["attach", "dequeue"])
def test_run_reclaims_the_slot_a_failed_admission_pass_could_not_release(
    failing_step: str,
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The pass fails after reserving a slot, and releasing that slot times out
    # on the admission lock as well. The slot's owner is this live worker, so
    # the dead-owner reconcile keeps it; with one admission slot no job would
    # start again until the worker exits.
    monkeypatch.setattr(queue_worker_mod, "_WORKER_STATE_RECONCILE_INTERVAL_SECONDS", 0.0)
    workers: list[OrcaQueueWorker] = []
    leaked_tokens: list[str] = []
    seen: list[tuple[list[tuple[str, int, str]], dict[str, QueueStatus], list[str]]] = []

    def sleep(_seconds: float) -> None:
        slots = admission_store.list_all_slots(workers[0].admission_root)
        if not seen:
            leaked_tokens.extend(slot.token for slot in slots)
        seen.append(
            (
                [(slot.state, slot.owner_pid, slot.queue_id) for slot in slots],
                queue_statuses(queue_root),
                list(workers[0]._running),
            )
        )
        if len(seen) == 3:
            workers[0]._shutdown_requested = True

    worker = make_worker(max_concurrent=1, start=child_starter, sleep=sleep)
    workers.append(worker)
    assert worker.max_concurrent == 1
    admission_lock_held = TimeoutError("admission lock held past its deadline")
    if failing_step == "attach":
        monkeypatch.setattr(
            queue_worker_mod,
            "update_slot_metadata",
            _fail_first_call(queue_worker_mod.update_slot_metadata, admission_lock_held),
        )
    else:
        monkeypatch.setattr(
            queue_worker_mod.roots,
            "dequeue_next_entry",
            _fail_first_call(
                queue_worker_mod.roots.dequeue_next_entry,
                QueueLockTimeoutError("queue lock held past its deadline"),
            ),
        )
    monkeypatch.setattr(
        worker,
        "_release_admission_slot",
        _fail_first_call(worker._release_admission_slot, admission_lock_held),
    )
    entries: list[QueueEntry] = []
    for name in ("mol_first", "mol_second"):
        rxn = queue_root / name
        rxn.mkdir()
        entries.append(enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn)))
    first, second = entries

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        assert worker.run() == 0

    # A failed start fails its row; a failed dequeue leaves it pending.
    failed_start = failing_step == "attach"
    after_failure = {
        first.queue_id: QueueStatus.FAILED if failed_start else QueueStatus.PENDING,
        second.queue_id: QueueStatus.PENDING,
    }
    assert seen[:2] == [
        ([("reserved", os.getpid(), "")], after_failure, []),
        # The next periodic reconcile releases the slot the worker never attached.
        ([], after_failure, []),
    ]
    admitted = second if failed_start else first
    started = child_starter.started[-1]
    assert started.entry.queue_id == admitted.queue_id
    assert seen[2] == (
        [("active", started.process.pid, admitted.queue_id)],
        {**after_failure, admitted.queue_id: QueueStatus.RUNNING},
        [admitted.queue_id],
    )
    released = [
        record for record in caplog.records if record.getMessage().startswith("Released admission")
    ]
    assert [(record.levelno, record.getMessage()) for record in released] == [
        (
            logging.WARNING,
            f"Released admission slot {leaked_tokens[0]} that this worker reserved "
            "but never attached to a job",
        )
    ]


def test_reconcile_releases_only_this_workers_unattached_reservations(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
) -> None:
    # Another live owner's reservation keeps its capacity, and a slot tied to a
    # queue row keeps protecting that row.
    root = worker.admission_root
    leaked = reserve_slot(root, 3, source="queue_worker", state="reserved")
    foreign = reserve_slot(
        root, 3, source="queue_worker", state="reserved", owner_pid=sleeping_child().pid
    )
    tied = reserve_slot(root, 3, source="queue_worker", state="reserved", queue_id="q-tied")
    assert leaked and foreign and tied

    worker._reconcile_worker_state()

    assert [slot.token for slot in admission_store.list_all_slots(root)] == [foreign, tied]


# ---------------------------------------------------------------------------
# Worker lifecycle: singleton lock, startup failure, reconciliation cadence,
# child start and shutdown sweep (ported from the former generic base tests)
# ---------------------------------------------------------------------------


class _RecordingWorker(OrcaQueueWorker):
    """Records finalize/shutdown per job; the child seams stay inert."""

    def __init__(
        self, cfg: AppConfig, calls: list[tuple[str, str]], *, fail_finalize: bool = False
    ) -> None:
        super().__init__(
            replace(cfg, runtime=replace(cfg.runtime, max_concurrent=2)),
            "/tmp/config.yaml",
            sleep_fn=lambda _seconds: None,
        )
        self.calls = calls
        self.fail_finalize = fail_finalize

    def _finalize_completed_job(self, queue_id: str, job: OrcaRunningJob, rc: int) -> None:
        self.calls.append(("finalize", queue_id))
        if self.fail_finalize:
            raise RuntimeError("finalize failed once")

    def _shutdown_running_job(self, queue_id: str, job: OrcaRunningJob) -> None:
        self.calls.append(("shutdown", queue_id))


def _plain_entry(queue_id: str) -> QueueEntry:
    return QueueEntry(queue_id, "orca_auto_orca", "task-1", "orca_run_inp", "orca")


def test_start_reserved_finalizes_snapshot_intent_before_start(
    worker: OrcaQueueWorker, queue_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconciled: list[Path] = []
    events: list[str] = []

    def reconcile_snapshots(root: Path) -> int:
        reconciled.append(root)
        return 0

    def start_job(*_args: Any, **_kwargs: Any) -> bool:
        events.append("start")
        return True

    monkeypatch.setattr(
        queue_worker_mod, "reconcile_orphaned_snapshot_generations", reconcile_snapshots
    )
    monkeypatch.setattr(
        queue_worker_mod, "retire_snapshot_intent_for_row", lambda *_args: events.append("intent")
    )
    monkeypatch.setattr(worker, "_start_job", start_job)

    assert worker._start_reserved(ReservedQueueEntry(queue_root, _plain_entry("q-1"), "slot"))
    assert events == ["intent", "start"]
    # Abandoned pre-enqueue snapshots are swept on the first reconcile only,
    # then at most once per interval.
    worker._reconcile_worker_state()
    worker._reconcile_worker_state()
    assert reconciled == [queue_root]


def test_idle_state_reconciliation_is_throttled(
    worker_cfg: AppConfig, queue_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    reconcile_calls: list[float] = []
    sleep_calls: list[float] = []

    class _Worker(OrcaQueueWorker):
        def _reconcile_worker_state(self) -> None:
            reconcile_calls.append(now[0])

    monkeypatch.setattr(queue_worker_mod.time, "monotonic", lambda: now[0])
    worker = _Worker(worker_cfg, str(queue_root / "config.yaml"), sleep_fn=sleep_calls.append)
    monkeypatch.setattr(worker, "_write_pid_file", lambda: None)

    worker._before_run()
    worker._sleep()
    now[0] = 59.0
    worker._sleep()
    now[0] = 60.0
    worker._sleep()

    assert reconcile_calls == [0.0, 60.0]
    assert sleep_calls == [5.0, 5.0, 5.0]


def test_shutdown_all_reaps_finished_job_before_requeuing(
    worker_cfg: AppConfig, queue_root: Path
) -> None:
    # A child that finished during the final poll interval must be reaped through
    # the normal completion path at shutdown, not force-terminated and requeued
    # (which would needlessly re-run a completed job on the next worker start).
    calls: list[tuple[str, str]] = []
    worker = _RecordingWorker(worker_cfg, calls)
    finished = running_job(
        worker, _plain_entry("done"), queue_root, FakeManagedProcess(poll_result=0), "slot-done"
    )
    still_running = running_job(
        worker, _plain_entry("busy"), queue_root, FakeManagedProcess(), "slot-busy"
    )
    worker._running = {"done": finished, "busy": still_running}

    worker._shutdown_all()

    assert ("finalize", "done") in calls
    assert ("shutdown", "done") not in calls
    assert ("shutdown", "busy") in calls
    assert worker._running == {}


def test_shutdown_all_does_not_requeue_exited_job_after_finalize_failure(
    worker_cfg: AppConfig, queue_root: Path
) -> None:
    calls: list[tuple[str, str]] = []
    worker = _RecordingWorker(worker_cfg, calls, fail_finalize=True)
    finished = running_job(
        worker, _plain_entry("done"), queue_root, FakeManagedProcess(poll_result=0), "slot-done"
    )
    still_running = running_job(
        worker, _plain_entry("busy"), queue_root, FakeManagedProcess(), "slot-busy"
    )
    worker._running = {"done": finished, "busy": still_running}

    worker._shutdown_all()

    assert ("finalize", "done") in calls
    assert ("shutdown", "done") not in calls
    assert ("shutdown", "busy") in calls
    assert worker._running == {"done": finished}


def test_run_once_returns_error_when_singleton_lock_held(
    worker: OrcaQueueWorker,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_calls: list[tuple[Path, float]] = []

    def locked_file_lock(path: Path, *, timeout_seconds: float) -> object:
        lock_calls.append((path, timeout_seconds))
        raise TimeoutError("held")

    monkeypatch.setattr(queue_worker_mod, "file_lock", locked_file_lock)

    assert worker.run_once(idle_message=None, blocked_message=None) == 1

    assert "queue worker already running" in capsys.readouterr().err
    assert lock_calls == [(queue_root.resolve() / "queue_worker.pid.lock", 0.0)]
    assert not worker._pid_file_path().exists()


def test_run_reports_startup_failure_and_removes_pid_file(
    worker_cfg: AppConfig,
    queue_root: Path,
    preserved_signals: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del preserved_signals

    class _StartupFailingWorker(OrcaQueueWorker):
        def _reconcile_worker_state(self) -> None:
            raise TimeoutError("admission lock held by service restart")

    worker = _StartupFailingWorker(worker_cfg, str(queue_root / "config.yaml"))

    with caplog.at_level(logging.ERROR):
        rc = worker.run()

    assert rc == 1
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert [record.getMessage() for record in errors] == [
        "Queue worker startup failed: admission lock held by service restart"
    ]
    assert errors[0].exc_info is None
    assert not worker._pid_file_path().exists()


def test_rejected_attach_terminates_child_and_reports_start_error(
    worker_cfg: AppConfig, queue_root: Path, fake_children: FakeChildren
) -> None:
    start_errors: list[tuple[Path, str, str]] = []
    process = fake_children.spawn()

    class RejectingWorker(OrcaQueueWorker):
        def _start_background_process(self, **_kwargs: Any) -> ManagedProcess:
            return process

        def _on_worker_process_started(self, *_args: Any, **_kwargs: Any) -> bool:
            return False

        def _handle_worker_start_error(
            self, queue_root: Path, entry: QueueEntry, admission_token: str, exc: OSError
        ) -> None:
            start_errors.append((queue_root, admission_token, str(exc)))

    worker = RejectingWorker(
        replace(worker_cfg, runtime=replace(worker_cfg.runtime, max_concurrent=1)),
        str(queue_root / "config.yaml"),
    )

    assert not worker._start_job(queue_root, _plain_entry("queue-reject"), admission_token="slot-1")
    assert fake_children.stopped(process)
    assert start_errors == [(queue_root, "slot-1", "admission_slot_missing")]
    assert worker._running == {}
