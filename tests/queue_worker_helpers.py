"""Shared pieces of the ORCA queue worker tests (``tests/orca/queue/test_worker_*.py``).

The worker tests drive the worker against real queue, admission and run-state
files and read the outcome back from them. Children are fakes reached only
through the two OS seams a stop goes through (``os.killpg`` and the pid probe),
or real ``sleep`` processes when the exit path itself is under test. Faults are
injected as states the worker can meet in production: a held ``run.lock`` (an
ORCA instance still owns the directory), a read-only queue root, an unreadable
job index, an engine launch pending under a live owner. Their fixtures live in
``tests/orca/queue/conftest.py``.
"""

from __future__ import annotations

import json
import signal
import subprocess
from argparse import Namespace
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any

from orca_auto.core.admission import reserve_slot
from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue.persistence import save_entries as save_entries_core
from orca_auto.core.queue.processes import ManagedProcess
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.process_tracking import RUN_LOCK_FILE_NAME
from orca_auto.orca.attempt.reporting import build_final_result
from orca_auto.orca.config import AppConfig, load_config
from orca_auto.orca.queue.adapter import list_queue
from orca_auto.orca.queue.entries import queue_entry_reaction_dir
from orca_auto.orca.queue.models import OrcaRunningJob
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.submission import create_queued_submission
from tests.conftest import (
    RecordingChannel,
    make_app_cfg,
    write_config_file,
    write_fake_orca,
    write_run_state,
)
from tests.process_helpers import FakeManagedProcess


def queued_submission(tmp_path: Path) -> tuple[AppConfig, Path, QueueEntry, Path]:
    """A real config file, loaded config, one queued job and its fake ORCA executable."""

    runs_root = tmp_path / "runs"
    job_dir = runs_root / "job"
    job_dir.mkdir(parents=True)
    selected = job_dir / "h2.inp"
    selected.write_text("! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    executable = write_fake_orca(tmp_path / "fake-orca")
    config_path = write_config_file(
        tmp_path / "orca_auto.yaml",
        make_app_cfg(runs_root, orca_executable=executable, max_concurrent=1),
    )
    cfg = load_config(str(config_path))
    queued = create_queued_submission(
        cfg, Namespace(force=False, priority=10), job_dir, selected_inp=selected
    ).entry
    return cfg, config_path, queued, executable


def current_orca_queue_metadata(
    reaction_dir: Path,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    reaction_dir.mkdir(parents=True, exist_ok=True)
    status = reaction_dir.stat()
    return {
        "reaction_dir": str(reaction_dir),
        "execution_snapshot": {
            "job_dir_identity": {
                "device": int(status.st_dev),
                "inode": int(status.st_ino),
            }
        },
        **(extra or {}),
    }


def write_completed_run_state(reaction_dir: Path) -> None:
    selected_inp = reaction_dir / "rxn.inp"
    out_path = str(reaction_dir / "rxn.out")
    write_run_state(
        reaction_dir,
        status=RunStatus.COMPLETED,
        job_id="task_terminal_123",
        selected_inp=selected_inp,
        attempts=[
            {
                "index": 1,
                "inp_path": str(selected_inp),
                "out_path": out_path,
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-05-29T12:00:00+00:00",
                "ended_at": "2026-05-29T12:01:00+00:00",
            }
        ],
        final_result=build_final_result(
            status=RunStatus.COMPLETED,
            analyzer_status=AnalyzerStatus.COMPLETED,
            reason="normal_termination",
            last_out_path=out_path,
            resumed=False,
        ),
    )


def reconcile_statuses(worker: OrcaQueueWorker) -> dict[str, str]:
    statuses = worker.replay_state.reconcile_statuses
    assert statuses is not None
    return statuses


def run_terminal_replay(worker: OrcaQueueWorker, entry: QueueEntry) -> None:
    """One real recovery pass of ``worker`` over its queue, whose only row is *entry*.

    Nothing is patched: the pass also runs the slot and orphan steps, so the
    test's queue and admission files must hold exactly what it set up.
    """
    assert [row.queue_id for row in list_queue(worker.queue_root)] == [entry.queue_id]
    worker._reconcile_worker_state()


WORKER_LOGGER = "orca_auto.orca.queue.worker"


# ---------------------------------------------------------------------------
# Fakes: children behind the OS seams, a recording child starter
# ---------------------------------------------------------------------------


@dataclass
class SlowStoppingProcess(FakeManagedProcess):
    """A fake child still handling its SIGTERM until its parent waits on it."""

    finish_stop: Callable[[], None] | None = None

    def wait(self, timeout: float | None = None) -> int:
        finish, self.finish_stop = self.finish_stop, None
        if finish is not None:
            finish()
        return super().wait(timeout)


@dataclass
class FakeChildren:
    """Children with fake pids behind the two OS seams a stop reaches them through.

    ``terminate_process_group`` runs for real: it polls, signals the group via
    ``os.killpg``, waits and escalates. Only the kernel's answers are faked: a
    SIGTERM makes the child exit with its configured code (after its own stop
    handling, when given; a slow child finishes that only while its parent
    waits on it), a SIGKILL ends it regardless, a stubborn child ignores both,
    and the pid probe reports what the registry says.
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
        slow_stop: bool = False,
    ) -> FakeManagedProcess:
        pid = self.next_pid
        self.next_pid += 1
        process_type = SlowStoppingProcess if slow_stop else FakeManagedProcess
        process = process_type(pid=pid, poll_result=exited)
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
            if isinstance(child, SlowStoppingProcess):
                child.finish_stop = lambda: self._stop(pgid, child)
            else:
                self._stop(pgid, child)
        elif signum == signal.SIGKILL:
            child.poll_result = -signal.SIGKILL

    def _stop(self, pgid: int, child: FakeManagedProcess) -> None:
        hook = self.stop_hooks.get(pgid)
        if hook is not None:
            hook()
        child.poll_result = self.exit_codes[pgid]

    def pid_exists(self, pid: int) -> bool:
        child = self.by_pid.get(pid)
        return child is not None and child.poll_result is None

    def stopped(self, process: FakeManagedProcess) -> bool:
        return process.poll_result is not None and (process.pid, signal.SIGTERM) in self.signals


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
# Queue, admission and run-state helpers
# ---------------------------------------------------------------------------


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
        queue_root=worker.queue_root,
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


__all__ = [
    "ChildStarter",
    "FakeChildren",
    "SpawnCall",
    "StartedChild",
    "WORKER_LOGGER",
    "admission_file_identity",
    "awaited_send",
    "current_orca_queue_metadata",
    "held_run_lock",
    "insert_pending_successor",
    "job_record",
    "queue_row",
    "queue_statuses",
    "queued_submission",
    "reconcile_statuses",
    "reserve_job_slot",
    "run_terminal_replay",
    "running_job",
    "write_completed_run_state",
]
