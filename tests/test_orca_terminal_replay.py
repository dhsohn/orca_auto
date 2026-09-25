"""Tests for durable ORCA terminal replay and state reconciliation.

Every reconcile pass here runs ``replay.reconcile_worker_state`` against a real
queue file under ``queue_root``; the durable outcomes it pins are the queue row
(status, ``run_id``, replay marker), ``job_state.json``, ``job_locations.json``
and the messages the recording notification channel received.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.messaging.channel import SendResult
from orca_auto.core.queue.store import save_entries as save_entries_core
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from orca_auto.orca.config import AppConfig
from orca_auto.orca.job_locations import list_job_location_records
from orca_auto.orca.queue import replay as replay_mod
from orca_auto.orca.queue import worker_tracking as worker_tracking_mod
from orca_auto.orca.queue.adapter import (
    QUEUE_FILE_NAME,
    cancel,
    enqueue,
    list_queue,
    mark_failed,
    requeue_running_entry,
)
from orca_auto.orca.queue.models import OrcaWorkerReplayState
from orca_auto.orca.queue.run_state_replay import (
    record_cancelled_run_state as _record_cancelled_run_state,
)
from orca_auto.orca.queue.run_state_replay import (
    record_failed_run_state as _record_failed_run_state,
)
from orca_auto.orca.queue.terminal_replay import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
    StateGenerationFingerprint,
    terminal_replay_marker,
)
from orca_auto.orca.queue.worker_tracking import (
    get_run_id_from_state as _get_run_id_from_state,
)
from orca_auto.orca.run_cleanup import clear_terminal_queue_entries
from orca_auto.orca.run_lock import acquire_run_lock
from orca_auto.orca.state import finalize_state, new_state, save_state
from orca_auto.orca.state_reading import load_state, report_json_path, state_path
from orca_auto.orca.statuses import RunStatus
from tests.conftest import RecordingChannel, claim_next_entry, make_queue_entry, write_run_state
from tests.engine_artifact_helpers import orca_artifact_payload
from tests.queue_worker_helpers import reconcile_statuses as _reconcile_statuses

_NOTIFICATION_THREAD_NAME = "orca-terminal-notification"


# ---------------------------------------------------------------------------
# Real-store harness
# ---------------------------------------------------------------------------


def _replay_worker(cfg: AppConfig, admission_root: Path) -> SimpleNamespace:
    """The explicit state ``replay.reconcile_worker_state`` takes, as one object."""
    return SimpleNamespace(
        cfg=cfg, admission_root=admission_root, replay_state=OrcaWorkerReplayState()
    )


def _reconcile(worker: Any) -> None:
    replay_mod.reconcile_worker_state(
        worker.cfg, admission_root=worker.admission_root, replay_state=worker.replay_state
    )
    _wait_for_notifications()


def _wait_for_notifications() -> None:
    """Join the advisory delivery threads so ``recording_channel.sends`` is settled."""
    for thread in threading.enumerate():
        if thread.name == _NOTIFICATION_THREAD_NAME:
            thread.join(timeout=5)


def _entry(
    reaction_dir: Path,
    status: QueueStatus,
    *,
    queue_id: str = "queue-replay",
    task_id: str = "task-replay",
    metadata: dict[str, Any] | None = None,
    **fields: Any,
) -> QueueEntry:
    return make_queue_entry(
        queue_id=queue_id,
        task_id=task_id,
        reaction_dir=reaction_dir,
        status=status,
        metadata=metadata,
        **fields,
    )


def _store(root: Path, *entries: QueueEntry) -> None:
    """Persist rows exactly as another writer left them (no dedup, no marker)."""
    save_entries_core(root, list(entries))


def _row(root: Path, queue_id: str) -> QueueEntry:
    return next(entry for entry in list_queue(root) if entry.queue_id == queue_id)


def _key(root: Path, entry: QueueEntry) -> tuple[str, str]:
    return (str(root.resolve()), entry.queue_id)


def _seed_cursor(worker: Any, root: Path, entry: QueueEntry, status: str) -> None:
    """Record what the previous poll of this long-running worker saw for ``entry``."""
    statuses = dict(worker.replay_state.reconcile_statuses or {})
    statuses[_key(root, entry)] = status
    worker.replay_state.reconcile_statuses = statuses


def _claimed_at(reaction_dir: Path) -> str:
    state = load_state(reaction_dir)
    assert state is not None
    final_result = state["final_result"]
    assert final_result is not None
    return str(final_result.get("finished_notification_claimed_at") or "")


def _index_path(root: Path) -> Path:
    return root / "job_locations.json"


def _corrupt_index(root: Path) -> None:
    """Make every job-location upsert fail until ``_repair_index`` runs."""
    _index_path(root).write_text("{not a job location index", encoding="utf-8")


def _repair_index(root: Path) -> None:
    _index_path(root).unlink()


@pytest.fixture
def replay_cfg(queue_root: Path, app_cfg: Callable[..., AppConfig]) -> AppConfig:
    return app_cfg(runs_root=queue_root)


@pytest.fixture
def reaction_dir(queue_root: Path) -> Path:
    path = queue_root / "rxn"
    path.mkdir()
    return path


# ---------------------------------------------------------------------------
# get_run_id_from_state
# ---------------------------------------------------------------------------


def test_get_run_id_from_state_without_state(tmp_path: Path) -> None:
    assert _get_run_id_from_state(str(tmp_path)) is None


def test_get_run_id_from_state_with_state(tmp_path: Path) -> None:
    save_state(
        tmp_path,
        {
            "run_id": "test_run_123",
            "reaction_dir": str(tmp_path),
            "selected_inp": "",
            "status": "completed",
            "attempts": [],
            "final_result": {},
        },
    )
    assert _get_run_id_from_state(str(tmp_path)) == "test_run_123"


def test_expected_job_id_rejects_previous_generation_state(tmp_path: Path) -> None:
    save_state(
        tmp_path,
        {
            "job_id": "task-a",
            "run_id": "run-a",
            "reaction_dir": str(tmp_path),
            "selected_inp": "",
            "status": "completed",
            "attempts": [],
            "final_result": {},
        },
    )

    assert _get_run_id_from_state(str(tmp_path), expected_job_id="task-b") is None
    assert _get_run_id_from_state(str(tmp_path), expected_job_id="task-a") == "run-a"


# ---------------------------------------------------------------------------
# Startup cursor and marker admission
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "terminal_status",
    [QueueStatus.COMPLETED, QueueStatus.FAILED, QueueStatus.CANCELLED],
)
@pytest.mark.parametrize("existing_cursor", [False, True])
@pytest.mark.parametrize("replay_marker", [None, {"version": 2}])
def test_worker_does_not_replay_unobserved_terminal_entry_without_valid_marker(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
    terminal_status: QueueStatus,
    existing_cursor: bool,
    replay_marker: object,
) -> None:
    entry = _entry(
        reaction_dir,
        terminal_status,
        metadata={"run_id": "run-original", TERMINAL_REPLAY_METADATA_KEY: replay_marker},
    )
    _store(queue_root, entry)
    queue_file = queue_root / QUEUE_FILE_NAME
    queue_bytes = queue_file.read_bytes()
    worker = _replay_worker(replay_cfg, queue_root)
    if existing_cursor:
        worker.replay_state.reconcile_statuses = {
            (str(queue_root.resolve()), "other-queue"): STATUS_RUNNING
        }

    _reconcile(worker)

    # Closed history (or a repair-blocked marker) is never replayed: no state is
    # synthesized, no index row or notification is produced and the row bytes,
    # marker included, are left alone.
    assert queue_file.read_bytes() == queue_bytes
    assert not state_path(reaction_dir).exists()
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == terminal_status.value
    assert worker.replay_state.pending_replays == {}


def test_repeated_worker_startup_preserves_historical_failed_queue_bytes(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    entry = _entry(
        reaction_dir,
        QueueStatus.FAILED,
        finished_at="2026-07-14T10:51:04+00:00",
        error="retry_limit_reached",
        metadata={"run_id": "run-original", TERMINAL_REPLAY_METADATA_KEY: None},
    )
    _store(queue_root, entry)
    queue_file = queue_root / QUEUE_FILE_NAME
    queue_bytes = queue_file.read_bytes()
    queue_mtime_ns = queue_file.stat().st_mtime_ns

    for _restart in range(2):
        _reconcile(_replay_worker(replay_cfg, queue_root))

    assert queue_file.read_bytes() == queue_bytes
    assert queue_file.stat().st_mtime_ns == queue_mtime_ns
    assert not state_path(reaction_dir).exists()
    assert not report_json_path(reaction_dir).exists()
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    [preserved] = list_queue(queue_root)
    assert preserved.finished_at == entry.finished_at
    assert preserved.error == entry.error
    assert preserved.metadata["run_id"] == "run-original"


@pytest.mark.parametrize(
    ("writer", "expected_status"),
    [
        ("pending_cancel", STATUS_CANCELLED),
        ("start_like_failure", STATUS_FAILED),
        ("requeue_cancel", STATUS_CANCELLED),
        ("orphan_cancel", STATUS_CANCELLED),
    ],
)
def test_terminal_writer_marker_replays_once_after_fresh_worker_restart(
    queue_root: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
    writer: str,
    expected_status: str,
) -> None:
    reaction_dir = queue_root / writer
    reaction_dir.mkdir()
    entry = enqueue(
        queue_root,
        str(reaction_dir),
        task_id=f"task-{writer}",
        metadata={"selected_inp": str(reaction_dir / "job.inp")},
    )

    if writer == "pending_cancel":
        assert cancel(queue_root, entry.queue_id, expected_entry=entry) is not None
    else:
        running = claim_next_entry(queue_root)
        assert running is not None
        if writer == "start_like_failure":
            assert mark_failed(
                queue_root,
                entry.queue_id,
                error="worker_start_error",
                expected_entry=running,
            )
        else:
            requested = cancel(queue_root, entry.queue_id, expected_entry=running)
            assert requested is not None and requested.cancel_requested
            if writer == "requeue_cancel":
                assert requeue_running_entry(
                    queue_root,
                    entry.queue_id,
                    expected_entry=requested,
                )
            else:
                assert (
                    replay_mod.reconcile_orphaned_running_entries(
                        queue_root,
                        ignore_worker_pid=True,
                    )
                    == 1
                )

    [terminal] = list_queue(queue_root)
    assert terminal.status.value == expected_status
    assert replay_mod.terminal_replay_marker_from_entry(terminal) is not None

    _reconcile(_replay_worker(replay_cfg, queue_root))
    _reconcile(_replay_worker(replay_cfg, queue_root))

    # The durable marker made a fresh worker finish the side effects exactly
    # once: one index row, one notification, then the marker is cleared.
    [record] = list_job_location_records(queue_root)
    assert record.job_id == entry.task_id
    assert record.status == expected_status
    assert len(recording_channel.sends) == 1
    [closed] = list_queue(queue_root)
    assert replay_mod.terminal_replay_marker_from_entry(closed) is None
    state = load_state(reaction_dir)
    assert state is not None
    assert state["job_id"] == entry.task_id
    final_result = state["final_result"]
    assert final_result is not None
    assert final_result["status"] == expected_status
    assert _claimed_at(reaction_dir)


@pytest.mark.parametrize("bad_version", [None, True, 2, "1", [], {}])
def test_terminal_replay_marker_rejects_malformed_version(bad_version: object) -> None:
    entry = QueueEntry(
        queue_id="queue-corrupt-marker",
        app_name="orca_auto_orca",
        task_id="task-corrupt-marker",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.FAILED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {"version": bad_version},
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is None


@pytest.mark.parametrize("bad_observed_state", [None, [], {}, {"present": "yes"}])
def test_terminal_replay_marker_rejects_malformed_state_fingerprint(
    bad_observed_state: object,
) -> None:
    entry = QueueEntry(
        queue_id="queue-corrupt-fingerprint",
        app_name="orca_auto_orca",
        task_id="task-corrupt-fingerprint",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.FAILED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {
                "version": 1,
                "task_id": "task-corrupt-fingerprint",
                "observed_state": bad_observed_state,
            },
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is None


@pytest.mark.parametrize(
    ("marker_task_id", "marker_status"),
    [
        ("other-task", STATUS_FAILED),
        ("task-replay", STATUS_RUNNING),
        ("task-replay", ""),
    ],
)
def test_terminal_replay_marker_rejects_unbound_identity_or_nonterminal_status(
    marker_task_id: str,
    marker_status: str,
) -> None:
    entry = QueueEntry(
        queue_id="queue-invalid-binding",
        app_name="orca_auto_orca",
        task_id="task-replay",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.FAILED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {
                "version": 1,
                "task_id": marker_task_id,
                "selected_inp": "",
                "status": marker_status,
                "error": "exit_code=1",
                "observed_state": {
                    "present": False,
                    "readable": True,
                    "job_id": "",
                    "run_id": "",
                    "terminal_status": "",
                },
            },
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is None


def test_terminal_replay_marker_allows_durable_status_correction() -> None:
    entry = QueueEntry(
        queue_id="queue-corrected-status",
        app_name="orca_auto_orca",
        task_id="task-corrected-status",
        task_kind="orca_run_inp",
        engine="orca",
        status=QueueStatus.COMPLETED,
        metadata={
            "reaction_dir": "/tmp/reaction",
            "orca_terminal_replay": {
                "version": 1,
                "task_id": "task-corrected-status",
                "selected_inp": "",
                # A cancellation replay may discover an already-completed state
                # and correct the queue before it clears the original marker.
                "status": STATUS_CANCELLED,
                "error": "cancel_requested",
                "observed_state": {
                    "present": False,
                    "readable": True,
                    "job_id": "",
                    "run_id": "",
                    "terminal_status": "",
                },
            },
        },
    )

    assert replay_mod.terminal_replay_marker_from_entry(entry) is not None


@pytest.mark.parametrize("blocked_kind", ["fence_only", "malformed", "conflict"])
def test_repair_blocked_terminal_never_uses_observed_active_edge(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
    blocked_kind: str,
) -> None:
    metadata: dict[str, Any] = {}
    if blocked_kind == "fence_only":
        metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] = True
    elif blocked_kind == "malformed":
        metadata[TERMINAL_REPLAY_METADATA_KEY] = {"version": 2}
    else:
        metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] = True
        metadata[TERMINAL_REPLAY_METADATA_KEY] = terminal_replay_marker(
            reaction_dir=str(reaction_dir),
            task_id="task-replay",
            selected_inp="",
            status=STATUS_FAILED,
            error="administrative_fence",
        )
    entry = _entry(reaction_dir, QueueStatus.FAILED, metadata=metadata)
    _store(queue_root, entry)
    queue_bytes = (queue_root / QUEUE_FILE_NAME).read_bytes()
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    _reconcile(worker)

    assert (queue_root / QUEUE_FILE_NAME).read_bytes() == queue_bytes
    assert not state_path(reaction_dir).exists()
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_FAILED
    assert worker.replay_state.pending_replays == {}


def test_terminal_replay_with_empty_reaction_dir_never_resolves_workspace(
    tmp_path: Path,
) -> None:
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-empty-reaction",
        reaction_dir="",
        reaction_key="",
        task_id="task-empty-reaction",
        observed_status=STATUS_FAILED,
        selected_inp="",
        error="exit_code=1",
    )

    with pytest.raises(RuntimeError, match="no reaction directory"):
        replay_mod._prepare_terminal_replay_work_item(item)

    assert list(tmp_path.iterdir()) == []
    assert replay_mod._pending_replay_state_is_superseded(item)


# ---------------------------------------------------------------------------
# Notification is advisory; the index row is not
# ---------------------------------------------------------------------------


def test_terminal_replay_completes_when_the_notification_fails(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    # A messenger outage must cost one message, not the queue: the replay
    # completes with nothing pending, and nothing resends the notification
    # later. The slot release is pinned by the worker-level finalize test.
    recording_channel.on_send = lambda _message: SendResult(sent=False)
    write_run_state(reaction_dir, status=RunStatus.COMPLETED, job_id="task-replay")
    entry = _entry(reaction_dir, QueueStatus.COMPLETED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    _reconcile(worker)

    assert len(recording_channel.sends) == 1
    assert _claimed_at(reaction_dir)
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_COMPLETED
    assert worker.replay_state.pending_replays == {}
    [record] = list_job_location_records(queue_root)
    assert record.status == STATUS_COMPLETED

    _reconcile(worker)

    assert len(recording_channel.sends) == 1


def test_terminal_replay_completes_when_the_notifier_raises(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A notifier exception is the same missed message as a failed send: the
    # replay must not stay pending on it, and it is not retried.
    write_run_state(reaction_dir, status=RunStatus.COMPLETED, job_id="task-replay")
    entry = _entry(reaction_dir, QueueStatus.COMPLETED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)
    resolutions: list[str] = []

    def channel_resolution_fails_once(*_args: object, **_kwargs: object) -> RecordingChannel:
        resolutions.append("resolve")
        if len(resolutions) == 1:
            raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")
        return recording_channel

    # Fault injection at the messenger boundary: the product has no other way
    # to make the notifier itself raise.
    monkeypatch.setattr(worker_tracking_mod, "notification_channel", channel_resolution_fails_once)

    _reconcile(worker)

    assert resolutions == ["resolve"]
    assert recording_channel.sends == []
    assert _claimed_at(reaction_dir) == ""
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_COMPLETED
    assert worker.replay_state.pending_replays == {}
    [record] = list_job_location_records(queue_root)
    assert record.status == STATUS_COMPLETED

    _reconcile(worker)

    # A working channel on the next pass does not resend: the missed message
    # was final.
    assert resolutions == ["resolve"]
    assert recording_channel.sends == []
    assert _claimed_at(reaction_dir) == ""


def test_terminal_replay_retries_when_job_record_artifacts_are_not_ready(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    entry = _entry(reaction_dir, QueueStatus.COMPLETED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    # A completed row whose child has not published its state yet: no index
    # row can be built, so the transition stays pending and nothing is sent.
    _reconcile(worker)

    assert recording_channel.sends == []
    assert list_job_location_records(queue_root) == []
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_RUNNING
    assert _key(queue_root, entry) in worker.replay_state.pending_replays

    write_run_state(reaction_dir, status=RunStatus.COMPLETED, job_id="task-replay")
    _reconcile(worker)

    [record] = list_job_location_records(queue_root)
    assert record.status == STATUS_COMPLETED
    assert len(recording_channel.sends) == 1
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_COMPLETED
    assert worker.replay_state.pending_replays == {}


# ---------------------------------------------------------------------------
# Cancel precedence and terminal correction
# ---------------------------------------------------------------------------


def test_terminal_replay_finalizes_cancelled_state_before_side_effects(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    entry = _entry(reaction_dir, QueueStatus.CANCELLED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    _reconcile(worker)

    # The cancelled state is synthesized for the queue task first; the queue
    # row is then bound to that state's run_id, and only then do the index row
    # and the notification (claimed inside that state) follow.
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == entry.task_id
    assert written["status"] == STATUS_CANCELLED
    assert written["selected_inp"] == str(reaction_dir / "-")
    final_result = written["final_result"]
    assert final_result is not None
    assert final_result["status"] == STATUS_CANCELLED
    row = _row(queue_root, entry.queue_id)
    assert row.status is QueueStatus.CANCELLED
    assert row.metadata["run_id"] == written["run_id"]
    [record] = list_job_location_records(queue_root)
    assert record.job_id == entry.task_id
    assert record.status == STATUS_CANCELLED
    assert len(recording_channel.sends) == 1
    assert _claimed_at(reaction_dir)
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_CANCELLED


def test_terminal_replay_corrects_cancelled_queue_to_existing_completed_state(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    completed = write_run_state(reaction_dir, status=RunStatus.COMPLETED, job_id="task-replay")
    entry = _entry(reaction_dir, QueueStatus.CANCELLED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    _reconcile(worker)

    # The run finished just before the cancel landed: the real outcome wins.
    row = _row(queue_root, entry.queue_id)
    assert row.status is QueueStatus.COMPLETED
    assert row.metadata["run_id"] == completed["run_id"]
    written = load_state(reaction_dir)
    assert written is not None
    assert written["status"] == STATUS_COMPLETED
    assert written["run_id"] == completed["run_id"]
    [record] = list_job_location_records(queue_root)
    assert record.status == STATUS_COMPLETED
    assert len(recording_channel.sends) == 1
    assert _reconcile_statuses(worker)[_key(queue_root, entry)] == STATUS_COMPLETED


def test_terminal_replay_observes_pending_to_cancelled_transition(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    pending = _entry(reaction_dir, QueueStatus.PENDING)
    _store(queue_root, pending)
    worker = _replay_worker(replay_cfg, queue_root)

    _reconcile(worker)
    assert list_job_location_records(queue_root) == []
    assert not state_path(reaction_dir).exists()

    _store(queue_root, replace(pending, status=QueueStatus.CANCELLED, cancel_requested=True))
    _reconcile(worker)
    _reconcile(worker)

    # Replay the transition once, then retain the successful terminal status as
    # the long-running worker's cursor rather than duplicating side effects.
    [record] = list_job_location_records(queue_root)
    assert record.job_id == pending.task_id
    assert record.status == STATUS_CANCELLED
    assert len(recording_channel.sends) == 1
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == pending.task_id
    assert written["status"] == STATUS_CANCELLED
    assert _reconcile_statuses(worker)[_key(queue_root, pending)] == STATUS_CANCELLED


# ---------------------------------------------------------------------------
# Generation ownership
# ---------------------------------------------------------------------------


def test_terminal_replay_skips_superseded_cancelled_generation(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    old_cancelled = _entry(
        reaction_dir,
        QueueStatus.CANCELLED,
        queue_id="queue-z",
        task_id="task-a",
        enqueued_at="2099-07-09T00:00:00+00:00",
    )
    current_running = _entry(
        reaction_dir, QueueStatus.RUNNING, queue_id="queue-0", task_id="task-b"
    )
    current_state = new_state(reaction_dir, reaction_dir / "task-b.inp")
    current_state["job_id"] = "task-b"
    current_state["status"] = STATUS_RUNNING
    save_state(reaction_dir, current_state)
    _store(queue_root, old_cancelled, current_running)
    worker = _replay_worker(replay_cfg, queue_root)

    # task-b's child is alive (it holds run.lock): the old cancelled row must
    # not touch the shared reaction directory.
    with acquire_run_lock(reaction_dir):
        _reconcile(worker)
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["status"] == STATUS_RUNNING
    assert _row(queue_root, "queue-0").status is QueueStatus.RUNNING

    finalize_state(
        reaction_dir,
        current_state,
        status=STATUS_COMPLETED,
        final_result={
            "status": STATUS_COMPLETED,
            "reason": "normal_termination",
            "completed_at": "2026-07-10T00:00:00+00:00",
        },
    )
    _reconcile(worker)

    # The dead child's completed state closes task-b (row, index, notification);
    # task-a is never replayed onto task-b's artifacts.
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["status"] == STATUS_COMPLETED
    assert written["run_id"] == current_state["run_id"]
    [record] = list_job_location_records(queue_root)
    assert record.job_id == "task-b"
    assert record.status == STATUS_COMPLETED
    assert len(recording_channel.sends) == 1
    assert _claimed_at(reaction_dir)
    closed = _row(queue_root, "queue-0")
    assert closed.status is QueueStatus.COMPLETED
    assert replay_mod.terminal_replay_marker_from_entry(closed) is None
    assert _row(queue_root, "queue-z").status is QueueStatus.CANCELLED


def test_terminal_owner_switches_from_terminal_owner_to_seen_active_generation(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    active_a = _entry(
        reaction_dir,
        QueueStatus.RUNNING,
        queue_id="queue-a",
        task_id="task-a",
        enqueued_at="2026-07-10T00:00:00+00:00",
    )
    failed_b = _entry(
        reaction_dir,
        QueueStatus.FAILED,
        queue_id="queue-b",
        task_id="task-b",
        enqueued_at="2026-07-11T00:00:00+00:00",
        error="lock failed",
    )
    _store(queue_root, active_a, failed_b)
    owner_a = _key(queue_root, active_a)
    owner_b = _key(queue_root, failed_b)
    reaction_key = str(reaction_dir.resolve())
    worker = _replay_worker(replay_cfg, queue_root)
    worker.replay_state.generation_owners = {reaction_key: owner_b}
    worker.replay_state.generation_owner_active = {reaction_key: True}
    worker.replay_state.reconcile_statuses = {owner_a: STATUS_RUNNING, owner_b: STATUS_RUNNING}
    queue_bytes = (queue_root / QUEUE_FILE_NAME).read_bytes()

    with acquire_run_lock(reaction_dir):
        _reconcile(worker)

    assert worker.replay_state.generation_owners[reaction_key] == owner_a
    assert worker.replay_state.generation_owner_active[reaction_key] is True
    assert (queue_root / QUEUE_FILE_NAME).read_bytes() == queue_bytes
    assert not state_path(reaction_dir).exists()
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    assert _reconcile_statuses(worker)[owner_b] == STATUS_RUNNING


def test_terminal_owner_uses_current_state_over_future_or_blank_timestamps(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    old_cancelled = _entry(
        reaction_dir,
        QueueStatus.CANCELLED,
        queue_id="queue-old",
        task_id="task-old",
        enqueued_at="2099-07-10T00:00:00+00:00",
    )
    new_cancelled = _entry(
        reaction_dir, QueueStatus.CANCELLED, queue_id="queue-new", task_id="task-new"
    )
    state = new_state(reaction_dir, reaction_dir / "new.inp")
    state["job_id"] = new_cancelled.task_id
    save_state(reaction_dir, state)
    report_json_path(reaction_dir).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=old_cancelled.task_id,
                run_id="run-old",
                reaction_dir=str(reaction_dir),
                status=STATUS_COMPLETED,
                final_result={"status": STATUS_COMPLETED},
            )
        ),
        encoding="utf-8",
    )
    _store(queue_root, old_cancelled, new_cancelled)
    worker = _replay_worker(replay_cfg, queue_root)
    for entry in (old_cancelled, new_cancelled):
        _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)
    old_row_before = _row(queue_root, "queue-old")

    _reconcile(worker)

    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == new_cancelled.task_id
    assert written["status"] == STATUS_CANCELLED
    assert written["run_id"] == state["run_id"]
    assert _row(queue_root, "queue-new").metadata["run_id"] == state["run_id"]
    assert _row(queue_root, "queue-old") == old_row_before
    [record] = list_job_location_records(queue_root)
    assert record.job_id == new_cancelled.task_id
    assert record.status == STATUS_CANCELLED
    assert len(recording_channel.sends) == 1
    reaction_key = str(reaction_dir.resolve())
    assert worker.replay_state.generation_owners[reaction_key] == _key(queue_root, new_cancelled)
    assert _reconcile_statuses(worker)[_key(queue_root, old_cancelled)] == STATUS_RUNNING


def test_ambiguous_terminal_generations_retry_when_state_identity_appears(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    cancelled_a = _entry(
        reaction_dir,
        QueueStatus.CANCELLED,
        queue_id="queue-a",
        task_id="task-a",
        enqueued_at="2099-07-10T00:00:00+00:00",
    )
    cancelled_b = _entry(reaction_dir, QueueStatus.CANCELLED, queue_id="queue-b", task_id="task-b")
    _store(queue_root, cancelled_a, cancelled_b)
    worker = _replay_worker(replay_cfg, queue_root)
    for entry in (cancelled_a, cancelled_b):
        _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)
    row_a_before = _row(queue_root, "queue-a")

    _reconcile(worker)

    # Two terminal generations and no state identity: nobody may synthesize.
    assert not state_path(reaction_dir).exists()
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    assert all(status == STATUS_RUNNING for status in _reconcile_statuses(worker).values())

    state = new_state(reaction_dir, reaction_dir / "b.inp")
    state["job_id"] = cancelled_b.task_id
    save_state(reaction_dir, state)
    _reconcile(worker)

    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == cancelled_b.task_id
    assert written["status"] == STATUS_CANCELLED
    assert written["run_id"] == state["run_id"]
    assert _row(queue_root, "queue-b").metadata["run_id"] == state["run_id"]
    assert _row(queue_root, "queue-a") == row_a_before
    [record] = list_job_location_records(queue_root)
    assert record.job_id == cancelled_b.task_id
    assert len(recording_channel.sends) == 1


# ---------------------------------------------------------------------------
# Retry from the immutable snapshot
# ---------------------------------------------------------------------------


def test_terminal_replay_snapshot_survives_entry_disappearance(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    entry = _entry(reaction_dir, QueueStatus.CANCELLED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    # State synthesis and the run_id binding succeed; the index write fails.
    _corrupt_index(queue_root)
    _reconcile(worker)
    [pending] = worker.replay_state.pending_replays.values()
    assert pending.state_prepared
    written = load_state(reaction_dir)
    assert written is not None
    assert written["status"] == STATUS_CANCELLED
    assert _row(queue_root, entry.queue_id).metadata["run_id"] == written["run_id"]
    assert recording_channel.sends == []

    # A queue clear removes the row before the retry.
    assert clear_terminal_queue_entries(queue_root) == (1, 0)
    _repair_index(queue_root)
    _reconcile(worker)

    [record] = list_job_location_records(queue_root)
    assert record.job_id == entry.task_id
    assert record.status == STATUS_CANCELLED
    assert len(recording_channel.sends) == 1
    assert _claimed_at(reaction_dir)
    assert worker.replay_state.pending_replays == {}


def test_terminal_replay_snapshot_retries_state_preparation_after_disappearance(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    entry = _entry(reaction_dir, QueueStatus.CANCELLED)
    _store(queue_root, entry)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, entry, STATUS_RUNNING)

    # The state cannot be synthesized while the run lock is still held, and the
    # row is cleared before the lock is released.
    with acquire_run_lock(reaction_dir):
        _reconcile(worker)
        [pending] = worker.replay_state.pending_replays.values()
        assert not pending.state_prepared
        assert not state_path(reaction_dir).exists()
        assert clear_terminal_queue_entries(queue_root) == (1, 0)

    _reconcile(worker)

    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == entry.task_id
    assert written["status"] == STATUS_CANCELLED
    assert list_queue(queue_root) == []
    [record] = list_job_location_records(queue_root)
    assert record.job_id == entry.task_id
    assert record.status == STATUS_CANCELLED
    assert len(recording_channel.sends) == 1
    assert _claimed_at(reaction_dir)
    assert worker.replay_state.pending_replays == {}


def test_unprepared_terminal_replay_keeps_transition_evidence_while_entry_remains(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    running = _entry(reaction_dir, QueueStatus.RUNNING)
    stale = new_state(reaction_dir, reaction_dir / "old.inp")
    stale["job_id"] = "task-old"
    finalize_state(
        reaction_dir,
        stale,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "old-generation"},
    )
    _store(queue_root, running)
    worker = _replay_worker(replay_cfg, queue_root)

    with acquire_run_lock(reaction_dir):
        _reconcile(worker)
        # Another actor terminalized the row while the child still holds the
        # lock, so state preparation fails on this pass.
        _store(queue_root, replace(running, status=QueueStatus.CANCELLED))
        _reconcile(worker)
        [pending] = worker.replay_state.pending_replays.values()
        assert not pending.state_prepared

    _reconcile(worker)

    # The observed active -> terminal edge survived the failed preparation, so
    # the stale completed state of task-old did not hand ownership back.
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == running.task_id
    assert written["status"] == STATUS_CANCELLED
    [record] = list_job_location_records(queue_root)
    assert record.job_id == running.task_id
    assert record.status == STATUS_CANCELLED
    assert len(recording_channel.sends) == 1
    assert _claimed_at(reaction_dir)
    assert worker.replay_state.pending_replays == {}
    assert worker.replay_state.generation_owners[str(reaction_dir.resolve())] == _key(
        queue_root, running
    )


def test_prepared_terminal_replay_is_dropped_when_entry_state_is_superseded(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    running = _entry(reaction_dir, QueueStatus.RUNNING)
    current = new_state(reaction_dir, reaction_dir / "current.inp")
    current["job_id"] = running.task_id
    save_state(reaction_dir, current)
    _store(queue_root, running)
    worker = _replay_worker(replay_cfg, queue_root)

    with acquire_run_lock(reaction_dir):
        _reconcile(worker)
    _store(queue_root, replace(running, status=QueueStatus.CANCELLED))
    _corrupt_index(queue_root)
    _reconcile(worker)
    [pending] = worker.replay_state.pending_replays.values()
    assert pending.state_prepared
    prepared = load_state(reaction_dir)
    assert prepared is not None
    assert prepared["status"] == STATUS_CANCELLED
    _repair_index(queue_root)

    newer = new_state(reaction_dir, reaction_dir / "newer.inp")
    newer["job_id"] = "task-newer"
    newer["status"] = STATUS_RUNNING
    save_state(reaction_dir, newer)
    _reconcile(worker)

    # The prepared snapshot is dropped, not replayed onto the newer generation.
    assert worker.replay_state.pending_replays == {}
    assert _reconcile_statuses(worker)[_key(queue_root, running)] == STATUS_CANCELLED
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-newer"
    assert written["status"] == STATUS_RUNNING


def test_durable_terminal_replay_drops_old_finalizer_after_newer_terminal_state(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    state_a = new_state(reaction_dir, reaction_dir / "a.inp")
    state_a["job_id"] = "task-a"
    finalize_state(
        reaction_dir,
        state_a,
        status=STATUS_CANCELLED,
        final_result={"status": STATUS_CANCELLED, "reason": "cancel_requested"},
    )
    marker = terminal_replay_marker(
        reaction_dir=str(reaction_dir),
        task_id="task-a",
        selected_inp=str(reaction_dir / "a.inp"),
        status=STATUS_CANCELLED,
        error="cancel_requested",
    )
    old_entry = _entry(
        reaction_dir,
        QueueStatus.CANCELLED,
        task_id="task-a",
        metadata={TERMINAL_REPLAY_METADATA_KEY: marker},
    )
    state_b = new_state(reaction_dir, reaction_dir / "b.inp")
    state_b["job_id"] = "task-b"
    finalize_state(
        reaction_dir,
        state_b,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    state_bytes = state_path(reaction_dir).read_bytes()
    _store(queue_root, old_entry)
    worker = _replay_worker(replay_cfg, queue_root)

    _reconcile(worker)

    assert state_path(reaction_dir).read_bytes() == state_bytes
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []
    assert worker.replay_state.pending_replays == {}
    closed = _row(queue_root, old_entry.queue_id)
    assert closed.status is QueueStatus.CANCELLED
    assert replay_mod.terminal_replay_marker_from_entry(closed) is None


def test_new_active_generation_supersedes_disappeared_terminal_replay(
    queue_root: Path,
    reaction_dir: Path,
    replay_cfg: AppConfig,
    recording_channel: RecordingChannel,
) -> None:
    old_cancelled = _entry(
        reaction_dir, QueueStatus.CANCELLED, queue_id="queue-old", task_id="task-old"
    )
    _store(queue_root, old_cancelled)
    worker = _replay_worker(replay_cfg, queue_root)
    _seed_cursor(worker, queue_root, old_cancelled, STATUS_RUNNING)

    _corrupt_index(queue_root)
    _reconcile(worker)
    assert len(worker.replay_state.pending_replays) == 1
    written = load_state(reaction_dir)
    assert written is not None
    assert written["job_id"] == "task-old"
    assert written["status"] == STATUS_CANCELLED

    # The row is cleared, the index recovers, and a new generation is admitted
    # in the same directory (its child holds run.lock).
    assert clear_terminal_queue_entries(queue_root) == (1, 0)
    _repair_index(queue_root)
    new_running = _entry(
        reaction_dir, QueueStatus.RUNNING, queue_id="queue-new", task_id="task-new"
    )
    _store(queue_root, new_running)
    with acquire_run_lock(reaction_dir):
        _reconcile(worker)

    assert worker.replay_state.pending_replays == {}
    assert worker.replay_state.generation_owners[str(reaction_dir.resolve())] == _key(
        queue_root, new_running
    )
    assert list_job_location_records(queue_root) == []
    assert recording_channel.sends == []


# ---------------------------------------------------------------------------
# Terminal state writers
# ---------------------------------------------------------------------------


def test_record_cancelled_run_state_synthesizes_missing_terminal_state(tmp_path: Path) -> None:
    selected_inp = tmp_path / "job.inp"

    run_id, terminal_status = _record_cancelled_run_state(
        tmp_path,
        fallback_job_id="task-cancelled",
        selected_inp=str(selected_inp),
    )

    assert run_id
    assert terminal_status == STATUS_CANCELLED
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-cancelled"
    assert written["selected_inp"] == str(selected_inp)
    assert written["status"] == STATUS_CANCELLED
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == STATUS_CANCELLED


def test_record_cancelled_run_state_normalizes_nonterminal_final_result(tmp_path: Path) -> None:
    state = new_state(tmp_path, tmp_path / "job.inp")
    state["job_id"] = "task-cancelled"
    state["final_result"] = {"status": STATUS_RUNNING, "reason": "malformed"}
    save_state(tmp_path, state)

    run_id, terminal_status = _record_cancelled_run_state(
        tmp_path,
        fallback_job_id="task-cancelled",
        selected_inp=str(tmp_path / "job.inp"),
    )

    assert run_id == state["run_id"]
    assert terminal_status == STATUS_CANCELLED
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-cancelled"
    assert written["status"] == STATUS_CANCELLED
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == STATUS_CANCELLED
    assert written["final_result"]["reason"] == "cancel_requested"


def test_record_failed_run_state_normalizes_nonterminal_final_result(tmp_path: Path) -> None:
    state = new_state(tmp_path, tmp_path / "job.inp")
    state["job_id"] = "task-failed"
    state["final_result"] = {"status": STATUS_RUNNING, "reason": "malformed"}
    save_state(tmp_path, state)

    run_id, terminal_status = _record_failed_run_state(
        tmp_path,
        fallback_job_id="task-failed",
        selected_inp=str(tmp_path / "job.inp"),
        reason="exit_code=9",
    )

    assert run_id == state["run_id"]
    assert terminal_status == "failed"
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-failed"
    assert written["status"] == "failed"
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "failed"
    assert written["final_result"]["reason"] == "exit_code=9"


def test_terminal_state_helpers_fail_closed_on_active_generation_mismatch(
    tmp_path: Path,
) -> None:
    state = new_state(tmp_path, tmp_path / "task-b.inp")
    state["job_id"] = "task-b"
    state["status"] = STATUS_RUNNING
    save_state(tmp_path, state)
    before = state_path(tmp_path).read_bytes()

    with pytest.raises(RuntimeError, match="different active generation"):
        _record_cancelled_run_state(
            tmp_path,
            fallback_job_id="task-a",
            selected_inp=str(tmp_path / "task-a.inp"),
        )
    assert state_path(tmp_path).read_bytes() == before

    with pytest.raises(RuntimeError, match="different active generation"):
        _record_failed_run_state(
            tmp_path,
            fallback_job_id="task-a",
            selected_inp=str(tmp_path / "task-a.inp"),
            reason="exit_code=1",
        )
    assert state_path(tmp_path).read_bytes() == before


def test_terminal_state_helper_cannot_write_while_current_run_lock_is_held(
    tmp_path: Path,
) -> None:
    state = new_state(tmp_path, tmp_path / "task-a.inp")
    state["job_id"] = "task-a"
    state["status"] = STATUS_RUNNING
    save_state(tmp_path, state)
    before = state_path(tmp_path).read_bytes()

    with acquire_run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            _record_failed_run_state(
                tmp_path,
                fallback_job_id="task-a",
                selected_inp=str(tmp_path / "task-a.inp"),
                reason="exit_code=1",
            )

    assert state_path(tmp_path).read_bytes() == before


def test_terminal_state_cas_rejects_changed_terminal_fingerprint(tmp_path: Path) -> None:
    state_a = new_state(tmp_path, tmp_path / "a.inp")
    state_a["job_id"] = "task-a"
    finalize_state(
        tmp_path,
        state_a,
        status=STATUS_CANCELLED,
        final_result={"status": STATUS_CANCELLED, "reason": "cancel_requested"},
    )
    observed = replay_mod.load_state_generation_fingerprint(tmp_path)

    state_b = new_state(tmp_path, tmp_path / "b.inp")
    state_b["job_id"] = "task-b"
    finalize_state(
        tmp_path,
        state_b,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    before = state_path(tmp_path).read_bytes()

    with pytest.raises(RuntimeError, match="superseded"):
        _record_failed_run_state(
            tmp_path,
            fallback_job_id="task-a",
            selected_inp=str(tmp_path / "a.inp"),
            reason="exit_code=1",
            observed_state=observed,
        )

    assert state_path(tmp_path).read_bytes() == before
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-b"


def test_terminal_replay_keeps_marker_when_state_identity_is_unreadable(
    tmp_path: Path,
) -> None:
    observed = StateGenerationFingerprint(
        present=True,
        readable=True,
        job_id="task-old",
        run_id="run-old",
        terminal_status=STATUS_COMPLETED,
    )
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-unreadable",
        reaction_dir=str(tmp_path),
        reaction_key=str(tmp_path.resolve()),
        task_id="task-current",
        observed_status=STATUS_FAILED,
        selected_inp="",
        error="exit_code=1",
        observed_state=observed,
    )

    # The current state file is unreadable: identity cannot be judged.
    state_path(tmp_path).write_text("{unreadable job state", encoding="utf-8")
    assert replay_mod.load_state_generation_fingerprint(tmp_path) == StateGenerationFingerprint(
        present=True, readable=False
    )
    assert not replay_mod._pending_replay_state_is_superseded(item)

    # The observed fingerprint was unreadable at mark time: a readable other
    # generation now does not prove supersession either.
    other = new_state(tmp_path, tmp_path / "other.inp")
    other["job_id"] = "task-other"
    save_state(tmp_path, other)
    unreadable_observed = replace(
        item,
        observed_state=StateGenerationFingerprint(present=True, readable=False),
    )
    assert not replay_mod._pending_replay_state_is_superseded(unreadable_observed)


def test_terminal_state_cas_rejects_same_task_new_run_id(tmp_path: Path) -> None:
    first = new_state(tmp_path, tmp_path / "same.inp")
    first["job_id"] = "task-same"
    save_state(tmp_path, first)
    observed = replay_mod.load_state_generation_fingerprint(tmp_path)

    second = new_state(tmp_path, tmp_path / "same.inp")
    second["job_id"] = "task-same"
    save_state(tmp_path, second)
    before = state_path(tmp_path).read_bytes()
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-same-task",
        reaction_dir=str(tmp_path),
        reaction_key=str(tmp_path.resolve()),
        task_id="task-same",
        observed_status=STATUS_FAILED,
        selected_inp=str(tmp_path / "same.inp"),
        error="exit_code=1",
        observed_state=observed,
    )

    assert replay_mod._pending_replay_state_is_superseded(item)
    with pytest.raises(RuntimeError, match="newer run"):
        _record_failed_run_state(
            tmp_path,
            fallback_job_id="task-same",
            selected_inp=str(tmp_path / "same.inp"),
            reason="exit_code=1",
            observed_state=observed,
        )

    assert state_path(tmp_path).read_bytes() == before
    written = load_state(tmp_path)
    assert written is not None
    assert written["run_id"] == second["run_id"]


def test_terminal_state_cas_rejects_expected_task_run_after_different_observation(
    tmp_path: Path,
) -> None:
    previous = new_state(tmp_path, tmp_path / "previous.inp")
    previous["job_id"] = "task-a"
    finalize_state(
        tmp_path,
        previous,
        status=STATUS_COMPLETED,
        final_result={"status": STATUS_COMPLETED, "reason": "normal_termination"},
    )
    observed = replay_mod.load_state_generation_fingerprint(tmp_path)

    current = new_state(tmp_path, tmp_path / "current.inp")
    current["job_id"] = "task-b"
    current["status"] = STATUS_RUNNING
    save_state(tmp_path, current)
    before = state_path(tmp_path).read_bytes()
    item = replay_mod.TerminalReplayWorkItem(
        queue_root=tmp_path,
        queue_id="queue-task-b",
        reaction_dir=str(tmp_path),
        reaction_key=str(tmp_path.resolve()),
        task_id="task-b",
        observed_status=STATUS_FAILED,
        selected_inp=str(tmp_path / "current.inp"),
        error="exit_code=1",
        observed_state=observed,
    )

    assert replay_mod._pending_replay_state_is_superseded(item)
    with pytest.raises(RuntimeError, match="new run for the expected task"):
        replay_mod._prepare_terminal_replay_work_item(item)

    assert state_path(tmp_path).read_bytes() == before
    written = load_state(tmp_path)
    assert written is not None
    assert written["job_id"] == "task-b"
    assert written["run_id"] == current["run_id"]
    assert written["status"] == STATUS_RUNNING


def test_terminal_upsert_filters_previous_generation_report(
    queue_root: Path, reaction_dir: Path, replay_cfg: AppConfig
) -> None:
    selected_inp = reaction_dir / "task-b.inp"
    _record_cancelled_run_state(
        reaction_dir,
        fallback_job_id="task-b",
        selected_inp=str(selected_inp),
    )
    report_json_path(reaction_dir).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="task-a",
                run_id="run-a",
                reaction_dir=str(reaction_dir),
                status=STATUS_COMPLETED,
                final_result={"status": STATUS_COMPLETED},
            )
        ),
        encoding="utf-8",
    )

    assert worker_tracking_mod.upsert_terminal_job_record(
        replay_cfg,
        str(reaction_dir),
        fallback_job_id="task-b",
    )

    [record] = list_job_location_records(queue_root)
    assert record.job_id == "task-b"
    assert record.status == STATUS_CANCELLED


def test_record_cancelled_run_state_writes_terminal_cancelled(tmp_path: Path) -> None:
    # A cancelled run is stopped by a signal and never writes its own terminal
    # result, so the worker records a cancelled outcome on its behalf. Without it
    # the run state lingers as "running" (job never leaves the list, no notify).
    state = new_state(tmp_path, tmp_path / "job.inp")
    state["status"] = "running"
    save_state(tmp_path, state)

    run_id, terminal_status = _record_cancelled_run_state(tmp_path)

    assert run_id == state["run_id"]
    assert terminal_status == "cancelled"
    written = load_state(tmp_path)
    assert written is not None
    assert written["status"] == "cancelled"
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "cancelled"
    assert written["final_result"]["reason"] == "cancel_requested"


def test_record_cancelled_run_state_keeps_existing_terminal_result(tmp_path: Path) -> None:
    # If a real terminal outcome landed just before cancellation, don't clobber it.
    state = new_state(tmp_path, tmp_path / "job.inp")
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

    run_id, terminal_status = _record_cancelled_run_state(tmp_path)

    # The pre-existing terminal outcome is preserved and reported back so the
    # caller can reconcile the queue entry to "completed" instead of "cancelled".
    assert run_id == state["run_id"]
    assert terminal_status == "completed"
    written = load_state(tmp_path)
    assert written is not None
    assert written["final_result"] is not None
    assert written["final_result"]["status"] == "completed"


@pytest.mark.parametrize("budget", [0, 3])
@pytest.mark.parametrize("terminal", [True, False])
def test_retired_generation_is_frozen_across_terminal_replay_and_notification(
    tmp_path: Path,
    budget: int,
    terminal: bool,
    recording_channel: Any,
) -> None:
    from orca_auto.orca.queue.worker_tracking import notify_terminal_job_from_state
    from orca_auto.orca.report.publication import write_report_files
    from orca_auto.orca.state_reading import load_report_json_with_output_receipt
    from tests.conftest import make_app_cfg
    from tests.engine_artifact_helpers import bind_report_generation

    selected = tmp_path / "job.inp"
    selected.write_text("! HF\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n")
    state = new_state(tmp_path, selected)
    state["job_id"] = "retired-job"
    generation = bind_report_generation(tmp_path, cast(dict[str, Any], state))
    if terminal:
        finalize_state(
            tmp_path,
            state,
            status="failed",
            final_result={
                "status": "failed",
                "analyzer_status": "incomplete",
                "reason": "scants_recipes_exhausted",
                "last_out_path": None,
            },
        )
        write_report_files(tmp_path, state)
    else:
        state["status"] = "retrying"
        save_state(tmp_path, state)
    for state_file in (state_path(tmp_path), state_path(generation)):
        payload = json.loads(state_file.read_text())
        payload["engine_payload"]["max_retries"] = budget
        state_file.write_text(json.dumps(payload))
    machine_path = report_json_path(generation)
    if terminal:
        observation = json.loads(machine_path.read_text())
        observation["payload"]["data"]["results"]["max_retries"] = budget
        machine_path.write_text(json.dumps(observation))
        assert load_report_json_with_output_receipt(generation) is not None
    frozen = {path: path.read_bytes() for path in generation.iterdir() if path.is_file()}

    for _ in range(2):
        _record_failed_run_state(
            tmp_path, reason="unsupported_snapshot", fallback_job_id="retired-job"
        )
        current = load_state(tmp_path)
        assert current is not None
        assert current["status"] == "failed"
        notify_terminal_job_from_state(
            make_app_cfg(tmp_path), str(tmp_path), expected_job_id="retired-job"
        )
        assert {path: path.read_bytes() for path in frozen} == frozen
        assert "max_retries" not in json.loads(state_path(tmp_path).read_text())["engine_payload"]
        notified = load_state(tmp_path)
        assert notified is not None
        final_result = notified["final_result"]
        assert final_result is not None
        assert final_result["finished_notification_claimed_at"]

    if terminal:
        assert load_report_json_with_output_receipt(generation) is not None
        observation["payload"]["data"]["results"]["max_retries"] = budget + 1
        machine_path.write_text(json.dumps(observation))
        assert load_report_json_with_output_receipt(generation) is None
    else:
        assert not machine_path.exists()
