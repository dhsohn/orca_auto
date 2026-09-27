"""Terminal settlement under a fault at each step, on the live and the restart paths.

A generation that turned terminal is settled in the order mark, prepare
(terminal ``job_state.json`` and reports), bind (queue outcome and run id),
release slot, finish (location record, notification claim, marker clear).
Each case fails one step with a fault the worker can meet in production and
records what the worker keeps: the job it still supervises and the work item
on it, the replay item it holds, the directory it withholds from admission,
and the durable queue row, slot, state, location record and notification.
The worker then retries without the fault and settles the generation once.

Faults act below settlement (a held ``run.lock``, an unreadable location
index, one refused queue or slot save), so the matrix does not depend on how
settlement is split into functions. The paths are a child that exited
non-zero without a terminal state (``exit``), a cancelled running child
(``cancel``), a cancelled child that ignored SIGTERM and was SIGKILLed
(``cancel_killed``), a fresh worker replaying a row whose parent died after the
terminal mark (``restart``), and a fresh worker replaying a row the cancelled
child marked before its parent died (``cancel_restart``). No child writes a
terminal state: the parent's settlement is the one writer of the cancelled
result (ADR 0008).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from orca_auto.core.admission import admission_dir, get_slot, release_slot
from orca_auto.core.admission import persistence as admission_persistence
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.types import QueueEntry
from orca_auto.orca.queue.adapter import (
    cancel,
    enqueue,
    list_queue,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.orca.queue.models import TerminalReplayWorkItem
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.state_reading import load_state
from tests.conftest import RecordingChannel, claim_next_entry
from tests.queue_worker_helpers import (
    FakeChildren,
    held_run_lock,
    job_record,
    reserve_job_slot,
    running_job,
)

_MARKER = "orca_terminal_replay"
_TASK = "task-settle"


class Kept(NamedTuple):
    """What the worker and the durable files hold after one settlement attempt."""

    supervised: str | None  # the item on the job still in ``_running``
    replay: str | None  # the item in ``replay_state.pending_replays``
    retry: bool  # the row is in ``replay_state.retry_keys``
    withheld: bool  # the directory is withheld from admission
    marker: bool  # the row still carries its replay marker
    bound: bool  # the row's run_id is the state's run_id
    slot: bool  # the execution slot is still reserved
    state: str | None  # the root job_state: None, "unclaimed" or "claimed"
    record: bool  # the terminal location record is published
    notified: bool  # the terminal notification was sent


_SETTLED = Kept(None, None, False, False, False, True, False, "claimed", True, True)

# Items: "unprepared", "prepared" (state written, row not bound yet), "bound".
_LIVE = {
    "none": _SETTLED,
    "prepare": Kept("unprepared", None, False, True, True, False, True, None, False, False),
    "bind": Kept("prepared", None, False, True, True, False, True, "unclaimed", False, False),
    "release": Kept("bound", "bound", False, True, True, True, True, "unclaimed", False, False),
    "finish": Kept(None, "bound", False, True, True, True, False, "unclaimed", False, False),
    "clear": Kept(None, "bound", False, True, True, True, False, "claimed", True, True),
}
_RESTART = {
    "none": _SETTLED,
    "prepare": Kept(None, "unprepared", True, True, True, False, False, None, False, False),
    "bind": Kept(None, "prepared", True, True, True, False, False, "unclaimed", False, False),
    "finish": Kept(None, "bound", True, True, True, True, False, "unclaimed", False, False),
    "clear": Kept(None, "bound", True, True, True, True, False, "claimed", True, True),
}
_EXPECTED = {
    "exit": _LIVE,
    "cancel": _LIVE,
    "cancel_killed": _LIVE,
    "restart": _RESTART,
    "cancel_restart": _RESTART,
}


def _item(item: TerminalReplayWorkItem | None, status: str) -> str | None:
    if item is None:
        return None
    assert (item.observed_status, item.task_id) == (status, _TASK)
    if not item.state_prepared:
        assert item.run_id is None
        return "unprepared"
    assert item.resolved_status == status and item.run_id
    return "bound" if item.recorded_run_id == item.run_id else "prepared"


def _observe(
    worker: OrcaQueueWorker,
    entry: QueueEntry,
    token: str | None,
    status: str,
    channel: RecordingChannel,
) -> Kept:
    for thread in threading.enumerate():
        if thread.name.endswith("-notification"):
            thread.join(timeout=5)
    root = worker.queue_root
    rxn = Path(entry.metadata["reaction_dir"])
    job = worker._running.get(entry.queue_id)
    if job is not None:
        assert job.terminal_finalize_pending
    assert set(worker.replay_state.pending_replays) <= {entry.queue_id}
    assert worker.replay_state.retry_keys <= {entry.queue_id}
    withheld = worker._unresolved_terminal_reaction_keys()
    assert withheld is not None and withheld <= {str(rxn.resolve())}
    [row] = list_queue(root)
    assert row.status.value == status
    state = load_state(rxn)
    if state is not None:
        assert (state["job_id"], state["status"]) == (_TASK, status)
    claimed = state is not None and bool(
        (state.get("final_result") or {}).get("finished_notification_claimed_at")
    )
    record = job_record(root, _TASK)
    assert record is None or record["status"] == status
    assert len(channel.sends) <= 1
    return Kept(
        supervised=None if job is None else _item(job.pending_terminal_replay, status),
        replay=_item(worker.replay_state.pending_replays.get(entry.queue_id), status),
        retry=entry.queue_id in worker.replay_state.retry_keys,
        withheld=bool(withheld),
        marker=bool(row.metadata.get(_MARKER)),
        bound=state is not None and row.metadata.get("run_id") == state["run_id"],
        slot=token is not None and get_slot(admission_dir(root), token) is not None,
        state=None if state is None else ("claimed" if claimed else "unclaimed"),
        record=record is not None,
        notified=len(channel.sends) == 1,
    )


@contextmanager
def _refuse_queue_save(
    queue_id: str, refuse: Callable[[QueueEntry, QueueEntry], bool]
) -> Iterator[None]:
    """Refuse the first queue save that turns the saved row into a matching new row."""
    real = queue_store.save_entries
    refused = False

    def save(root: Any, entries: Any) -> None:
        nonlocal refused
        [old] = [row for row in queue_store.load_entries(root) if row.queue_id == queue_id]
        [new] = [row for row in entries if row.queue_id == queue_id]
        if not refused and refuse(old, new):
            refused = True
            raise OSError("queue save refused")
        real(root, entries)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(queue_store, "save_entries", save)
        yield
    assert refused


@contextmanager
def _refuse_slot_release(token: str) -> Iterator[None]:
    real = admission_persistence.save_slots
    refused = False

    def save(root: Any, slots: Any) -> None:
        nonlocal refused
        if not refused and all(slot.token != token for slot in slots):
            refused = True
            raise OSError("slot save refused")
        real(root, slots)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(admission_persistence, "save_slots", save)
        yield
    assert refused


@contextmanager
def _unreadable_index(root: Path) -> Iterator[None]:
    index = root / "job_locations.json"
    assert not index.exists()
    index.write_text("{unreadable index", encoding="utf-8")
    yield
    index.unlink()


def _binds(old: QueueEntry, new: QueueEntry) -> bool:
    return bool(new.metadata.get(_MARKER)) and new.metadata.get("run_id") != old.metadata.get(
        "run_id"
    )


def _clears(old: QueueEntry, new: QueueEntry) -> bool:
    return bool(old.metadata.get(_MARKER)) and not new.metadata.get(_MARKER)


@contextmanager
def _fault(name: str, entry: QueueEntry, root: Path, token: str | None) -> Iterator[None]:
    if name == "none":
        yield
    elif name == "prepare":
        with held_run_lock(Path(entry.metadata["reaction_dir"])):
            yield
    elif name == "bind":
        with _refuse_queue_save(entry.queue_id, _binds):
            yield
    elif name == "release":
        assert token is not None
        with _refuse_slot_release(token):
            yield
    elif name == "finish":
        with _unreadable_index(root):
            yield
    else:
        assert name == "clear"
        with _refuse_queue_save(entry.queue_id, _clears):
            yield


@pytest.mark.parametrize("fault", ["none", "prepare", "bind", "release", "finish", "clear"])
@pytest.mark.parametrize("path", ["exit", "cancel", "cancel_killed", "restart", "cancel_restart"])
def test_settlement_fault_matrix(
    make_worker: Callable[..., OrcaQueueWorker],
    fake_children: FakeChildren,
    recording_channel: RecordingChannel,
    queue_root: Path,
    path: str,
    fault: str,
) -> None:
    if path in ("restart", "cancel_restart") and fault == "release":
        pytest.skip("restart replay holds no execution slot")
    status = "failed" if path in ("exit", "restart") else "cancelled"
    worker = make_worker(max_concurrent=1)
    rxn = queue_root / "job"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn), task_id=_TASK)
    running = claim_next_entry(queue_root)
    assert running is not None
    token: str | None = reserve_job_slot(admission_dir(queue_root), 1, entry, rxn)
    assert token is not None
    if path == "restart":
        # The parent marked the row and died; its slot went with it.
        assert mark_failed(queue_root, entry.queue_id, error="exit_code=1", expected_entry=running)
        assert release_slot(admission_dir(queue_root), token)
        token = None
    elif path == "cancel_restart":
        # The cancelled child marked its row and exited without a terminal
        # state; the parent died before settling it.
        cancel(queue_root, entry.queue_id)
        assert requeue_running_entry(queue_root, entry.queue_id, expected_entry=running)
        assert release_slot(admission_dir(queue_root), token)
        token = None
    else:
        child = fake_children.spawn(
            exited=1 if path == "exit" else None, ignores_sigterm=path == "cancel_killed"
        )
        worker._running[entry.queue_id] = running_job(worker, entry, rxn, child, token)
        if path != "exit":
            cancel(queue_root, entry.queue_id)

    with _fault(fault, entry, queue_root, token):
        if path == "exit":
            worker._check_completed_jobs()
        elif path in ("cancel", "cancel_killed"):
            worker._check_cancel_requests()
        else:
            worker._reconcile_worker_state()
    if path == "cancel_killed":
        assert [signum for _pid, signum in fake_children.signals] == [
            signal.SIGTERM,
            signal.SIGKILL,
        ]

    assert _observe(worker, entry, token, status, recording_channel) == _EXPECTED[path][fault]

    # Without the fault the owner that kept the generation settles it once.
    worker._check_completed_jobs()
    worker._reconcile_worker_state()
    assert _observe(worker, entry, token, status, recording_channel) == _SETTLED
