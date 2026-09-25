"""Behaviour of ``orca_auto.orca.queue.worker``: admission, finalization, cancellation, shutdown.

Every test drives the worker against real queue, admission and run-state
files and reads the outcome back from them. Children are fakes reached only
through the two OS seams a stop goes through (``os.killpg`` and the pid probe),
or real ``sleep`` processes when the exit path itself is under test. Faults are
injected as states the worker can meet in production: a held ``run.lock`` (an
ORCA instance still owns the directory), a read-only queue root, an unreadable
job index, an engine launch pending under a live owner.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import BoundedSemaphore, Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from orca_auto.core.admission import (
    active_slot_count,
    get_slot,
    list_slots,
    prepare_slot_engine_process,
    release_slot,
    reserve_slot,
    set_slot_engine_process,
)
from orca_auto.core.admission import store as admission_store
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue import processes as queue_processes
from orca_auto.core.queue.processes import (
    ManagedProcess,
    ProcessGroupTerminationDeps,
    terminate_process_group,
)
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_PREPARING,
    queue_record_publication_lock_path,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker.models import ReservedQueueEntry
from orca_auto.core.statuses import STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED
from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.process_tracking import RUN_LOCK_FILE_NAME
from orca_auto.orca import execution as execution_mod
from orca_auto.orca import notifications as lifecycle_notifications
from orca_auto.orca.config import AppConfig
from orca_auto.orca.queue import job_records, publication_repair
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue import worker as queue_worker_mod
from orca_auto.orca.queue.adapter import (
    DuplicateEntryError,
    cancel,
    enqueue,
    list_queue,
    mark_failed,
    queue_entry_reaction_dir,
)
from orca_auto.orca.queue.models import OrcaRunningJob, TerminalReplayWorkItem
from orca_auto.orca.queue.orphans import read_worker_pid
from orca_auto.orca.queue.replay import TerminalQueueMarkResult
from orca_auto.orca.queue.terminal_replay import terminal_replay_marker_from_entry
from orca_auto.orca.queue.worker import DEFAULT_MAX_CONCURRENT, OrcaQueueWorker
from orca_auto.orca.queue.worker_tracking import notify_terminal_job_from_state
from orca_auto.orca.state import finalize_state, new_state, save_state
from orca_auto.orca.state_reading import load_state, report_json_path
from orca_auto.orca.statuses import RunStatus
from orca_auto.orca.types import RunFinalResult
from tests.conftest import (
    RecordingChannel,
    claim_next_entry,
    enqueue_entry,
    make_app_cfg,
    make_queue_entry,
    write_run_state,
)
from tests.engine_artifact_helpers import bind_report_generation, orca_artifact_payload
from tests.process_helpers import FakeManagedProcess, missing_process_group
from tests.queue_worker_helpers import (
    current_orca_queue_metadata,
    reconcile_statuses,
    run_terminal_replay,
    write_completed_run_state,
)

WORKER_LOGGER = "orca_auto.orca.queue.worker"

# ---------------------------------------------------------------------------
# Fakes: children behind the OS seams, a recording child starter
# ---------------------------------------------------------------------------


@dataclass
class FakeChildren:
    """Children with fake pids behind the two OS seams a stop reaches them through.

    ``terminate_process_group`` runs for real: it polls, signals the group via
    ``os.killpg``, waits and escalates. Only the kernel's answers are faked: a
    SIGTERM makes the child exit with its configured code (after its own stop
    handling, when given), a SIGKILL ends it regardless, a stubborn child
    ignores both, and the pid probe reports what the registry says.
    """

    by_pid: dict[int, FakeManagedProcess] = field(default_factory=dict)
    exit_codes: dict[int, int] = field(default_factory=dict)
    stop_hooks: dict[int, Callable[[], None]] = field(default_factory=dict)
    stubborn: set[int] = field(default_factory=set)
    sigterm_ignorers: set[int] = field(default_factory=set)
    signals: list[tuple[int, int]] = field(default_factory=list)
    next_pid: int = 40001

    def spawn(
        self,
        *,
        exited: int | None = None,
        exit_code: int = -signal.SIGTERM,
        on_stop: Callable[[], None] | None = None,
        stubborn: bool = False,
        ignores_sigterm: bool = False,
    ) -> FakeManagedProcess:
        pid = self.next_pid
        self.next_pid += 1
        process = FakeManagedProcess(pid=pid, poll_result=exited)
        if stubborn:
            self.stubborn.add(pid)
            process.wait_side_effects = [
                subprocess.TimeoutExpired(cmd="child", timeout=1),
                subprocess.TimeoutExpired(cmd="child", timeout=1),
            ]
        elif ignores_sigterm:
            self.sigterm_ignorers.add(pid)
            process.wait_side_effects = [subprocess.TimeoutExpired(cmd="child", timeout=1)]
        self.by_pid[pid] = process
        self.exit_codes[pid] = exit_code
        if on_stop is not None:
            self.stop_hooks[pid] = on_stop
        return process

    def killpg(self, pgid: int, signum: int) -> None:
        child = self.by_pid.get(pgid)
        if child is None or child.poll_result is not None:
            raise ProcessLookupError(f"no process group {pgid}")
        if signum == 0:
            return
        self.signals.append((pgid, signum))
        if pgid in self.stubborn or (pgid in self.sigterm_ignorers and signum == signal.SIGTERM):
            return
        if signum == signal.SIGTERM:
            hook = self.stop_hooks.get(pgid)
            if hook is not None:
                hook()
            child.poll_result = self.exit_codes[pgid]
        elif signum == signal.SIGKILL:
            child.poll_result = -signal.SIGKILL

    def pid_exists(self, pid: int) -> bool:
        child = self.by_pid.get(pid)
        return child is not None and child.poll_result is None

    def stopped(self, process: FakeManagedProcess) -> bool:
        return process.poll_result is not None and (process.pid, signal.SIGTERM) in self.signals


class SequencedPollProcess(FakeManagedProcess):
    """A child whose successive ``poll`` answers are scripted (the leader exits mid-check)."""

    def __init__(self, polls: list[int | None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._polls = list(polls)

    def poll(self) -> int | None:
        if len(self._polls) > 1:
            return self._polls.pop(0)
        return self._polls[0]


@dataclass
class StartedChild:
    queue_root: Path
    entry: QueueEntry
    admission_token: str
    process: FakeManagedProcess


@dataclass
class ChildStarter:
    """The worker's ``_start_background_process`` seam, spawning fake children."""

    children: FakeChildren
    started: list[StartedChild] = field(default_factory=list)
    error: Exception | None = None

    def __call__(
        self,
        *,
        queue_root: Path,
        entry: QueueEntry,
        admission_token: str,
    ) -> ManagedProcess:
        if self.error is not None:
            raise self.error
        process = self.children.spawn()
        self.started.append(StartedChild(queue_root, entry, admission_token, process))
        return process


@dataclass
class SpawnCall:
    args: list[str]
    log_path: Path | None
    process: FakeManagedProcess


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_children(monkeypatch: pytest.MonkeyPatch) -> FakeChildren:
    children = FakeChildren()
    real_start_ticks = admission_store._process_start_ticks

    def start_ticks(pid: int) -> int | None:
        # A fake pid has no /proc entry; give it a stable identity so the
        # admission store can attach it as a slot owner. Real pids stay real.
        return pid if pid in children.by_pid else real_start_ticks(pid)

    real_kill = os.kill

    def kill(pid: int, signum: int) -> None:
        # The admission store probes slot owners with ``os.kill(pid, 0)``; a
        # fake child is one process, so signalling it is signalling its group.
        if pid not in children.by_pid:
            real_kill(pid, signum)
            return
        children.killpg(pid, signum)

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "killpg", children.killpg)
    monkeypatch.setattr(queue_processes, "_pid_exists", children.pid_exists)
    monkeypatch.setattr(admission_store, "_process_start_ticks", start_ticks)
    return children


@pytest.fixture
def child_starter(fake_children: FakeChildren) -> ChildStarter:
    return ChildStarter(fake_children)


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch, fake_children: FakeChildren) -> list[SpawnCall]:
    """Replace ``subprocess.Popen`` (the spawn itself) and record what the worker launched."""

    calls: list[SpawnCall] = []

    def popen(args: list[str], **kwargs: Any) -> FakeManagedProcess:
        fileno = getattr(kwargs.get("stdout"), "fileno", None)
        log_path = Path(os.readlink(f"/proc/self/fd/{fileno()}")) if fileno else None
        process = fake_children.spawn()
        calls.append(SpawnCall(list(args), log_path, process))
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


@pytest.fixture
def sleeping_child() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Spawn real ``sleep`` children in their own session; whatever survives is killed."""

    children: list[subprocess.Popen[bytes]] = []

    def spawn() -> subprocess.Popen[bytes]:
        process = subprocess.Popen(["sleep", "60"], start_new_session=True)
        children.append(process)
        return process

    yield spawn
    for process in children:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.fixture
def worker_cfg(app_cfg: Callable[..., AppConfig], queue_root: Path) -> AppConfig:
    return app_cfg(runs_root=queue_root)


@pytest.fixture
def make_worker(
    worker_cfg: AppConfig,
    queue_root: Path,
    preserved_signals: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., OrcaQueueWorker]:
    """Factory: ``make_worker(max_concurrent=2, cfg=None, start=None, sleep=None)``."""

    del preserved_signals

    def factory(
        *,
        max_concurrent: int = 2,
        cfg: AppConfig | None = None,
        start: ChildStarter | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> OrcaQueueWorker:
        config_path = str(queue_root / "config.yaml")
        worker = OrcaQueueWorker(
            cfg or worker_cfg, config_path, max_concurrent=max_concurrent, sleep_fn=sleep
        )
        if start is not None:
            monkeypatch.setattr(worker, "_start_background_process", start)
        return worker

    return factory


@pytest.fixture
def worker(make_worker: Callable[..., OrcaQueueWorker]) -> OrcaQueueWorker:
    return make_worker()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _command_arg(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def reserve_job_slot(
    root: Path, limit: int, entry: QueueEntry, reaction_dir: Path, **extra: Any
) -> str:
    token = reserve_slot(
        root,
        limit,
        work_dir=str(reaction_dir),
        queue_id=entry.queue_id,
        source="queue_worker",
        state="reserved",
        **extra,
    )
    assert token is not None
    return token


def pending_launch_slot(root: Path, limit: int, entry: QueueEntry, reaction_dir: Path) -> str:
    """A slot whose engine launch is pending under this (live) process: recovery must refuse."""

    token = reserve_job_slot(
        root,
        limit,
        entry,
        reaction_dir,
        owner_pid=os.getpid(),
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(root, token) is not None
    return token


def running_job(
    worker: OrcaQueueWorker,
    entry: QueueEntry,
    reaction_dir: Path | str,
    process: ManagedProcess,
    admission_token: str,
    *,
    task_id: str | None = None,
) -> OrcaRunningJob:
    return OrcaRunningJob(
        queue_root=worker.allowed_root,
        queue_id=entry.queue_id,
        reaction_dir=str(reaction_dir),
        process=process,
        admission_token=admission_token,
        task_id=entry.task_id if task_id is None else task_id,
    )


def queue_statuses(root: Path) -> dict[str, QueueStatus]:
    return {row.queue_id: row.status for row in list_queue(root)}


def queue_row(root: Path, queue_id: str) -> QueueEntry:
    return next(row for row in list_queue(root) if row.queue_id == queue_id)


def admission_file_identity(root: Path) -> tuple[int, int]:
    status = (root / "admission_slots.json").stat()
    return (status.st_ino, status.st_mtime_ns)


def job_record(root: Path, job_id: str) -> dict[str, Any] | None:
    path = root / "job_locations.json"
    if not path.exists():
        return None
    records = json.loads(path.read_text(encoding="utf-8"))
    return next((record for record in records if record.get("job_id") == job_id), None)


def save_child_state(
    reaction_dir: Path, job_id: str, status: str, final_result: RunFinalResult | None = None
) -> None:
    """The ``job_state.json`` a child left behind, as it would have written it."""

    state = new_state(reaction_dir, reaction_dir / "job.inp")
    state["job_id"] = job_id
    state["status"] = status
    if final_result is not None:
        state["final_result"] = final_result
    save_state(reaction_dir, state)


def replay_item(root: Path, queue_id: str, reaction_dir: Path, *, resolved: bool = True) -> Any:
    return TerminalReplayWorkItem(
        queue_root=root,
        queue_id=queue_id,
        reaction_dir=str(reaction_dir),
        reaction_key=str(reaction_dir.resolve()) if resolved else str(reaction_dir),
        task_id=f"task-{queue_id}",
        observed_status="failed",
        selected_inp="",
        error="",
    )


def awaited_send(channel: RecordingChannel, *, sent: bool = True) -> Event:
    """Deliveries happen on a background thread; the event fires when one lands."""

    delivered = Event()

    def on_send(_message: object) -> SendResult | None:
        delivered.set()
        return None if sent else SendResult(sent=False)

    channel.on_send = on_send
    return delivered


@contextmanager
def held_run_lock(reaction_dir: Path) -> Iterator[None]:
    """Hold ``run.lock`` so the terminal run-state writers refuse (an ORCA instance owns the dir)."""

    with file_lock(reaction_dir / RUN_LOCK_FILE_NAME, timeout_seconds=0.0):
        yield


@contextmanager
def read_only(directory: Path) -> Iterator[None]:
    """Refuse new files under ``directory`` (existing files stay readable)."""

    if os.geteuid() == 0:
        pytest.skip("a read-only directory does not refuse writes to root")
    directory.chmod(0o500)
    try:
        yield
    finally:
        directory.chmod(0o700)


def insert_pending_successor(root: Path, reaction_dir: Path, *, queue_id: str) -> QueueEntry:
    # The enqueue fence refuses a same-directory successor while the prior
    # generation is unpublished; the worker gate is the second barrier for
    # the windows that fence does not cover, so the row is written directly.
    rows = list_queue(root)
    template = next(row for row in rows if queue_entry_reaction_dir(row) == str(reaction_dir))
    successor = replace(
        template,
        queue_id=queue_id,
        task_id=f"task-{queue_id}",
        status=QueueStatus.PENDING,
        started_at="",
        finished_at="",
        error="",
        cancel_requested=False,
        metadata={
            **{k: v for k, v in template.metadata.items() if k != "orca_terminal_replay"},
            **current_orca_queue_metadata(reaction_dir),
        },
    )
    save_entries_core(root, [*rows, successor])
    return successor


# ---------------------------------------------------------------------------
# terminate_process_group
# ---------------------------------------------------------------------------

_NO_LIVE_PIDS = ProcessGroupTerminationDeps(pid_exists=lambda _pid: False)


def _terminate(process: ManagedProcess, deps: ProcessGroupTerminationDeps = _NO_LIVE_PIDS) -> bool:
    return terminate_process_group(process, killpg_fn=missing_process_group, deps=deps)


def test_already_terminated() -> None:
    process = FakeManagedProcess(poll_result=0)
    assert _terminate(process) is True
    assert process.terminate_calls == 0


def test_terminate_success() -> None:
    process = SequencedPollProcess([None, 0, 0], pid=1234)
    assert _terminate(process) is True
    assert (process.terminate_calls, process.kill_calls) == (1, 0)


def test_terminate_ignores_errors() -> None:
    process = SequencedPollProcess([None, 0, 0], pid=1234, terminate_error=RuntimeError("nope"))
    assert _terminate(process) is True
    assert process.terminate_calls == 1


def test_terminate_does_not_signal_reused_pid() -> None:
    process = SequencedPollProcess([None, 0], pid=1234)
    assert _terminate(process, ProcessGroupTerminationDeps(pid_exists=lambda _pid: True)) is True
    assert (process.terminate_calls, process.kill_calls) == (0, 0)


def test_escalate_to_kill() -> None:
    process = FakeManagedProcess(
        pid=1234,
        wait_side_effects=[
            subprocess.TimeoutExpired(cmd="worker", timeout=10),
            subprocess.TimeoutExpired(cmd="worker", timeout=5),
        ],
    )
    assert _terminate(process) is False
    assert (process.terminate_calls, process.kill_calls) == (1, 1)


# ---------------------------------------------------------------------------
# read_worker_pid
# ---------------------------------------------------------------------------


def test_no_pid_file(tmp_path: Path) -> None:
    assert read_worker_pid(tmp_path) is None


def test_stale_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "queue_worker.pid"
    pid_path.write_text("999999999")  # non-existent pid
    assert read_worker_pid(tmp_path) is None
    # PID file should be cleaned up
    assert not pid_path.exists()


def test_invalid_pid_content(tmp_path: Path) -> None:
    (tmp_path / "queue_worker.pid").write_text("not_a_number")
    assert read_worker_pid(tmp_path) is None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_max_concurrent_floor(tmp_path: Path) -> None:
    worker = OrcaQueueWorker(
        make_app_cfg(tmp_path), str(tmp_path / "config.yaml"), max_concurrent=0
    )
    assert worker.max_concurrent == 1


def test_default_init(tmp_path: Path) -> None:
    worker = OrcaQueueWorker(make_app_cfg(tmp_path), str(tmp_path / "config.yaml"))
    assert worker.max_concurrent == DEFAULT_MAX_CONCURRENT
    assert not worker._shutdown_requested
    assert len(worker._running) == 0


def test_queue_worker_does_not_mutate_config_max_concurrent(tmp_path: Path) -> None:
    cfg = make_app_cfg(tmp_path)
    original_max_concurrent = cfg.runtime.max_concurrent

    worker = OrcaQueueWorker(cfg, str(tmp_path / "config.yaml"), max_concurrent=2)

    assert cfg.runtime.max_concurrent == original_max_concurrent
    assert worker.cfg is not cfg
    assert worker.cfg.runtime is not cfg.runtime
    assert worker.cfg.runtime.max_concurrent == 2
    assert worker.max_concurrent == 2
    assert worker.admission_limit == 2


def test_queue_worker_does_not_mutate_config_with_explicit_admission_limit(tmp_path: Path) -> None:
    cfg = make_app_cfg(tmp_path)
    cfg = replace(cfg, runtime=replace(cfg.runtime, admission_limit=5))
    original_max_concurrent = cfg.runtime.max_concurrent

    worker = OrcaQueueWorker(cfg, str(tmp_path / "config.yaml"), max_concurrent=2)

    assert worker.cfg is cfg
    assert cfg.runtime.max_concurrent == original_max_concurrent
    assert worker.max_concurrent == 2
    assert worker.admission_limit == 5


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


# ---------------------------------------------------------------------------
# Admission and child start
# ---------------------------------------------------------------------------


def test_fill_slots_empty_queue(worker: OrcaQueueWorker) -> None:
    worker._fill_slots()
    assert len(worker._running) == 0


def test_fill_slots_idle_poll_leaves_admission_file_untouched(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    # Capacity must remain: at the limit main also skips the write, which
    # would make the assertion vacuous. One live slot out of two stays.
    token = reserve_slot(worker.admission_root, 2, source="queue_worker", state="reserved")
    assert token is not None
    assert worker.admission_limit == 2
    assert len(list_slots(worker.admission_root)) == 1
    before = admission_file_identity(queue_root)

    status = worker._fill_slots()

    assert status == "idle"
    assert admission_file_identity(queue_root) == before
    slots = json.loads((queue_root / "admission_slots.json").read_text(encoding="utf-8"))
    assert [slot["token"] for slot in slots] == [token]
    assert len(worker._running) == 0
    assert child_starter.started == []


def test_admission_reservation_moves_the_admission_file_to_a_new_inode(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    # Positive control for the idle-poll probe above: a real reservation
    # write replaces the admission file, so an unchanged inode is evidence
    # that no reservation happened.
    first = reserve_slot(worker.admission_root, 2, source="probe", state="reserved")
    assert first is not None
    before = admission_file_identity(queue_root)
    second = reserve_slot(worker.admission_root, 2, source="probe", state="reserved")
    assert second is not None
    # The atomic replace may reuse the freed inode number, so the probe
    # the idle-poll test relies on is the (inode, mtime_ns) pair.
    assert admission_file_identity(queue_root) != before
    assert len(list_slots(worker.admission_root)) == 2


def test_start_job(worker: OrcaQueueWorker, fake_popen: list[SpawnCall], queue_root: Path) -> None:
    entry = QueueEntry(
        queue_id="q_test",
        app_name="orca_auto_orca",
        task_id="task_test_123",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={
            "reaction_dir": str(queue_root / "mol_A"),
            "force": False,
            "worker_log": "/tmp/unsafe-worker.log",
        },
    )
    token = reserve_slot(queue_root, worker.max_concurrent, source="queue_worker", state="reserved")
    assert token is not None

    worker._start_job(queue_root, entry, admission_token=token)

    assert "q_test" in worker._running
    [spawned] = fake_popen
    assert worker._running["q_test"].process is spawned.process
    log_path = (queue_root / "logs" / "q_test.log").resolve()
    assert spawned.log_path == log_path
    assert log_path.exists()
    command = spawned.args
    assert "orca_auto.orca.commands.worker_child" in command
    assert "--engine" not in command
    assert _command_arg(command, "--queue-root") == str(queue_root)
    assert _command_arg(command, "--queue-id") == "q_test"
    assert _command_arg(command, "--admission-token") == token
    assert "--reaction-dir" not in command


def test_start_job_prefers_queue_metadata_for_tracking(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    reaction_dir = queue_root / "mol_meta"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "rxn.inp"
    # Derived from this input the job would be an "opt" of molecule "rxn";
    # the queue row carries a different identity, which must win.
    selected_inp.write_text("! Opt\n", encoding="utf-8")
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
            "job_type": "freq",
            "molecule_key": "queue-H2",
            "resource_request": {"max_cores": 4, "max_memory_gb": 12},
            "resource_actual": {"max_cores": 3, "max_memory_gb": 11},
        },
    )
    token = reserve_slot(queue_root, worker.max_concurrent, source="queue_worker", state="reserved")
    assert token is not None

    worker._start_job(queue_root, entry, admission_token=token)

    record = job_record(queue_root, "task_meta_123")
    assert record is not None
    assert record["status"] == "running"
    assert Path(record["original_run_dir"]) == reaction_dir.resolve()
    assert record["job_type"] == "orca_freq"
    assert record["selected_input_xyz"] == str(selected_inp)
    assert record["molecule_key"] == "queue-H2"
    assert record["resource_request"] == {"max_cores": 4, "max_memory_gb": 12}
    assert record["resource_actual"] == {"max_cores": 3, "max_memory_gb": 11}


def test_start_job_oserror(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    child_starter.error = OSError("spawn failed")
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_err"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)

    assert worker._start_job(queue_root, entry, admission_token=token) is False

    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    [failed] = list_queue(queue_root)
    assert (failed.status, failed.error) == (QueueStatus.FAILED, "spawn failed")


def test_start_job_attach_error_releases_slot_and_terminates_process(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The slot vanished between reservation and attach (an operator cleared
    # the admission file): the child must not run without admission.
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_attach_err"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)
    release_slot(queue_root, token)
    release_calls: list[str] = []
    real_release = worker._release_admission_slot

    def counted_release(admission_token: str) -> object:
        release_calls.append(admission_token)
        return real_release(admission_token)

    monkeypatch.setattr(worker, "_release_admission_slot", counted_release)

    assert worker._start_job(queue_root, entry, admission_token=token) is False

    assert entry.queue_id not in worker._running
    [started] = child_starter.started
    assert fake_children.stopped(started.process)
    assert active_slot_count(queue_root) == 0
    [updated] = list_queue(queue_root)
    assert updated.status == QueueStatus.FAILED
    # Handled exactly once: the specific refusal reason survives and the slot
    # is released once (a second release of the same token would return False).
    assert updated.error == "admission_slot_missing"
    assert release_calls == [token]


def test_start_error_does_not_fail_replacement_generation(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    rxn = queue_root / "mol_start_error_replacement"
    rxn.mkdir()
    selected = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_job_slot(queue_root, 2, selected, rxn)
    replacement = replace(running, task_id="task-b")
    save_entries_core(queue_root, [replacement])

    worker._mark_entry_failed_and_release(queue_root, running, token, error="worker start failed")

    [durable] = list_queue(queue_root)
    assert (durable.task_id, durable.status) == ("task-b", QueueStatus.RUNNING)
    assert active_slot_count(queue_root) == 0


def test_fill_slots_starts_pending_jobs(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_A"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))

    worker._fill_slots()

    assert list(worker._running) == [entry.queue_id]
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.RUNNING}


def test_fill_slots_does_not_reclaim_a_row_whose_previous_job_is_still_tracked(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    # A child stopped by an external SIGTERM requeues its own row for resume
    # and only then exits; the requeue is applied directly here. Until the
    # parent has seen that exit and released the slot, the row must not start
    # a second job under the same queue id: that would replace the tracked
    # job and strand its admission slot.
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_requeued"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))
    behind = queue_root / "mol_behind"
    behind.mkdir()

    worker._fill_slots()
    tracked = worker._running[entry.queue_id]
    assert queue_worker_mod.requeue_running_entry(queue_root, entry.queue_id)
    behind_entry = enqueue(queue_root, str(behind), metadata=current_orca_queue_metadata(behind))

    worker._fill_slots()

    assert len(child_starter.started) == 2
    assert worker._running[entry.queue_id] is tracked
    statuses = queue_statuses(queue_root)
    assert statuses[entry.queue_id] == QueueStatus.PENDING
    # The row behind it is unaffected and takes the free slot.
    assert statuses[behind_entry.queue_id] == QueueStatus.RUNNING
    assert worker._running[behind_entry.queue_id].process is child_starter.started[1].process


def test_fill_slots_with_only_a_tracked_pending_row_leaves_admission_untouched(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_requeued_only"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))
    worker._fill_slots()
    assert queue_worker_mod.requeue_running_entry(queue_root, entry.queue_id)
    before = admission_file_identity(queue_root)

    status = worker._fill_slots()

    assert status == "idle"
    assert admission_file_identity(queue_root) == before
    assert len(child_starter.started) == 1
    [row] = list_queue(queue_root)
    assert row.status == QueueStatus.PENDING


def test_fill_slots_attaches_queue_identity_to_reserved_slot(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    rxn = queue_root / "mol_identity"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), metadata=current_orca_queue_metadata(rxn))

    worker._fill_slots()

    [slot] = list_slots(queue_root)
    [started] = child_starter.started
    assert slot.queue_id == entry.queue_id
    assert slot.app_name == entry.app_name
    assert slot.task_id == entry.task_id
    assert slot.state == "active"
    assert slot.owner_pid == started.process.pid
    assert slot.work_dir == str(rxn)


def test_fill_slots_preserves_task_id_across_slot_and_worker_handoff(
    make_worker: Callable[..., OrcaQueueWorker], fake_popen: list[SpawnCall], queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1)
    rxn = queue_root / "mol_task_identity"
    rxn.mkdir()
    entry = enqueue(
        queue_root,
        str(rxn),
        task_id="orca_task_preserved_123",
        metadata=current_orca_queue_metadata(rxn),
    )
    assert entry.queue_id != entry.task_id

    worker._fill_slots()

    [slot] = list_slots(queue_root)
    assert slot.queue_id == entry.queue_id
    assert slot.task_id == entry.task_id
    assert slot.queue_id != slot.task_id
    [spawned] = fake_popen
    assert spawned.log_path == (queue_root / "logs" / f"{entry.queue_id}.log").resolve()
    assert _command_arg(spawned.args, "--admission-token") == slot.token
    assert _command_arg(spawned.args, "--queue-id") == entry.queue_id
    assert "--admission-task-id" not in spawned.args
    assert "--admission-app-name" not in spawned.args


def test_fill_slots_respects_max_concurrent(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    for name in ("a", "b"):
        d = queue_root / name
        d.mkdir()
        enqueue(queue_root, str(d), metadata=current_orca_queue_metadata(d))

    worker._fill_slots()

    assert len(worker._running) == 1
    assert sorted(queue_statuses(queue_root).values(), key=str) == [
        QueueStatus.PENDING,
        QueueStatus.RUNNING,
    ]


def test_fill_slots_fills_all_available_capacity(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=3, start=child_starter)
    for name in ("p1", "p2", "p3", "p4"):
        reaction_dir = queue_root / name
        reaction_dir.mkdir()
        enqueue(queue_root, str(reaction_dir), metadata=current_orca_queue_metadata(reaction_dir))

    worker._fill_slots()

    queue_by_name = {
        Path(queue_entry_reaction_dir(entry)).name: entry.status.value
        for entry in list_queue(queue_root)
    }
    assert len(worker._running) == 3
    assert len(child_starter.started) == 3
    assert queue_by_name == {"p1": "running", "p2": "running", "p3": "running", "p4": "pending"}


def test_fill_slots_refills_immediately_after_completion(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    first_dir = queue_root / "first"
    second_dir = queue_root / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    completed_entry = enqueue(
        queue_root,
        str(first_dir),
        task_id="task_terminal_123",
        metadata=current_orca_queue_metadata(first_dir),
    )
    pending_entry = enqueue(
        queue_root, str(second_dir), metadata=current_orca_queue_metadata(second_dir)
    )
    claim_next_entry(queue_root)
    write_completed_run_state(first_dir)
    token = reserve_job_slot(queue_root, worker.max_concurrent, completed_entry, first_dir)
    worker._running[completed_entry.queue_id] = running_job(
        worker, completed_entry, first_dir, fake_children.spawn(exited=0), token, task_id=None
    )

    worker._check_completed_jobs()
    worker._fill_slots()

    queue_by_name = {
        Path(queue_entry_reaction_dir(entry)).name: entry.status.value
        for entry in list_queue(queue_root)
    }
    assert len(child_starter.started) == 1
    assert list(worker._running) == [pending_entry.queue_id]
    assert queue_by_name == {"first": "completed", "second": "running"}


def test_fill_slots_respects_admission_slots_without_run_lock(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    queued = queue_root / "queued_only"
    queued.mkdir()
    entry = enqueue(queue_root, str(queued), metadata=current_orca_queue_metadata(queued))
    token = reserve_slot(
        worker.admission_root,
        1,
        work_dir=str(queue_root / "reserved_hold"),
        source="queue_worker",
        state="reserved",
    )
    assert token is not None

    worker._fill_slots()

    assert len(worker._running) == 0
    assert child_starter.started == []
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def test_fill_slots_counts_existing_worker_admission_slot_once(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    worker = make_worker(max_concurrent=2, start=child_starter)
    active_dir = queue_root / "already_running"
    token = reserve_slot(
        queue_root,
        worker.max_concurrent,
        work_dir=str(active_dir),
        queue_id="q_existing",
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    worker._running["q_existing"] = OrcaRunningJob(
        queue_root=worker.allowed_root,
        queue_id="q_existing",
        reaction_dir=str(active_dir),
        process=fake_children.spawn(),
        admission_token=token,
    )
    queued = queue_root / "queued_only"
    queued.mkdir()
    enqueue(queue_root, str(queued), metadata=current_orca_queue_metadata(queued))

    worker._fill_slots()

    assert len(worker._running) == 2
    assert len(child_starter.started) == 1


# ---------------------------------------------------------------------------
# Completion and terminal finalization
# ---------------------------------------------------------------------------


def test_check_completed_jobs_success(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_done"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=0), token, task_id=None
    )

    worker._check_completed_jobs()

    assert len(worker._running) == 0
    assert active_slot_count(queue_root) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}


def test_check_completed_jobs_leaves_a_deferred_child_pending(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    queue_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The child was refused RAM scratch before ORCA started, returned its
    # own row to the queue and exited non-zero. That exit code must not
    # fail the pending row, and the slot must become reusable.
    rxn = queue_root / "mol_deferred"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    claim_next_entry(queue_root)
    assert queue_worker_mod.requeue_running_entry(
        queue_root,
        entry.queue_id,
        admission_deferral_reason="engine scratch cannot guarantee RAM headroom",
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=75), token, task_id=None
    )

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        worker._check_completed_jobs()

    assert len(worker._running) == 0
    assert active_slot_count(queue_root) == 0
    [updated] = list_queue(queue_root)
    assert (updated.status, updated.error) == (QueueStatus.PENDING, "")
    assert any(
        "waits in the queue" in record.getMessage() and "RAM headroom" in record.getMessage()
        for record in caplog.records
    )
    assert not (rxn / "job_state.json").exists()


def test_check_completed_jobs_failure(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_fail"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=1), "slot_fail", task_id=None
    )

    worker._check_completed_jobs()

    assert len(worker._running) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}


def test_check_completed_jobs_still_running(
    worker: OrcaQueueWorker, fake_children: FakeChildren
) -> None:
    worker._running["q_run"] = OrcaRunningJob(
        queue_root=worker.allowed_root,
        queue_id="q_run",
        reaction_dir="/tmp/r",
        process=fake_children.spawn(),
        admission_token="slot_run",
    )
    worker._check_completed_jobs()
    assert len(worker._running) == 1


def test_completed_job_retries_when_engine_recovery_raises(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    # The slot records an engine launch pending under a live owner, so the
    # engine identity cannot be recovered yet. The exited job must be retained
    # (row running, slot held) until recovery succeeds; here the owner dies.
    rxn = queue_root / "mol_recovery_retry"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-recovery")
    claim_next_entry(queue_root)
    owner = sleeping_child()
    token = reserve_job_slot(
        queue_root,
        worker.max_concurrent,
        entry,
        rxn,
        owner_pid=owner.pid,
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(queue_root, token) is not None
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=1), token
    )

    worker._check_completed_jobs()

    assert entry.queue_id in worker._running
    assert active_slot_count(queue_root) == 1
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.RUNNING}

    owner.kill()
    owner.wait()
    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}


def test_failed_state_write_leaves_durable_replay_for_worker_restart(
    make_worker: Callable[..., OrcaQueueWorker], fake_children: FakeChildren, queue_root: Path
) -> None:
    worker = make_worker()
    rxn = queue_root / "mol_durable_restart"
    rxn.mkdir()
    old_state = new_state(rxn, rxn / "task-a.inp")
    old_state["job_id"] = "task-a"
    finalize_state(
        rxn,
        old_state,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    entry = enqueue(queue_root, str(rxn), force=True, task_id="task-b")
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    # Another ORCA instance holds the directory: the failed run state cannot be written.
    with held_run_lock(rxn), pytest.raises(RuntimeError, match="already running"):
        worker._finalize_completed_job(entry.queue_id, job, rc=1)

    assert active_slot_count(queue_root) == 1
    [terminal] = list_queue(queue_root)
    assert terminal.status == QueueStatus.FAILED
    marker = terminal.metadata.get("orca_terminal_replay")
    assert isinstance(marker, dict)
    assert marker["task_id"] == "task-b"
    assert marker["observed_state"]["job_id"] == "task-a"

    restarted = make_worker()
    run_terminal_replay(restarted, queue_root, terminal)

    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == ("task-b", STATUS_FAILED)
    [replayed] = list_queue(queue_root)
    assert replayed.metadata.get("orca_terminal_replay") is None


def test_terminal_side_effect_failure_withholds_only_the_same_directory(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = make_worker(start=child_starter)
    rxn = queue_root / "mol_terminal_replay_barrier"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-a")
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, 2, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=1), token
    )
    unrelated = queue_root / "mol_unrelated"
    unrelated.mkdir()

    with held_run_lock(rxn):
        worker._check_completed_jobs()

        assert entry.queue_id in worker._running
        assert active_slot_count(queue_root) == 1
        [pending_replay] = list_queue(queue_root)
        assert isinstance(pending_replay.metadata.get("orca_terminal_replay"), dict)
        with pytest.raises(DuplicateEntryError):
            enqueue(queue_root, str(rxn), force=True, task_id="task-b")
        # One of two slots is free, so what holds the successor back is the
        # replay barrier, not capacity. With nothing else pending the poll
        # is idle and leaves the admission file alone.
        successor = insert_pending_successor(queue_root, rxn, queue_id="q_forced_successor")
        before = admission_file_identity(queue_root)
        with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
            assert worker._fill_slots() == "idle"
        assert admission_file_identity(queue_root) == before
        assert any(str(rxn.resolve()) in record.getMessage() for record in caplog.records)
        assert child_starter.started == []

        # An unrelated job queued behind the withheld row is admitted.
        other = enqueue(
            queue_root,
            str(unrelated),
            task_id="task-unrelated",
            metadata=current_orca_queue_metadata(unrelated),
        )
        assert worker._fill_slots() == "processed"
        assert other.queue_id in worker._running
        assert successor.queue_id not in worker._running
        statuses = queue_statuses(queue_root)
        assert statuses[successor.queue_id] == QueueStatus.PENDING
        assert statuses[other.queue_id] == QueueStatus.RUNNING
        assert len(child_starter.started) == 1

    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == ("task-a", STATUS_FAILED)
    # The previous generation is published: its successor may start.
    assert worker._fill_slots() == "processed"
    assert successor.queue_id in worker._running
    assert len(child_starter.started) == 2


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("restart", [False, True])
def test_terminal_index_failure_releases_capacity_and_replays_after_recovery(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    outcome: str,
    restart: bool,
) -> None:
    worker = make_worker(max_concurrent=1, start=child_starter)
    rxn = queue_root / "terminal_index_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-terminal-index")
    claim_next_entry(queue_root)
    if outcome == "completed":
        write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, 1, entry, rxn)
    child = fake_children.spawn(exited=None if outcome == "cancelled" else int(outcome == "failed"))
    worker._running[entry.queue_id] = running_job(worker, entry, rxn, child, token)
    if outcome == "cancelled":
        cancel(queue_root, entry.queue_id)

    other_dir = queue_root / "terminal_index_unrelated"
    other = enqueue(
        queue_root,
        str(other_dir),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(other_dir),
    )
    job_records.upsert_queued_job_record(worker.cfg, other)
    index_path = queue_root / "job_locations.json"
    index_before = index_path.read_bytes()
    index_path.write_text("{unreadable index", encoding="utf-8")

    if outcome == "cancelled":
        worker._check_cancel_requests()
    else:
        worker._check_completed_jobs()

    # Execution capacity is independent of the derived index. The durable
    # marker still owns this generation even after the reaped child is dropped.
    assert entry.queue_id not in worker._running
    assert get_slot(queue_root, token) is None
    terminal = queue_row(queue_root, entry.queue_id)
    assert terminal.status.value == outcome
    assert terminal_replay_marker_from_entry(terminal) is not None
    saved = load_state(rxn)
    assert saved is not None and saved["status"] == outcome
    run_id = saved["run_id"]
    assert recording_channel.sends == []

    if restart:
        worker = make_worker(max_concurrent=1, start=child_starter)
        worker._reconcile_worker_state()
    assert len(worker.replay_state.pending_replays) == 1
    with pytest.raises(DuplicateEntryError):
        enqueue(queue_root, str(rxn), force=True, task_id="task-successor")
    # Even a separately inserted successor cannot claim the freed slot.
    successor = insert_pending_successor(queue_root, rxn, queue_id="q_wait_for_publication")
    assert worker._fill_slots() == "processed"
    assert list(worker._running) == [other.queue_id]
    assert queue_statuses(queue_root)[successor.queue_id] == QueueStatus.PENDING
    assert [started.entry.queue_id for started in child_starter.started] == [other.queue_id]

    # Remove the deliberately injected competing row before recovery so the
    # durable terminal generation remains the unambiguous artifact owner.
    save_entries_core(
        queue_root, [row for row in list_queue(queue_root) if row.queue_id != successor.queue_id]
    )
    index_path.write_bytes(index_before)
    delivered = awaited_send(recording_channel)
    worker._reconcile_worker_state()
    assert delivered.wait(1)
    worker._reconcile_worker_state()
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == outcome
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert worker.replay_state.pending_replays == {}
    saved = load_state(rxn)
    assert saved is not None and saved["run_id"] == run_id
    assert len(recording_channel.sends) == 1
    # A normal force submission is admitted by the generation fence again.
    assert enqueue(queue_root, str(rxn), force=True, task_id="task-successor")


@pytest.mark.parametrize("state_problem", ["missing", "unreadable", "running"])
def test_completed_child_retains_capacity_until_terminal_evidence_is_ready(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    state_problem: str,
) -> None:
    rxn = queue_root / "terminal_evidence_missing"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-evidence")
    claim_next_entry(queue_root)
    if state_problem == "unreadable":
        (rxn / "job_state.json").write_text("{unreadable state", encoding="utf-8")
    elif state_problem == "running":
        write_run_state(rxn, status=RunStatus.RUNNING, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    worker._running[entry.queue_id] = job

    worker._check_completed_jobs()
    assert worker._running[entry.queue_id] is job
    assert get_slot(queue_root, token) is not None
    assert job.pending_terminal_replay is not None
    assert not job.pending_terminal_replay.state_prepared
    assert worker.replay_state.pending_replays == {}
    assert terminal_replay_marker_from_entry(queue_row(queue_root, entry.queue_id))
    assert job_record(queue_root, entry.task_id) is None
    assert recording_channel.sends == []

    if state_problem == "running":
        state = load_state(rxn)
        assert state is not None
        finalize_state(
            rxn,
            state,
            status=STATUS_COMPLETED,
            final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
        )
    else:
        write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    worker._check_completed_jobs()
    assert entry.queue_id not in worker._running
    assert get_slot(queue_root, token) is None
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert job_record(queue_root, entry.task_id) is not None


@pytest.mark.parametrize("actual_status", [RunStatus.FAILED, RunStatus.CANCELLED])
def test_terminal_evidence_corrects_zero_exit_status_before_capacity_is_returned(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    queue_root: Path,
    actual_status: RunStatus,
) -> None:
    rxn = queue_root / "terminal_exit_disagreement"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-actual-outcome")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=actual_status, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    release = worker._release_admission_slot
    seen: list[tuple[str, str]] = []

    def observe_release(admission_token: str) -> object:
        row = queue_row(queue_root, entry.queue_id)
        seen.append((row.status.value, str(row.metadata.get("run_id"))))
        return release(admission_token)

    with patch.object(worker, "_release_admission_slot", side_effect=observe_release):
        worker._finalize_completed_job(entry.queue_id, job, rc=0)

    saved = load_state(rxn)
    assert saved is not None
    assert seen == [(actual_status.value, saved["run_id"])]
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == actual_status.value
    assert get_slot(queue_root, token) is None


def test_terminal_slot_release_failure_keeps_retry_owner_before_publication(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    rxn = queue_root / "terminal_release_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-release-failure")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    worker._running[entry.queue_id] = job

    with patch.object(worker, "_release_admission_slot", side_effect=OSError("slot write failed")):
        worker._check_completed_jobs()

    assert worker._running[entry.queue_id] is job
    assert job.terminal_finalize_pending
    assert job.pending_terminal_replay is not None
    assert job.pending_terminal_replay.state_prepared
    assert job.pending_terminal_replay.key in worker.replay_state.pending_replays
    assert get_slot(queue_root, token) is not None
    assert terminal_replay_marker_from_entry(queue_row(queue_root, entry.queue_id))
    assert job_record(queue_root, entry.task_id) is None
    assert recording_channel.sends == []

    assert replay_mod.update_terminal(
        queue_root, entry.queue_id, STATUS_FAILED, expected_task_id=entry.task_id
    )
    delivered = awaited_send(recording_channel)
    worker._check_completed_jobs()
    assert delivered.wait(1)
    assert queue_row(queue_root, entry.queue_id).status == QueueStatus.COMPLETED
    assert entry.queue_id not in worker._running
    assert get_slot(queue_root, token) is None
    assert job.pending_terminal_replay is None
    assert not job.terminal_finalize_pending
    assert worker.replay_state.pending_replays == {}
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert job_record(queue_root, entry.task_id) is not None
    assert len(recording_channel.sends) == 1


def test_pending_publication_rechecks_current_queue_outcome_on_retry(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "terminal_queue_correction"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-queue-correction")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    index_path = queue_root / "job_locations.json"
    index_path.write_text("{unreadable index", encoding="utf-8")
    worker._finalize_completed_job(entry.queue_id, job, rc=0)
    assert worker.replay_state.pending_replays
    # A competing terminal projection changed after the work item was prepared.
    # Retry must compare with the current row, not its cached observed status.
    assert replay_mod.update_terminal(
        queue_root, entry.queue_id, STATUS_FAILED, expected_task_id=entry.task_id
    )
    index_path.unlink()

    worker._reconcile_worker_state()

    assert queue_row(queue_root, entry.queue_id).status == QueueStatus.COMPLETED
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == STATUS_COMPLETED
    assert worker.replay_state.pending_replays == {}
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None


def test_terminal_marker_clear_noop_retains_replay_without_execution_capacity(
    make_worker: Callable[..., OrcaQueueWorker],
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    worker = make_worker()
    rxn = queue_root / "terminal_marker_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-marker-failure")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=0), token
    )
    delivered = awaited_send(recording_channel)

    with patch.object(replay_mod, "_clear_terminal_replay_marker", return_value=False):
        worker._check_completed_jobs()
        assert delivered.wait(1)
        assert entry.queue_id not in worker._running
        assert get_slot(queue_root, token) is None
        assert len(worker.replay_state.pending_replays) == 1
        restarted = make_worker()
        restarted._reconcile_worker_state()
        # Both finish paths must verify a no-op instead of forgetting the marker.
        assert len(restarted.replay_state.pending_replays) == 1
        assert terminal_replay_marker_from_entry(queue_row(queue_root, entry.queue_id))
        assert job_record(queue_root, entry.task_id) is not None
        with pytest.raises(DuplicateEntryError):
            enqueue(queue_root, str(rxn), force=True, task_id="task-successor")

    restarted._reconcile_worker_state()
    assert restarted.replay_state.pending_replays == {}
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    assert len(recording_channel.sends) == 1


def test_pending_replay_without_a_slot_does_not_pause_unrelated_jobs(
    make_worker: Callable[..., OrcaQueueWorker], child_starter: ChildStarter, queue_root: Path
) -> None:
    # A replay found by reconciliation holds no slot and retries only every
    # minute; it used to pause the whole queue for as long as it lasted.
    worker = make_worker(max_concurrent=1, start=child_starter)
    withheld_dir = queue_root / "mol_replay_pending"
    unrelated = queue_root / "mol_replay_unrelated"
    withheld_row = enqueue(
        queue_root,
        str(withheld_dir),
        task_id="task-withheld",
        metadata=current_orca_queue_metadata(withheld_dir),
    )
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    item = replay_item(queue_root, "q_closed_generation", withheld_dir)
    worker.replay_state.pending_replays[item.key] = item

    assert worker._fill_slots() == "processed"

    assert list(worker._running) == [other.queue_id]
    assert queue_statuses(queue_root)[withheld_row.queue_id] == QueueStatus.PENDING


def test_unpublished_generation_without_a_directory_pauses_all_admission(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    worker = make_worker(start=child_starter)
    unrelated = queue_root / "mol_unknown_key_unrelated"
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    job = OrcaRunningJob(
        queue_root=worker.allowed_root,
        queue_id="q_unknown_directory",
        reaction_dir="",
        process=fake_children.spawn(),
        admission_token="slot_unknown_directory",
    )
    job.terminal_finalize_pending = True
    worker._running[job.queue_id] = job

    assert worker._fill_slots() == "blocked"

    assert child_starter.started == []
    assert list(worker._running) == [job.queue_id]
    [row] = list_queue(queue_root)
    assert (row.queue_id, row.status) == (other.queue_id, QueueStatus.PENDING)


@pytest.mark.parametrize("failure", ["busy", "index"])
def test_publication_repair_failure_withholds_only_its_row_and_later_recovers(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
    failure: str,
) -> None:
    worker = make_worker(start=child_starter)
    unpublished = queue_root / "mol_repair_pending"
    row = enqueue_entry(
        queue_root,
        make_queue_entry(
            reaction_dir=unpublished,
            task_id="task-unpublished",
            priority=1,
            metadata={
                **current_orca_queue_metadata(unpublished),
                **queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_PREPARING, token="record_sync_test", owner_pid=0
                ),
            },
        ),
    )
    unrelated = queue_root / "mol_repair_failure_unrelated"
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    job_records.upsert_queued_job_record(worker.cfg, other)
    index_path = queue_root / "job_locations.json"
    index_before = index_path.read_bytes()
    with ExitStack() as stack:
        if failure == "busy":
            lock_path = queue_record_publication_lock_path(queue_root, row.queue_id)
            lock_path.parent.mkdir(exist_ok=True)
            stack.enter_context(file_lock(lock_path, timeout_seconds=0.0))
        else:
            # The queue and snapshots remain readable while the shared index fails.
            index_path.write_text("{invalid index")
            stack.callback(index_path.write_bytes, index_before)

        assert worker._fill_slots() == "processed"
        assert list(worker._running) == [other.queue_id]
        assert [started.entry.queue_id for started in child_starter.started] == [other.queue_id]
        assert active_slot_count(queue_root) == 1
        assert queue_statuses(queue_root) == {
            row.queue_id: QueueStatus.PENDING,
            other.queue_id: QueueStatus.RUNNING,
        }
        pending = queue_row(queue_root, row.queue_id)
        assert queue_record_sync_state(pending) != QUEUE_RECORD_SYNC_COMPLETE
        if failure == "index":
            blocker = pending.metadata[QUEUE_RECORD_SYNC_BLOCKED_KEY]
            assert "job_locations.json" in blocker["reason"]
            assert row.queue_id in blocker["next_action"]
        # A blocked row alone does not churn admission slots on each poll.
        before = admission_file_identity(queue_root)
        assert worker._fill_slots() == "idle"
        assert admission_file_identity(queue_root) == before

    # Recovery automatically makes the original high-priority row eligible.
    assert worker._fill_slots() == "processed"
    repaired = queue_row(queue_root, row.queue_id)
    assert repaired.status == QueueStatus.RUNNING
    assert repaired.task_id == row.task_id
    assert queue_record_sync_state(repaired) == QUEUE_RECORD_SYNC_COMPLETE
    assert not repaired.metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY)
    assert [started.entry.queue_id for started in child_starter.started] == [
        other.queue_id,
        row.queue_id,
    ]
    assert active_slot_count(queue_root) == 2


def test_failed_publication_fence_and_diagnostic_write_withhold_only_unsafe_row(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
) -> None:
    worker = make_worker(start=child_starter)
    unsafe_dir = queue_root / "unsafe"
    unsafe = enqueue(
        queue_root,
        str(unsafe_dir),
        priority=1,
        metadata=current_orca_queue_metadata(unsafe_dir),
    )
    other_dir = queue_root / "ready"
    other = enqueue(
        queue_root,
        str(other_dir),
        metadata=current_orca_queue_metadata(other_dir),
    )
    # The lease says COMPLETE, but its original directory has moved. Neither
    # terminal fencing nor blocker diagnostics can be persisted this pass.
    moved = queue_root / "original"
    unsafe_dir.rename(moved)
    unsafe_dir.mkdir()
    with (
        patch.object(publication_repair, "mark_failed", side_effect=OSError("fence write failed")),
        patch.object(
            publication_repair, "mutate_entries", side_effect=OSError("blocker write failed")
        ),
    ):
        assert worker._fill_slots() == "processed"
        assert list(worker._running) == [other.queue_id]
        unchanged = queue_row(queue_root, unsafe.queue_id)
        assert unchanged.status == QueueStatus.PENDING
        assert queue_record_sync_state(unchanged) == QUEUE_RECORD_SYNC_COMPLETE
        assert not unchanged.metadata.get(QUEUE_RECORD_SYNC_BLOCKED_KEY)
    # Restoring the exact directory identity clears the per-pass refusal.
    unsafe_dir.rmdir()
    moved.rename(unsafe_dir)
    assert worker._fill_slots() == "processed"
    assert unsafe.queue_id in worker._running


def test_unreadable_queue_still_blocks_admission_without_reserving_capacity(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    queue_root: Path,
) -> None:
    worker = make_worker(start=child_starter)
    job_dir = queue_root / "ready"
    entry = enqueue(
        queue_root,
        str(job_dir),
        metadata=current_orca_queue_metadata(job_dir),
    )
    queue_path = queue_root / "queue.json"
    original = queue_path.read_bytes()
    queue_path.write_text("{invalid queue")
    admission_path = queue_root / "admission_slots.json"
    assert not admission_path.exists()
    assert worker._fill_slots() == "blocked"
    assert child_starter.started == []
    assert not admission_path.exists()
    assert queue_path.read_text() == "{invalid queue"
    queue_path.write_bytes(original)
    assert worker._fill_slots() == "processed"
    assert entry.queue_id in worker._running


def test_withheld_directory_is_matched_through_a_symlinked_spelling(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    withheld_dir = queue_root / "mol_symlink_target"
    withheld_dir.mkdir()
    alias = queue_root / "mol_symlink_alias"
    alias.symlink_to(withheld_dir, target_is_directory=True)
    state = worker.replay_state
    state.admission_withheld_keys = frozenset({str(withheld_dir.resolve())})

    def row(reaction_dir: str) -> QueueEntry:
        return QueueEntry(
            queue_id="q_candidate",
            app_name="orca_auto_orca",
            task_id="task-candidate",
            task_kind="orca_run_inp",
            engine="orca",
            metadata={"reaction_dir": reaction_dir},
        )

    assert worker._entry_waits_for_terminal_replay(row(str(alias)))
    assert worker._entry_waits_for_terminal_replay(row(""))
    assert not worker._entry_waits_for_terminal_replay(row(str(queue_root / "other")))
    state.admission_withheld_keys = frozenset()
    assert not worker._entry_waits_for_terminal_replay(row(""))


def test_row_whose_directory_cannot_be_resolved_is_withheld_while_any_is(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    state = worker.replay_state
    state.admission_withheld_keys = frozenset({str(queue_root / "mol_withheld")})
    # The row's directory is spelled through a user that does not exist:
    # it has no resolvable identity.
    candidate = QueueEntry(
        queue_id="q_unresolvable",
        app_name="orca_auto_orca",
        task_id="task-unresolvable",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={"reaction_dir": "~no_such_user_orca_auto/mol_elsewhere"},
    )

    assert worker._entry_waits_for_terminal_replay(candidate)
    state.admission_withheld_keys = frozenset()
    assert not worker._entry_waits_for_terminal_replay(candidate)


def test_withheld_keys_follow_a_directory_retargeted_after_the_replay_item_was_built(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    # The item froze its key when it was built. If the path is moved and the
    # old spelling becomes a symlink, a successor submitted through that
    # spelling resolves to the new location, which must be withheld as well.
    moved = queue_root / "proj_moved" / "job"
    moved.mkdir(parents=True)
    (queue_root / "proj").symlink_to(queue_root / "proj_moved", target_is_directory=True)
    item = replay_item(queue_root, "q_retargeted", queue_root / "proj" / "job", resolved=False)
    worker.replay_state.pending_replays[item.key] = item

    assert worker._unresolved_terminal_reaction_keys() == frozenset(
        {str(queue_root / "proj" / "job"), str(moved.resolve())}
    )


def test_exited_job_awaiting_finalize_retry_withholds_its_directory_without_an_item(
    make_worker: Callable[..., OrcaQueueWorker],
    child_starter: ChildStarter,
    fake_children: FakeChildren,
    queue_root: Path,
) -> None:
    # Finalization failed before any replay item existed (for example engine
    # process recovery raised): only the retry flag and the job's own
    # directory identify what must be withheld.
    worker = make_worker(start=child_starter)
    retained_dir = queue_root / "mol_retry_only"
    unrelated = queue_root / "mol_retry_only_unrelated"
    same_dir_row = enqueue(
        queue_root,
        str(retained_dir),
        task_id="task-same-dir",
        metadata=current_orca_queue_metadata(retained_dir),
    )
    other = enqueue(
        queue_root,
        str(unrelated),
        task_id="task-unrelated",
        metadata=current_orca_queue_metadata(unrelated),
    )
    job = OrcaRunningJob(
        queue_root=worker.allowed_root,
        queue_id="q_retry_only",
        reaction_dir=str(retained_dir),
        process=fake_children.spawn(exited=1),
        admission_token="slot_retry_only",
    )
    job.terminal_finalize_pending = True
    worker._running[job.queue_id] = job

    assert worker._fill_slots() == "processed"

    assert other.queue_id in worker._running
    assert same_dir_row.queue_id not in worker._running
    assert queue_statuses(queue_root)[same_dir_row.queue_id] == QueueStatus.PENDING


def test_withheld_directories_are_logged_when_the_set_changes_not_on_every_poll(
    worker: OrcaQueueWorker, queue_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    withheld_dir = queue_root / "mol_logged_once"
    withheld_dir.mkdir()
    item = replay_item(queue_root, "q_logged_once", withheld_dir)
    state = worker.replay_state
    state.pending_replays[item.key] = item

    with caplog.at_level(logging.INFO, logger=WORKER_LOGGER):
        for _ in range(3):
            worker._fill_slots()
        state.pending_replays.clear()
        for _ in range(3):
            worker._fill_slots()

    records = [record for record in caplog.records if record.name == WORKER_LOGGER]
    assert [record.levelname for record in records] == ["WARNING", "INFO"]
    assert str(withheld_dir.resolve()) in records[0].getMessage()


def test_finalize_clears_active_engine_record_before_mark_and_release(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_active_engine_finalize"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-active-engine")
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(
        queue_root, worker.max_concurrent, entry, rxn, engine_process_state="idle"
    )
    prepare_slot_engine_process(queue_root, token)
    # The recorded engine group is gone (the child exited with it): recovery
    # must clear the active record before anything terminal is published.
    set_slot_engine_process(queue_root, token, pid=424242, pgid=424242, process_start_ticks=10101)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)
    seen_at_mark: list[tuple[str | None, int]] = []
    real_mark = replay_mod.mark_terminal_queue_entry

    def mark(*args: Any, **kwargs: Any) -> TerminalQueueMarkResult:
        current = get_slot(queue_root, token)
        seen_at_mark.append(
            (current.engine_process_state if current else None, active_slot_count(queue_root))
        )
        return real_mark(*args, **kwargs)

    with patch.object(replay_mod, "mark_terminal_queue_entry", side_effect=mark):
        worker._finalize_completed_job(entry.queue_id, job, rc=0)

    # At mark time the engine record was already idle and the slot still held.
    assert seen_at_mark == [("idle", 1)]
    assert active_slot_count(queue_root) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}


def test_finalize_does_not_publish_without_persisted_marker_after_mark(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_mark_snapshot"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-b")
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)
    real_mark = replay_mod.mark_terminal_queue_entry

    def mark_then_lose_the_row(*args: Any, **kwargs: Any) -> TerminalQueueMarkResult:
        result = real_mark(*args, **kwargs)
        # Another actor removed the row right after the mark: no durable marker remains.
        save_entries_core(queue_root, [])
        return result

    with patch.object(replay_mod, "mark_terminal_queue_entry", side_effect=mark_then_lose_the_row):
        worker._finalize_completed_job(entry.queue_id, job, rc=1)

    # Nothing was published for the vanished generation, and the slot is free.
    assert not (rxn / "job_state.json").exists()
    assert job_record(queue_root, "task-b") is None
    assert list_queue(queue_root) == []
    assert active_slot_count(queue_root) == 0


def test_stale_finalizer_does_not_resurrect_cleared_terminal_marker(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_stale_finalizer"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-stale-finalizer")
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    assert mark_failed(queue_root, entry.queue_id, error="first_owner", expected_entry=running)
    assert replay_mod.update_queue_metadata(
        queue_root, entry.queue_id, {"orca_terminal_replay": None}
    )
    [closed] = list_queue(queue_root)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=1)

    assert not (rxn / "job_state.json").exists()
    assert active_slot_count(queue_root) == 0
    assert list_queue(queue_root) == [closed]
    assert closed.metadata.get("orca_terminal_replay") is None


def test_finalize_completed_job_recovers_once_and_releases_on_benign_mark_noop(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The row was moved or removed by another actor: nothing to mark, slot freed.
    moved = queue_root / "moved"
    token = reserve_slot(
        queue_root,
        worker.max_concurrent,
        work_dir=str(moved),
        queue_id="queue-moved",
        source="queue_worker",
        state="reserved",
    )
    assert token is not None
    job = OrcaRunningJob(
        queue_root=worker.allowed_root,
        queue_id="queue-moved",
        reaction_dir=str(moved),
        process=fake_children.spawn(exited=1),
        admission_token=token,
        task_id="task-moved",
    )

    worker._finalize_completed_job(job.queue_id, job, rc=1)

    assert not moved.exists()
    assert job_record(queue_root, "task-moved") is None
    assert active_slot_count(queue_root) == 0


def test_finalize_finished_job_clears_pending_launch_left_by_dead_child(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    # The child died after fencing its launch but before publishing the
    # engine record. The gate wrapper never received its release byte, so
    # no engine ran: the finalizer must clear the pending record and
    # release the slot instead of retrying forever with reconcile paused.
    rxn = queue_root / "mol_pending_launch"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-pending-launch")
    claim_next_entry(queue_root)
    child = sleeping_child()
    token = reserve_job_slot(
        queue_root,
        worker.max_concurrent,
        entry,
        rxn,
        owner_pid=child.pid,
        engine_process_state="idle",
        engine_launch_gated=True,
    )
    assert prepare_slot_engine_process(queue_root, token) is not None
    child.kill()
    child.wait()
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=1)

    record = job_record(queue_root, "task-pending-launch")
    assert record is not None and record["status"] == "failed"
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}
    assert active_slot_count(queue_root) == 0
    assert get_slot(queue_root, token) is None


def test_finalize_finished_job_marks_completed_and_releases_slot(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_completed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    write_run_state(rxn, status=RunStatus.COMPLETED, job_id=entry.task_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id, running_job(worker, entry, rxn, fake_children.spawn(exited=0), token), rc=0
    )

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "completed"
    assert active_slot_count(queue_root) == 0


def test_finalize_finished_job_sends_parent_terminal_notification_when_unmarked(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel)
    rxn = queue_root / "mol_terminal_notify"
    rxn.mkdir()
    write_completed_run_state(rxn)
    entry = enqueue(queue_root, str(rxn), task_id="task_terminal_123")
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id, running_job(worker, entry, rxn, fake_children.spawn(exited=0), token), rc=0
    )

    record = job_record(queue_root, "task_terminal_123")
    assert record is not None and record["status"] == "completed"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1
    saved = load_state(rxn)
    assert saved is not None
    final_result = saved["final_result"]
    assert final_result is not None
    assert "finished_notification_claimed_at" in final_result
    assert "finished_notification_sent_at" not in final_result


@pytest.mark.parametrize("return_code", [0, 1])
def test_child_publishes_and_parent_releases_slot_while_terminal_sender_is_blocked(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
) -> None:
    rxn = queue_root / "terminal_delivery"
    rxn.mkdir()
    selected = rxn / "calc.inp"
    selected.write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    state = new_state(rxn, selected)
    state["job_id"] = entry.task_id
    generation = bind_report_generation(rxn, cast(dict[str, Any], state))
    selected = Path(state["selected_inp"])
    started, release = Event(), Event()
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(lifecycle_notifications, "_NOTIFICATION_SLOTS", slots)
    monkeypatch.setattr(execution_mod, "notification_channel", lambda _cfg: recording_channel)

    def on_send(message: object) -> None:
        if getattr(message, "title", "") in {"ORCA completed", "ORCA failed"}:
            started.set()
            assert release.wait(5), "test did not release terminal delivery"

    recording_channel.on_send = on_send

    class Runner:
        def run(self, inp: Path) -> SimpleNamespace:
            out = inp.with_suffix(".out")
            out.write_text("FINAL SINGLE POINT ENERGY -1.1\n****ORCA TERMINATED NORMALLY****\n")
            return SimpleNamespace(out_path=str(out), return_code=return_code)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            child = pool.submit(
                execution_mod.run_with_state,
                cfg=worker.cfg,
                reaction_dir=rxn,
                selected_inp=selected,
                runner_cls=Runner,
                runner=Runner(),
                resumed=False,
                state=state,
            )
            assert child.result(timeout=2) == return_code
            assert not started.is_set(), "completion delivery must belong to the parent"
            reports = {path: path.read_bytes() for path in generation.iterdir() if path.is_file()}
            assert generation / "machine.json" in reports
            assert active_slot_count(queue_root) == 1
            job = running_job(worker, entry, rxn, fake_children.spawn(exited=return_code), token)
            worker._running[entry.queue_id] = job
            finalizing = pool.submit(worker._check_completed_jobs)
            finalizing.result(timeout=2)
            assert started.wait(1)
            assert active_slot_count(queue_root) == 0
            assert entry.queue_id not in worker._running
            [terminal] = list_queue(queue_root)
            expected = QueueStatus.COMPLETED if return_code == 0 else QueueStatus.FAILED
            assert terminal.status == expected
            assert terminal.metadata.get("orca_terminal_replay") is None
            saved = load_state(rxn)
            assert saved is not None and saved["final_result"] is not None
            assert saved["final_result"]["finished_notification_claimed_at"]
            assert "finished_notification_sent_at" not in saved["final_result"]
            run_terminal_replay(worker, queue_root, terminal)
            run_terminal_replay(worker, queue_root, terminal)
            assert len(recording_channel.sends) == 2  # one start, one terminal
            assert {path: path.read_bytes() for path in reports} == reports

            # A late sender must not stamp or republish a successor's state.
            successor = new_state(rxn, rxn / "next.inp")
            successor["job_id"] = "successor"
            save_state(rxn, successor)
            successor_bytes = (rxn / "job_state.json").read_bytes()
        finally:
            release.set()
    assert slots.acquire(timeout=2)  # sender's finally block has completed
    slots.release()
    assert (rxn / "job_state.json").read_bytes() == successor_bytes
    assert {path: path.read_bytes() for path in reports} == reports


def test_finalize_finished_job_releases_slot_when_terminal_notification_fails(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel, sent=False)
    rxn = queue_root / "mol_terminal_notify_failed"
    rxn.mkdir()
    write_completed_run_state(rxn)
    entry = enqueue(queue_root, str(rxn), task_id="task_terminal_123")
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=0), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=0)

    record = job_record(queue_root, "task_terminal_123")
    assert record is not None and record["status"] == "completed"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1
    [completed] = list_queue(queue_root)
    assert completed.status == QueueStatus.COMPLETED
    assert completed.metadata.get("orca_terminal_replay") is None
    assert active_slot_count(queue_root) == 0
    assert entry.queue_id not in worker._running
    saved = load_state(rxn)
    assert saved is not None
    final_result = saved["final_result"]
    assert final_result is not None
    assert "finished_notification_sent_at" not in final_result


def test_terminal_notification_skips_when_state_already_marked(
    worker_cfg: AppConfig, recording_channel: RecordingChannel, queue_root: Path
) -> None:
    rxn = queue_root / "mol_terminal_already_marked"
    rxn.mkdir()
    write_completed_run_state(rxn)
    state = load_state(rxn)
    assert state is not None
    final_result = state["final_result"]
    assert final_result is not None
    final_result["finished_notification_sent_at"] = "2026-05-29T12:02:00+00:00"
    finalize_state(rxn, state, status="completed", final_result=final_result)

    assert notify_terminal_job_from_state(worker_cfg, str(rxn)) is False
    assert recording_channel.sends == []


def test_terminal_notification_rejects_previous_generation_state(
    worker_cfg: AppConfig, recording_channel: RecordingChannel, queue_root: Path
) -> None:
    rxn = queue_root / "mol_terminal_stale_generation"
    rxn.mkdir()
    write_completed_run_state(rxn)

    assert notify_terminal_job_from_state(worker_cfg, str(rxn), expected_job_id="task-b") is False
    assert recording_channel.sends == []


def test_finalize_finished_job_marks_failed_run(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_failed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id,
        running_job(worker, entry, rxn, fake_children.spawn(exited=2), token),
        rc=2,
    )

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "failed"


def test_finalize_finished_job_synthesizes_current_generation_failure_state(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel)
    rxn = queue_root / "mol_failed_before_current_state"
    rxn.mkdir()
    previous = new_state(rxn, rxn / "task-a.inp")
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
    entry = enqueue(queue_root, str(rxn), force=True, task_id="task-b")
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exited=1), token)

    worker._finalize_completed_job(entry.queue_id, job, rc=1)

    terminal = queue_row(queue_root, entry.queue_id)
    assert terminal.status == QueueStatus.FAILED
    assert terminal.metadata.get("run_id")
    assert terminal.metadata.get("run_id") != previous_run_id
    written = load_state(rxn)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["run_id"] == terminal.metadata["run_id"]
    assert written["status"] == "failed"
    final_result = written["final_result"]
    assert final_result is not None
    assert (final_result["status"], final_result["reason"]) == ("failed", "exit_code=1")
    current_record = job_record(queue_root, "task-b")
    assert current_record is not None
    assert current_record["status"] == "failed"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1

    run_terminal_replay(worker, queue_root, terminal)
    run_terminal_replay(worker, queue_root, terminal)

    assert len(recording_channel.sends) == 1
    key = (str(queue_root.resolve()), entry.queue_id)
    assert reconcile_statuses(worker)[key] == "failed"


def test_finalize_finished_job_marks_cancelled_when_cancel_requested(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_requested_before_exit"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)

    worker._finalize_completed_job(
        entry.queue_id,
        running_job(worker, entry, rxn, fake_children.spawn(exited=143), token),
        rc=143,
    )

    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert not cancelled.cancel_requested
    assert cancelled.metadata.get("run_id")
    written = load_state(rxn)
    assert written is not None
    assert written["job_id"] == entry.task_id
    assert written["run_id"] == cancelled.metadata["run_id"]
    assert written["status"] == STATUS_CANCELLED
    final_result = written["final_result"]
    assert final_result is not None
    assert final_result["status"] == STATUS_CANCELLED
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "cancelled"
    assert active_slot_count(queue_root) == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_check_cancel_requests(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    rxn = queue_root / "mol_cancel"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    child = sleeping_child()
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_cancel", task_id=None
    )

    worker._check_cancel_requests()

    assert child.poll() == -signal.SIGTERM
    assert entry.queue_id not in worker._running
    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED
    assert not cancelled.cancel_requested
    assert cancelled.metadata.get("orca_terminal_replay") is None
    written = load_state(rxn)
    assert written is not None
    assert written["status"] == STATUS_CANCELLED


def test_check_cancel_requests_retains_live_job_when_termination_fails(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_live"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    child = fake_children.spawn(stubborn=True)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_cancel_live", task_id=None
    )

    worker._check_cancel_requests()

    assert [signum for _pid, signum in fake_children.signals] == [signal.SIGTERM, signal.SIGKILL]
    assert child.poll_result is None
    assert entry.queue_id in worker._running
    [row] = list_queue(queue_root)
    assert (row.status, row.cancel_requested) == (QueueStatus.RUNNING, True)


def test_check_cancel_requests_ignores_replacement_generation(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_replacement"
    rxn.mkdir()
    selected = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    replacement = replace(running, task_id="task-b", cancel_requested=True)
    save_entries_core(queue_root, [replacement])
    child = fake_children.spawn()
    worker._running[selected.queue_id] = running_job(
        worker, selected, rxn, child, "slot-a", task_id="task-a"
    )

    worker._check_cancel_requests()

    assert fake_children.signals == []
    assert child.poll_result is None
    assert selected.queue_id in worker._running
    [durable] = list_queue(queue_root)
    assert (durable.task_id, durable.status, durable.cancel_requested) == (
        "task-b",
        QueueStatus.RUNNING,
        True,
    )


def test_cancel_releases_prepared_execution_before_terminal_side_effects(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_order"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-order")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    child = fake_children.spawn(exit_code=0)
    job = running_job(worker, entry, rxn, child, token)
    seen_at_finalize: list[tuple[int | None, QueueStatus, int]] = []
    real_side_effects = replay_mod._run_terminal_replay_side_effects

    def side_effects(cfg: AppConfig, item: TerminalReplayWorkItem) -> None:
        [row] = list_queue(queue_root)
        seen_at_finalize.append((child.poll_result, row.status, active_slot_count(queue_root)))
        real_side_effects(cfg, item)

    with patch.object(
        replay_mod, "_run_terminal_replay_side_effects", side_effect=side_effects
    ) as finalize_cancelled:
        assert worker._cancel_running_job(entry.queue_id, job) is True

    # By the time the terminal side effects ran, the child had been stopped,
    # the row was durably cancelled and execution capacity was already free.
    assert seen_at_finalize == [(0, QueueStatus.CANCELLED, 0)]
    assert active_slot_count(queue_root) == 0
    item = finalize_cancelled.call_args.args[1]
    assert (item.queue_id, item.task_id, item.state_prepared) == (
        entry.queue_id,
        entry.task_id,
        True,
    )


def test_cancel_mark_failure_retains_queue_slot_and_skips_finalization(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The row's generation moved on under the job (another submission
    # replaced task-a with task-b): the durable cancel mark must refuse.
    rxn = queue_root / "mol_cancel_mark_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    save_entries_core(queue_root, [replace(running, task_id="task-b", cancel_requested=True)])
    job = running_job(worker, entry, rxn, fake_children.spawn(exit_code=0), token)

    assert worker._cancel_running_job(entry.queue_id, job) is False

    assert active_slot_count(queue_root) == 1
    [still_running] = list_queue(queue_root)
    assert (still_running.status, still_running.task_id) == (QueueStatus.RUNNING, "task-b")
    assert not (rxn / "job_state.json").exists()


def test_cancel_mark_false_completion_retry_keeps_running_entry_slot(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_mark_false_retry"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-mark-false-retry")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), token
    )

    with (
        # Cancel marks from the worker; the completion retry marks through the
        # replay engine's terminal mark. Both refuse here.
        patch.object(queue_worker_mod, "mark_cancelled", return_value=False),
        patch.object(replay_mod, "mark_cancelled", return_value=False),
    ):
        worker._check_cancel_requests()
        assert entry.queue_id in worker._running
        assert active_slot_count(queue_root) == 1

        worker._check_completed_jobs()

    assert entry.queue_id in worker._running
    assert active_slot_count(queue_root) == 1
    [still_running] = list_queue(queue_root)
    assert (still_running.status, still_running.cancel_requested) == (QueueStatus.RUNNING, True)


def test_cancel_mark_false_releases_after_concurrent_terminal_transition(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_mark_false_terminal_race"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-terminal-race")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exited=0), token
    )
    real_mark_cancelled = replay_mod.mark_cancelled

    def terminalize_then_report_false(*args: Any, **kwargs: Any) -> bool:
        assert real_mark_cancelled(*args, **kwargs)
        return False

    # The completion path marks through the replay engine's terminal mark;
    # here the row turns terminal but the caller is told nothing changed.
    with patch.object(replay_mod, "mark_cancelled", side_effect=terminalize_then_report_false):
        worker._check_completed_jobs()

    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == (entry.task_id, STATUS_CANCELLED)
    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.CANCELLED}


def test_cancel_mark_exception_isolated_and_retried_by_completion(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_mark_exception_retry"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-mark-exception-retry")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), token
    )

    # The queue file cannot be rewritten while the cancel is processed.
    with read_only(queue_root):
        worker._check_cancel_requests()
        assert entry.queue_id in worker._running
        assert active_slot_count(queue_root) == 1
        [still_running] = list_queue(queue_root)
        assert still_running.status == QueueStatus.RUNNING

    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.CANCELLED}
    written = load_state(rxn)
    assert written is not None
    assert written["status"] == STATUS_CANCELLED


def test_cancel_state_failure_retains_slot_after_terminal_mark(
    worker: OrcaQueueWorker,
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
) -> None:
    delivered = awaited_send(recording_channel)
    rxn = queue_root / "mol_cancel_state_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-state-failure")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    job = running_job(worker, entry, rxn, fake_children.spawn(exit_code=0), token)
    worker._running[entry.queue_id] = job

    # The cancelled run state cannot be written while another instance owns the directory.
    with held_run_lock(rxn):
        assert worker._cancel_running_job(entry.queue_id, job) is False
        assert active_slot_count(queue_root) == 1
        assert job_record(queue_root, entry.task_id) is None
        assert recording_channel.sends == []
        [cancelled_entry] = list_queue(queue_root)
        assert cancelled_entry.status == QueueStatus.CANCELLED
        assert job.pending_terminal_replay is not None

    worker._check_completed_jobs()

    assert active_slot_count(queue_root) == 0
    assert entry.queue_id not in worker._running
    written = load_state(rxn)
    assert written is not None
    assert (written["job_id"], written["status"]) == (entry.task_id, STATUS_CANCELLED)
    record = job_record(queue_root, entry.task_id)
    assert record is not None and record["status"] == "cancelled"
    assert delivered.wait(1)
    assert len(recording_channel.sends) == 1


def test_cancel_side_effect_failure_withholds_its_directory_until_strict_replay(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_replay_barrier"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-a")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, 2, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), token
    )

    with held_run_lock(rxn):
        worker._check_cancel_requests()

        assert entry.queue_id in worker._running
        assert active_slot_count(queue_root) == 1
        [pending_replay] = list_queue(queue_root)
        assert pending_replay.status == QueueStatus.CANCELLED
        assert isinstance(pending_replay.metadata.get("orca_terminal_replay"), dict)
        successor = insert_pending_successor(queue_root, rxn, queue_id="q_cancel_successor")
        assert worker._unresolved_terminal_reaction_keys() == frozenset({str(rxn.resolve())})
        assert worker._fill_slots() == "idle"
        assert successor.queue_id not in worker._running

    worker._check_completed_jobs()

    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    assert queue_row(queue_root, entry.queue_id).metadata.get("orca_terminal_replay") is None
    record = job_record(queue_root, "task-cancel-a")
    assert record is not None and record["status"] == "cancelled"
    assert worker._unresolved_terminal_reaction_keys() == frozenset()


def test_cancel_recovery_failure_retains_queue_slot_and_skips_mark(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn = queue_root / "mol_cancel_recovery_failure"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id="task-cancel-recovery-failure")
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    # The engine launch is still pending under a live owner: recovery must refuse.
    token = pending_launch_slot(queue_root, worker.max_concurrent, entry, rxn)
    child = fake_children.spawn(exit_code=0)
    job = running_job(worker, entry, rxn, child, token)

    assert worker._cancel_running_job(entry.queue_id, job) is False

    assert child.poll_result == 0
    assert active_slot_count(queue_root) == 1
    [still_running] = list_queue(queue_root)
    assert (still_running.status, still_running.cancel_requested) == (QueueStatus.RUNNING, True)
    assert not (rxn / "job_state.json").exists()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_shutdown_asks_every_child_to_stop_before_escalating_on_any(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The first child ignores SIGTERM for its whole graceful period. The
    # second must already have been asked to stop before the first is
    # SIGKILLed, so the graceful periods overlap instead of adding up.
    rxn_slow = queue_root / "mol_shut_slow"
    rxn_slow.mkdir()
    rxn_fast = queue_root / "mol_shut_fast"
    rxn_fast.mkdir()
    slow_entry = enqueue(queue_root, str(rxn_slow))
    fast_entry = enqueue(queue_root, str(rxn_fast))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    slow_child = fake_children.spawn(ignores_sigterm=True)
    fast_child = fake_children.spawn()
    worker._running[slow_entry.queue_id] = running_job(
        worker, slow_entry, rxn_slow, slow_child, "slot_slow", task_id=None
    )
    worker._running[fast_entry.queue_id] = running_job(
        worker, fast_entry, rxn_fast, fast_child, "slot_fast", task_id=None
    )

    worker._shutdown_all()

    signals = fake_children.signals
    # Both children are asked to stop up front; the slow one is asked again
    # in its own turn and then escalated. The fast child's request precedes
    # that escalation, so it stopped during the slow child's graceful period.
    assert signals[:2] == [(slow_child.pid, signal.SIGTERM), (fast_child.pid, signal.SIGTERM)]
    assert signals.index((fast_child.pid, signal.SIGTERM)) < signals.index(
        (slow_child.pid, signal.SIGKILL)
    )
    assert signals[-1] == (slow_child.pid, signal.SIGKILL)
    assert slow_child.poll_result == -signal.SIGKILL
    assert fake_children.stopped(fast_child)
    assert worker._running == {}
    statuses = queue_statuses(queue_root)
    assert statuses[slow_entry.queue_id] == QueueStatus.PENDING
    assert statuses[fast_entry.queue_id] == QueueStatus.PENDING


def test_start_warns_when_concurrency_exceeds_host_cores(
    make_worker: Callable[..., OrcaQueueWorker],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = make_worker(max_concurrent=2)
    cores_per_task = int(worker.cfg.resources.max_cores_per_task)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(cores_per_task)))

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        worker._before_run()

    [record] = [r for r in caplog.records if "oversubscribe" in r.getMessage()]
    message = record.getMessage()
    assert (
        f"max_concurrent=2 x max_cores_per_task={cores_per_task} requests {2 * cores_per_task} cores"
        in message
    )
    assert f"this worker can use {cores_per_task}" in message


def test_start_stays_quiet_when_concurrency_fits_host_cores(
    make_worker: Callable[..., OrcaQueueWorker],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = make_worker(max_concurrent=2)
    cores_per_task = int(worker.cfg.resources.max_cores_per_task)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(2 * cores_per_task)))

    with caplog.at_level(logging.WARNING, logger=WORKER_LOGGER):
        worker._before_run()

    assert not [r for r in caplog.records if "oversubscribe" in r.getMessage()]


def test_shutdown_all_empty(worker: OrcaQueueWorker) -> None:
    worker._shutdown_all()
    assert len(worker._running) == 0


def test_shutdown_all_with_running(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    rxn = queue_root / "mol_shut"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    child = sleeping_child()
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_shutdown", task_id=None
    )

    worker._shutdown_all()

    assert child.poll() == -signal.SIGTERM
    assert len(worker._running) == 0
    [row] = list_queue(queue_root)
    assert (row.status, row.started_at) == (QueueStatus.PENDING, "")


def test_shutdown_does_not_requeue_a_replacement_generation(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The shutdown requeue is fenced to the generation the job was started
    # for: a row that another submission has since replaced stays untouched.
    rxn = queue_root / "mol_shut_replaced"
    rxn.mkdir()
    selected = enqueue(queue_root, str(rxn), task_id="task-a")
    running = claim_next_entry(queue_root)
    assert running is not None
    save_entries_core(queue_root, [replace(running, task_id="task-b")])
    child = fake_children.spawn()
    worker._running[selected.queue_id] = running_job(
        worker, selected, rxn, child, "slot-a", task_id="task-a"
    )

    worker._shutdown_all()

    assert fake_children.stopped(child)
    assert len(worker._running) == 0
    [durable] = list_queue(queue_root)
    assert (durable.task_id, durable.status) == ("task-b", QueueStatus.RUNNING)


def test_shutdown_finalizes_cancel_requested_job(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # A cancel pending when the worker shuts down must be finalized (terminal +
    # run state written), not requeued for resume: the shutdown path routes it
    # through the same cancel finalization as the proactive loop.
    rxn = queue_root / "mol_shut_cancel"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    save_child_state(rxn, entry.task_id, "running")
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), "slot_shut_cancel", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.CANCELLED}
    written = load_state(rxn)
    assert written is not None
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "cancelled"


def test_shutdown_finalizes_a_child_that_finished_instead_of_requeueing(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The child was still alive at the last poll but had already finished
    # ORCA and was writing its state/report; it exits 0 during the grace
    # window. Requeueing it would re-run (force rows) or unbind (plain rows)
    # a completed generation, so it takes the normal completion path.
    rxn = queue_root / "mol_shut_done"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(
        rxn,
        entry.task_id,
        "completed",
        {"status": "completed", "reason": "normal_termination", "analyzer_status": "completed"},
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), "slot_shut_done", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.COMPLETED}


def test_shutdown_finalizes_a_child_that_finished_failed(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # A failed run also reached its own conclusion (state, report and
    # notification written) and exits 1; requeueing it would re-run a
    # generation the user was already told failed.
    rxn = queue_root / "mol_shut_failed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(
        rxn,
        entry.task_id,
        "failed",
        {"status": "failed", "reason": "retry_limit_reached", "analyzer_status": "incomplete"},
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=1), "slot_shut_failed", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.FAILED}


def test_shutdown_leaves_a_self_requeued_child_pending(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The common graceful-stop path: the child was stopped mid-run, requeued
    # its own row and exited 0. The completion path must not touch the
    # pending row and must still release the slot.
    rxn = queue_root / "mol_shut_self"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)

    def requeue_own_row() -> None:
        queue_worker_mod.requeue_running_entry(queue_root, entry.queue_id)

    worker._running[entry.queue_id] = running_job(
        worker,
        entry,
        rxn,
        fake_children.spawn(exit_code=0, on_stop=requeue_own_row),
        token,
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def test_shutdown_continues_with_the_next_job_when_finalizing_one_fails(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    rxn_done = queue_root / "mol_shut_done_raise"
    rxn_done.mkdir()
    rxn_live = queue_root / "mol_shut_live"
    rxn_live.mkdir()
    done_entry = enqueue(queue_root, str(rxn_done))
    live_entry = enqueue(queue_root, str(rxn_live))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    # The finished child left a terminal run state, so it is eligible for
    # the completion path; publishing it then fails on an unreadable job index.
    save_child_state(
        rxn_done,
        done_entry.task_id,
        "completed",
        {"status": "completed", "reason": "normal_termination", "analyzer_status": "completed"},
    )
    done_token = reserve_job_slot(queue_root, worker.max_concurrent, done_entry, rxn_done)
    (queue_root / "job_locations.json").mkdir()
    done_child = fake_children.spawn(exit_code=0)
    live_child = fake_children.spawn()
    worker._running[done_entry.queue_id] = running_job(
        worker, done_entry, rxn_done, done_child, done_token, task_id=None
    )
    worker._running[live_entry.queue_id] = running_job(
        worker, live_entry, rxn_live, live_child, "slot_live", task_id=None
    )

    worker._shutdown_all()

    assert fake_children.stopped(done_child) and fake_children.stopped(live_child)
    assert len(worker._running) == 0
    statuses = queue_statuses(queue_root)
    # The finished job keeps its durable publication marker for restart,
    # but releases execution capacity; the live one is requeued for resume.
    assert statuses[done_entry.queue_id] == QueueStatus.COMPLETED
    assert terminal_replay_marker_from_entry(queue_row(queue_root, done_entry.queue_id))
    assert get_slot(queue_root, done_token) is None
    assert statuses[live_entry.queue_id] == QueueStatus.PENDING


def test_shutdown_continues_with_the_next_job_when_terminating_one_fails(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # An exception before the completion path (here from termination
    # itself) must not leave the remaining children running unsupervised.
    rxn_broken = queue_root / "mol_shut_term_raise"
    rxn_broken.mkdir()
    rxn_live = queue_root / "mol_shut_live_2"
    rxn_live.mkdir()
    broken_entry = enqueue(queue_root, str(rxn_broken))
    live_entry = enqueue(queue_root, str(rxn_live))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    broken_child = fake_children.spawn()
    live_child = fake_children.spawn()
    worker._running[broken_entry.queue_id] = running_job(
        worker, broken_entry, rxn_broken, broken_child, "slot_term_raise", task_id=None
    )
    worker._running[live_entry.queue_id] = running_job(
        worker, live_entry, rxn_live, live_child, "slot_live_2", task_id=None
    )

    def terminate(process: ManagedProcess) -> bool:
        if process is broken_child:
            raise RuntimeError("simulated termination failure")
        return terminate_process_group(process)

    with patch.object(queue_worker_mod, "terminate_process_group", side_effect=terminate):
        worker._shutdown_all()

    assert len(worker._running) == 0
    # The core last resort stopped the child whose engine-level shutdown
    # raised, and the next job was still shut down normally.
    assert fake_children.stopped(broken_child) and fake_children.stopped(live_child)
    statuses = queue_statuses(queue_root)
    assert statuses[broken_entry.queue_id] == QueueStatus.RUNNING
    assert statuses[live_entry.queue_id] == QueueStatus.PENDING


def test_shutdown_requeues_a_child_that_died_handling_the_stop(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The child caught the stop but its own requeue write raised, so it
    # exited 1 with the row still running and a non-terminal run state.
    # That is an interrupted calculation, not a failed one.
    rxn = queue_root / "mol_shut_interrupted"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(rxn, entry.task_id, "running")
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=1), "slot_shut_interrupted", task_id=None
    )

    worker._shutdown_all()

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}
    written = load_state(rxn)
    assert written is not None
    assert written["status"] == "running"


def test_shutdown_tolerates_a_failed_cancel_read_and_still_stops_the_child(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The queue read that precedes termination raised; the child must still
    # be stopped and requeued through the ordinary path (the store-level
    # requeue honors a pending cancel on its own), and the next job is
    # still shut down.
    rxn_broken = queue_root / "mol_shut_pre_raise"
    rxn_broken.mkdir()
    rxn_live = queue_root / "mol_shut_live_3"
    rxn_live.mkdir()
    broken_entry = enqueue(queue_root, str(rxn_broken))
    live_entry = enqueue(queue_root, str(rxn_live))
    claim_next_entry(queue_root)
    claim_next_entry(queue_root)
    broken_child = fake_children.spawn()
    live_child = fake_children.spawn()
    worker._running[broken_entry.queue_id] = running_job(
        worker, broken_entry, rxn_broken, broken_child, "slot_pre_raise", task_id=None
    )
    worker._running[live_entry.queue_id] = running_job(
        worker, live_entry, rxn_live, live_child, "slot_live_3", task_id=None
    )
    real_get_cancel_requested = queue_worker_mod.get_cancel_requested

    def get_cancel_requested(queue_root_arg: Path, queue_id: str, **kwargs: Any) -> bool:
        if queue_id == broken_entry.queue_id:
            raise RuntimeError("simulated queue read failure")
        return real_get_cancel_requested(queue_root_arg, queue_id, **kwargs)

    with patch.object(queue_worker_mod, "get_cancel_requested", side_effect=get_cancel_requested):
        worker._shutdown_all()

    assert len(worker._running) == 0
    # Handled at the engine layer: the ordinary termination path ran for both jobs.
    assert fake_children.stopped(broken_child) and fake_children.stopped(live_child)
    assert broken_child.poll_result == -signal.SIGTERM
    assert queue_statuses(queue_root) == {
        broken_entry.queue_id: QueueStatus.PENDING,
        live_entry.queue_id: QueueStatus.PENDING,
    }


def test_shutdown_finalizes_a_child_whose_row_is_already_terminal(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The child recorded its own rejection on the row (crash recovery) and
    # exited 1 without writing a run state; the row is terminal, so the
    # completion path (a no-op mark plus slot release) applies, not requeue.
    rxn = queue_root / "mol_shut_row_terminal"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    assert replay_mod.mark_failed(
        queue_root,
        entry.queue_id,
        error="crash recovery rejected: simulated",
        expected_task_id=entry.task_id,
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=1), "slot_row_terminal", task_id=None
    )

    worker._shutdown_all()

    assert entry.queue_id not in worker._running
    row = queue_row(queue_root, entry.queue_id)
    assert row.status == QueueStatus.FAILED
    # The completion path really ran: the replay marker was consumed and
    # the failed run state was written for the row's task.
    assert terminal_replay_marker_from_entry(row) is None
    written = load_state(rxn)
    assert written is not None
    assert (written["status"], written["job_id"]) == ("failed", entry.task_id)


def test_shutdown_tolerated_cancel_read_still_honors_a_pending_cancel(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # The claim the tolerance rests on: with the cancel flag unreadable,
    # the ordinary requeue still turns a cancel-requested row into
    # cancelled (with its replay marker) rather than pending.
    rxn = queue_root / "mol_shut_pre_raise_cancel"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    cancel(queue_root, entry.queue_id)
    token = reserve_job_slot(queue_root, worker.max_concurrent, entry, rxn)
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(), token, task_id=None
    )

    with patch.object(
        queue_worker_mod,
        "get_cancel_requested",
        side_effect=RuntimeError("simulated queue read failure"),
    ):
        worker._shutdown_all()

    assert entry.queue_id not in worker._running
    assert active_slot_count(queue_root) == 0
    row = queue_row(queue_root, entry.queue_id)
    assert (row.status, row.cancel_requested) == (QueueStatus.CANCELLED, False)
    assert terminal_replay_marker_from_entry(row) is not None


def test_shutdown_ignores_a_terminal_state_left_by_an_earlier_task(
    worker: OrcaQueueWorker, fake_children: FakeChildren, queue_root: Path
) -> None:
    # A completed state from a previous submission in the same directory
    # does not conclude the current child's run.
    rxn = queue_root / "mol_shut_stale_state"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    save_child_state(
        rxn,
        "task-from-an-earlier-submission",
        "completed",
        {"status": "completed", "reason": "normal_termination"},
    )
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, fake_children.spawn(exit_code=0), "slot_stale_state", task_id=None
    )

    worker._shutdown_all()

    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


def test_shutdown_requeues_a_child_killed_mid_run(
    worker: OrcaQueueWorker,
    sleeping_child: Callable[[], subprocess.Popen[bytes]],
    queue_root: Path,
) -> None:
    # A child killed by a signal (SIGKILL after the grace window, or a death
    # before its stop handler was installed) exits negative and keeps the
    # resume path.
    rxn = queue_root / "mol_shut_killed"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    child = sleeping_child()
    worker._running[entry.queue_id] = running_job(
        worker, entry, rxn, child, "slot_shut_killed", task_id=None
    )

    worker._shutdown_all()

    assert child.poll() == -signal.SIGTERM
    assert queue_statuses(queue_root) == {entry.queue_id: QueueStatus.PENDING}


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_orphaned_running_ignores_root_report_even_with_worker_pid_file(
    worker: OrcaQueueWorker, queue_root: Path
) -> None:
    rxn = queue_root / "mol_done"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    worker._write_pid_file()
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

    worker._reconcile_worker_state()

    queue_data = json.loads((queue_root / "queue.json").read_text(encoding="utf-8"))
    found = next(item for item in queue_data if item["queue_id"] == entry.queue_id)
    assert found["status"] == "pending"
    assert "run_id" not in found["metadata"]


# ---------------------------------------------------------------------------
# Worker lifecycle: singleton lock, startup failure, reconciliation cadence,
# child start and shutdown sweep (ported from the former generic base tests)
# ---------------------------------------------------------------------------


class _RecordingWorker(OrcaQueueWorker):
    """Records finalize/shutdown per job; the child seams stay inert."""

    def __init__(
        self, cfg: AppConfig, calls: list[tuple[str, str]], *, fail_finalize: bool = False
    ) -> None:
        super().__init__(cfg, "/tmp/config.yaml", max_concurrent=2, sleep_fn=lambda _seconds: None)
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
    reconciled: list[tuple[Path, ...]] = []
    events: list[str] = []

    def reconcile_snapshots(roots: tuple[Path, ...]) -> int:
        reconciled.append(roots)
        return 0

    def start_job(*_args: Any, **_kwargs: Any) -> bool:
        events.append("start")
        return True

    monkeypatch.setattr(
        queue_worker_mod, "snapshot_runtime_roots_for_cfg", lambda _cfg: (queue_root,)
    )
    monkeypatch.setattr(
        queue_worker_mod, "reconcile_orphaned_snapshot_generations", reconcile_snapshots
    )
    monkeypatch.setattr(
        queue_worker_mod, "finalize_queued_snapshot_intent", lambda *_args: events.append("intent")
    )
    monkeypatch.setattr(worker, "_start_job", start_job)
    monkeypatch.setattr(replay_mod, "reconcile_worker_state", lambda *_args, **_kwargs: None)

    assert worker._start_reserved(ReservedQueueEntry(queue_root, _plain_entry("q-1"), "slot"))
    assert events == ["intent", "start"]
    # Abandoned pre-enqueue snapshots are swept on the first reconcile only,
    # then at most once per interval.
    worker._reconcile_worker_state()
    worker._reconcile_worker_state()
    assert reconciled == [(queue_root,)]


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

    worker = RejectingWorker(worker_cfg, str(queue_root / "config.yaml"), max_concurrent=1)

    assert not worker._start_job(queue_root, _plain_entry("queue-reject"), admission_token="slot-1")
    assert fake_children.stopped(process)
    assert start_errors == [(queue_root, "slot-1", "admission_slot_missing")]
    assert worker._running == {}
