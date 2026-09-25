"""ORCA terminal-replay engine: preparation, publication, reconciliation and owners.

Every function here takes its state explicitly (``cfg``, ``admission_root``,
``replay_state``); the worker that owns that state lives in ``queue/worker.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    list_slots,
    reconcile_stale_slots,
    recover_orphaned_engine_slots,
)
from orca_auto.core.queue.child.process import entry_status_is_running
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.queue.worker import live_queue_slot_keys_for_slots
from orca_auto.core.statuses import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from orca_auto.orca.queue.identity import entry_matches_engine_identity

from ..config import AppConfig
from ..execution_binding import orca_execution_provenance
from ..state_reading import load_state, state_path, state_payload_job_id
from . import roots, worker_tracking
from .adapter import (
    get_cancel_requested,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    queue_entry_id,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    reconcile_orphaned_running_entries,
    update_terminal,
)
from .adapter import update_metadata as update_queue_metadata
from .entries import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    queue_entry_is_retired_workflow_owned,
)
from .models import OrcaRunningJob, OrcaWorkerReplayState, TerminalReplayWorkItem
from .run_state_replay import record_cancelled_run_state, record_failed_run_state
from .terminal_replay import (
    TERMINAL_REPLAY_METADATA_KEY,
    TerminalReplayMarkerKind,
    load_state_generation_fingerprint,
    state_fingerprint_from_payload,
    terminal_replay_is_fence_only,
    terminal_replay_marker_from_entry,
    terminal_replay_marker_kind,
)

logger = logging.getLogger(__name__)


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return roots.queue_roots(cfg)


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, QueueEntry]]:
    return [
        (root, entry)
        for root, entry in roots.queue_entries_with_roots(cfg)
        if not queue_entry_is_retired_workflow_owned(entry, root)
    ]


@dataclass(frozen=True)
class ReactionGenerationRow:
    owner: tuple[str, str]
    task_id: str
    status: str
    transitioned_from_active: bool = False
    pending_replay: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass(frozen=True)
class ArtifactGeneration:
    readable: bool
    state_job_id: str = ""


def queue_entry_by_id(queue_root: Path, target_queue_id: str) -> QueueEntry | None:
    for entry in list_queue(Path(queue_root)):
        if queue_entry_id(entry) == target_queue_id and entry_matches_engine_identity(
            entry, "orca"
        ):
            return entry
    return None


def reaction_key_for_dir(reaction_dir: str) -> str | None:
    if not reaction_dir:
        return None
    return str(Path(reaction_dir).expanduser().resolve())


def reaction_generation_key(entry: Any) -> str | None:
    return reaction_key_for_dir(queue_entry_reaction_dir(entry))


@dataclass(frozen=True)
class TerminalQueueMarkResult:
    """Stable context captured while deciding and applying a terminal queue mark."""

    marked: bool
    status: str | None
    expected_job_id: str | None
    current_entry: QueueEntry | None
    queue_root: Path
    run_id: str | None = None


def mark_terminal_queue_entry(
    queue_id: str,
    job: OrcaRunningJob,
    *,
    rc: int,
) -> TerminalQueueMarkResult:
    queue_root = job_queue_root(job)
    current = queue_entry_by_id(queue_root, queue_id)
    current_task_id = queue_entry_task_id(current) if current is not None else None
    expected_job_id = current_task_id or job.task_id
    if current is None or not entry_status_is_running(current):
        logger.info("Skipping terminal mark for %s; entry is no longer running", queue_id)
        return TerminalQueueMarkResult(False, None, expected_job_id, current, queue_root)
    if job.task_id and current_task_id and job.task_id != current_task_id:
        logger.error("Skipping terminal mark for %s; running queue generation changed", queue_id)
        return TerminalQueueMarkResult(False, None, job.task_id, current, queue_root)
    run_id = worker_tracking.get_run_id_from_state(
        job.reaction_dir, expected_job_id=expected_job_id
    )
    if get_cancel_requested(
        queue_root, queue_id, expected_entry=current, expected_task_id=expected_job_id
    ):
        status = STATUS_CANCELLED
        logger.info("Job cancelled: %s (rc=%d)", queue_id, rc)
        marked = mark_cancelled(
            queue_root, queue_id, expected_entry=current, expected_task_id=expected_job_id
        )
    elif rc == 0:
        status = STATUS_COMPLETED
        logger.info("Job completed: %s (rc=%d)", queue_id, rc)
        marked = mark_completed(
            queue_root,
            queue_id,
            run_id=run_id,
            expected_entry=current,
            expected_task_id=expected_job_id,
        )
    else:
        status = STATUS_FAILED
        logger.warning("Job failed: %s (rc=%d)", queue_id, rc)
        marked = mark_failed(
            queue_root,
            queue_id,
            error=f"exit_code={rc}",
            run_id=run_id,
            expected_entry=current,
            expected_task_id=expected_job_id,
        )
    if not marked:
        logger.info(
            "Skipping terminal finalization for %s; terminal mark did not update the entry",
            queue_id,
        )
    return TerminalQueueMarkResult(
        marked=bool(marked),
        status=status if marked else None,
        expected_job_id=expected_job_id,
        current_entry=current,
        queue_root=queue_root,
        run_id=run_id,
    )


def child_run_concluded(queue_id: str, job: OrcaRunningJob) -> bool:
    """True when the child's row is no longer running or its run state is terminal.

    A child that exits non-negatively with a still-running row and a
    non-terminal run state (for example one whose self-requeue write raised
    while it handled the stop) did not conclude; it keeps the resume path.
    """
    current = queue_entry_by_id(job_queue_root(job), queue_id)
    if current is None or normalized_entry_status(current) != STATUS_RUNNING:
        return True
    reaction_dir = str(getattr(job, "reaction_dir", "") or "").strip()
    if not reaction_dir:
        return False
    state = load_state(Path(reaction_dir).expanduser().resolve())
    expected_job_id = (
        str(getattr(job, "task_id", "") or "").strip() or queue_entry_task_id(current) or None
    )
    if not state or not worker_tracking.payload_matches_expected_job_id(state, expected_job_id):
        return False
    return str(state.get("status") or "").strip().lower() in (
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
    )


def job_queue_root(job: OrcaRunningJob) -> Path:
    return job.queue_root.expanduser().resolve()


def normalized_entry_status(entry: Any) -> str:
    raw_status = getattr(entry, "status", None)
    return str(getattr(raw_status, "value", raw_status) or "").strip().lower()


def _clear_terminal_replay_marker(item: TerminalReplayWorkItem) -> bool:
    return update_queue_metadata(
        item.queue_root,
        item.queue_id,
        {TERMINAL_REPLAY_METADATA_KEY: None},
    )


def _load_artifact_generation(reaction_key: str) -> ArtifactGeneration:
    reaction_dir = Path(reaction_key)
    state_file = state_path(reaction_dir)
    state_existed = state_file.exists()
    try:
        state = load_state(reaction_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read ORCA state generation for %s: %s", reaction_dir, exc)
        return ArtifactGeneration(readable=False)
    if (state_existed or state_file.exists()) and state is None:
        logger.warning("Failing closed on unreadable ORCA state generation: %s", state_file)
        return ArtifactGeneration(readable=False)

    return ArtifactGeneration(
        readable=True,
        state_job_id=state_payload_job_id(state),
    )


def _select_generation_owner(
    rows: list[ReactionGenerationRow],
    *,
    previous_owner: tuple[str, str] | None,
    previous_owner_was_active: bool,
    artifacts: ArtifactGeneration,
) -> tuple[str, str] | None:
    def choose(candidates: list[ReactionGenerationRow]) -> tuple[str, str] | None:
        if len(candidates) == 1:
            return candidates[0].owner
        if previous_owner is not None and any(row.owner == previous_owner for row in candidates):
            return previous_owner
        task_ids = {row.task_id for row in candidates}
        if len(task_ids) == 1 and "" not in task_ids:
            return min(row.owner for row in candidates)
        return None

    # Any live generation fences every terminal replay for this reaction dir.
    # Multiple live entries are ambiguous: timestamps and queue ordering cannot
    # prove which child owns the shared artifacts.
    active_rows = [row for row in rows if row.active]
    if active_rows:
        return choose(active_rows) if len(active_rows) == 1 else None

    # A transition observed in this poll (or for the prior selected owner) is
    # stronger evidence than a stale state left by the preceding generation.
    transition_rows = [
        row
        for row in rows
        if row.transitioned_from_active
        or (row.owner == previous_owner and previous_owner_was_active)
    ]
    if transition_rows:
        if artifacts.state_job_id:
            matching_transition = [
                row for row in transition_rows if row.task_id == artifacts.state_job_id
            ]
            selected = choose(matching_transition)
            if selected is not None:
                return selected
        return choose(transition_rows)

    if not artifacts.readable:
        return None

    # State is authoritative.  A mismatching explicit identity means the visible
    # terminal entries do not own the current reaction-dir generation.
    if artifacts.state_job_id:
        return choose([row for row in rows if row.task_id == artifacts.state_job_id])

    pending_rows = [row for row in rows if row.pending_replay]
    if pending_rows:
        selected = choose(pending_rows)
        if selected is not None:
            return selected

    if len(rows) == 1:
        return rows[0].owner

    if len({row.task_id for row in rows}) == 1 and rows[0].task_id:
        return choose(rows)
    return None


def new_terminal_replay_work_item(
    queue_root: Any,
    entry: Any,
    *,
    reaction_dir: str,
    reaction_key: str,
) -> TerminalReplayWorkItem:
    metadata = queue_entry_metadata(entry)
    snapshot = metadata.get("execution_snapshot")
    try:
        execution_provenance = (
            orca_execution_provenance(snapshot) if isinstance(snapshot, Mapping) else None
        )
    except (TypeError, ValueError):
        execution_provenance = None
    marker = terminal_replay_marker_from_entry(entry)
    selected_inp = str(
        (marker or {}).get("selected_inp")
        or metadata.get("selected_inp")
        or metadata.get("selected_input_path")
        or ""
    ).strip()
    observed_state = state_fingerprint_from_payload((marker or {}).get("observed_state"))
    if observed_state is None:
        observed_state = load_state_generation_fingerprint(
            Path(reaction_dir).expanduser().resolve()
        )
    return TerminalReplayWorkItem(
        queue_root=Path(queue_root).expanduser().resolve(),
        queue_id=queue_entry_id(entry),
        reaction_dir=reaction_dir,
        reaction_key=reaction_key,
        task_id=str((marker or {}).get("task_id") or queue_entry_task_id(entry) or "").strip(),
        observed_status=normalized_entry_status(entry),
        selected_inp=selected_inp,
        error=str((marker or {}).get("error") or getattr(entry, "error", "") or "queue_failed"),
        execution_provenance=execution_provenance,
        recorded_run_id=str(metadata.get("run_id") or "").strip(),
        observed_state=observed_state,
    )


def _prepare_terminal_replay_work_item(
    item: TerminalReplayWorkItem,
) -> TerminalReplayWorkItem:
    run_id: str | None = None
    terminal_status: str | None = item.observed_status
    reaction_text = str(item.reaction_dir or "").strip()
    if not reaction_text:
        raise RuntimeError("terminal replay has no reaction directory")
    reaction_dir = Path(reaction_text).expanduser().resolve()
    if item.observed_status == STATUS_COMPLETED:
        # Exit code zero alone is not an execution result. Resolve the actual
        # outcome and run identity before the parent returns execution capacity.
        current = load_state_generation_fingerprint(reaction_dir)
        if not current.readable or current.job_id != item.task_id or not current.terminal_status:
            raise RuntimeError("terminal run state is not ready for the completed queue entry")
        run_id = current.run_id or None
        terminal_status = current.terminal_status
    elif item.observed_status == STATUS_FAILED:
        run_id, terminal_status = record_failed_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            reason=item.error,
            observed_state=item.observed_state,
            execution_provenance=item.execution_provenance,
        )
    elif item.observed_status == STATUS_CANCELLED:
        run_id, terminal_status = record_cancelled_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            observed_state=item.observed_state,
            execution_provenance=item.execution_provenance,
        )
    resolved_status = (
        terminal_status
        if terminal_status in (STATUS_COMPLETED, STATUS_CANCELLED)
        else STATUS_FAILED
    )
    return replace(
        item,
        resolved_status=resolved_status,
        run_id=run_id,
        state_prepared=True,
    )


def _run_terminal_replay_side_effects(cfg: AppConfig, item: TerminalReplayWorkItem) -> None:
    if not str(item.reaction_dir or "").strip():
        raise RuntimeError("terminal replay has no reaction directory")
    record_upserted = worker_tracking.upsert_terminal_job_record(
        cfg,
        item.reaction_dir,
        fallback_job_id=item.task_id,
        expected_job_id=item.task_id,
    )
    if not record_upserted:
        raise RuntimeError("terminal job record artifacts are not ready")
    # The notification is advisory, as it is at submission: a delivery failure
    # is logged (redacted) by the notifier and does not retain the replay.
    # Retaining it would unnecessarily fence the next generation in this
    # directory even though execution capacity has already been returned.
    # The notifier durably claims one attempt before background delivery; it
    # never writes state after sending, when a successor may already own it.
    # A failed dispatch or delivery is one missed message, not a retry loop.
    # An exception out of the notifier is the same missed message:
    # only the record upsert above and the marker may retain the publication.
    try:
        worker_tracking.notify_terminal_job_from_state(
            cfg,
            item.reaction_dir,
            expected_job_id=item.task_id,
            expected_run_id=item.run_id or item.recorded_run_id or None,
        )
    except Exception as exc:  # noqa: BLE001 - advisory boundary
        logger.warning(
            "Terminal notification raised and is not retried: reaction_dir=%s error=%s",
            item.reaction_dir,
            type(exc).__name__,
        )


def _clear_terminal_replay_marker_or_confirm_absent(item: TerminalReplayWorkItem) -> None:
    if _clear_terminal_replay_marker(item):
        return
    current = queue_entry_by_id(item.queue_root, item.queue_id)
    if current is None or terminal_replay_marker_from_entry(current) is None:
        return
    raise RuntimeError(
        f"terminal replay marker remained after successful side effects: queue_id={item.queue_id}"
    )


def _update_terminal_replay_entry(item: TerminalReplayWorkItem) -> TerminalReplayWorkItem:
    """Bind the queue projection to the prepared run's actual outcome and identity."""
    current = queue_entry_by_id(item.queue_root, item.queue_id)
    if item.resolved_status != normalized_entry_status(current) or (
        item.run_id and item.recorded_run_id != item.run_id
    ):
        updated = update_terminal(
            item.queue_root,
            item.queue_id,
            item.resolved_status,
            run_id=item.run_id,
            expected_task_id=item.task_id,
        )
        if not updated:
            logger.info(
                "Queue entry disappeared after terminal state preparation; "
                "continuing side effects: queue_id=%s",
                item.queue_id,
            )
        item = replace(item, recorded_run_id=item.run_id or item.recorded_run_id)
    return item


def prepare_terminal_replay(
    job: OrcaRunningJob,
    item: TerminalReplayWorkItem,
) -> TerminalReplayWorkItem | None:
    """Prepare durable execution evidence while the finalizer still owns capacity.

    Keep the retry snapshot on the job until the worker transfers it to replay
    bookkeeping and releases the slot. A superseded generation has no work left.
    """
    job.pending_terminal_replay = item
    if _pending_replay_state_is_superseded(item):
        _clear_terminal_replay_marker_or_confirm_absent(item)
        return None

    prepared_item = item if item.state_prepared else _prepare_terminal_replay_work_item(item)
    job.pending_terminal_replay = prepared_item
    prepared_item = _update_terminal_replay_entry(prepared_item)
    job.pending_terminal_replay = prepared_item
    return prepared_item


def finish_terminal_replay(cfg: AppConfig, item: TerminalReplayWorkItem) -> None:
    """Publish the derived index and advisory notification, then retire the marker.

    Failure leaves the durable generation pending; it needs no execution slot.
    Normal completion and restart reconciliation use this same finish boundary.
    """
    _run_terminal_replay_side_effects(cfg, item)
    _clear_terminal_replay_marker_or_confirm_absent(item)


def _pending_replay_state_is_superseded(item: TerminalReplayWorkItem) -> bool:
    if not str(item.reaction_dir or "").strip():
        return True
    if not item.task_id:
        return True
    current = load_state_generation_fingerprint(Path(item.reaction_dir).expanduser().resolve())
    if not current.readable:
        return False
    if current.readable and current.job_id == item.task_id:
        if (
            item.observed_state is not None
            and item.observed_state.readable
            and item.observed_state.job_id
            and item.observed_state.job_id != item.task_id
            and not current.terminal_status
        ):
            return True
        expected_run_id = str(item.run_id or item.recorded_run_id or "").strip()
        if (
            not expected_run_id
            and item.observed_state is not None
            and item.observed_state.job_id == item.task_id
        ):
            expected_run_id = item.observed_state.run_id
        return bool(expected_run_id and current.run_id and current.run_id != expected_run_id)
    if item.observed_state is not None:
        if not item.observed_state.readable:
            return False
        if current != item.observed_state:
            return True
        return bool(
            current.readable
            and current.job_id
            and current.job_id != item.task_id
            and not current.terminal_status
        )
    return bool(current.job_id and current.job_id != item.task_id)


def _collect_durable_terminal_replays(
    after_entries: list[tuple[Path, QueueEntry]],
    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem],
    previously_blocked_markers: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    blocked_marker_keys: set[tuple[str, str]] = set()
    for queue_root, entry in after_entries:
        if normalized_entry_status(entry) not in TERMINAL_STATUSES:
            continue
        resolved_root = str(Path(queue_root).expanduser().resolve())
        key = (resolved_root, queue_entry_id(entry))
        marker_kind = terminal_replay_marker_kind(entry)
        if marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED:
            blocked_marker_keys.add(key)
            if key not in previously_blocked_markers:
                logger.error(
                    "ORCA terminal replay is repair-blocked by an invalid or unsupported "
                    "durable marker; retaining the queue generation from clear/force: "
                    "queue_id=%s queue_root=%s",
                    queue_entry_id(entry),
                    resolved_root,
                )
            continue
        if marker_kind is not TerminalReplayMarkerKind.VALID:
            continue
        reaction_key = reaction_generation_key(entry)
        if reaction_key is None:
            continue
        durable_item = new_terminal_replay_work_item(
            queue_root,
            entry,
            reaction_dir=queue_entry_reaction_dir(entry),
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(key)
        if not (
            isinstance(existing_item, TerminalReplayWorkItem)
            and existing_item.task_id == durable_item.task_id
            and existing_item.reaction_key == durable_item.reaction_key
        ):
            pending_replays[key] = durable_item
    return blocked_marker_keys


def _drop_superseded_terminal_replays(
    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem],
) -> set[tuple[str, str]]:
    superseded_replay_keys: set[tuple[str, str]] = set()
    for key, item in list(pending_replays.items()):
        if not _pending_replay_state_is_superseded(item):
            continue
        # Revalidate prepared snapshots before owner selection even while the
        # terminal queue row still exists.  Otherwise a newer state identity
        # can make selection return ``None`` and strand the old snapshot in
        # the pending map forever.
        try:
            _clear_terminal_replay_marker(item)
        except Exception:
            logger.exception(
                "Failed to clear superseded ORCA terminal replay marker: %s",
                item.queue_id,
            )
            continue
        pending_replays.pop(key, None)
        superseded_replay_keys.add(key)
    return superseded_replay_keys


def _select_replay_generation_owners(
    after_entries: list[tuple[Path, QueueEntry]],
    before_by_key: Mapping[tuple[str, str], Any],
    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem],
    replay_state: OrcaWorkerReplayState,
) -> tuple[set[tuple[str, str]], dict[str, tuple[str, str]]]:
    generation_rows: dict[str, list[ReactionGenerationRow]] = {}
    current_generation_keys: set[tuple[str, str]] = set()
    for queue_root, entry in after_entries:
        reaction_key = reaction_generation_key(entry)
        if reaction_key is None:
            continue
        resolved_root = str(Path(queue_root).expanduser().resolve())
        queue_id = queue_entry_id(entry)
        owner = (resolved_root, queue_id)
        before_entry = before_by_key.get(owner)
        before_status = normalized_entry_status(before_entry)
        pending_item = pending_replays.get(owner)
        pending_replay = isinstance(pending_item, TerminalReplayWorkItem)
        current_generation_keys.add(owner)
        marker_kind = terminal_replay_marker_kind(entry)
        if normalized_entry_status(entry) in TERMINAL_STATUSES and (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            # Administrative fences own no run artifact generation.  An
            # invalid/unsupported marker is likewise excluded so neither an
            # observed edge nor a stale in-memory snapshot can bypass its
            # repair-blocked boundary.
            pending_replays.pop(owner, None)
            continue
        generation_rows.setdefault(reaction_key, []).append(
            ReactionGenerationRow(
                owner=owner,
                task_id=str(queue_entry_task_id(entry) or "").strip(),
                status=normalized_entry_status(entry),
                # If state preparation failed after this generation was selected,
                # keep the observed active -> terminal edge with its immutable
                # snapshot.  The next poll otherwise sees a terminal -> terminal
                # row and can incorrectly hand ownership back to stale artifacts.
                # Once preparation succeeds, state identity becomes authoritative
                # and a mismatch correctly supersedes this pending replay.
                transitioned_from_active=(
                    before_status in ACTIVE_STATUSES
                    or (
                        isinstance(pending_item, TerminalReplayWorkItem)
                        and not pending_item.state_prepared
                    )
                ),
                pending_replay=pending_replay,
            )
        )
    for key, item in pending_replays.items():
        if key in current_generation_keys:
            continue
        generation_rows.setdefault(item.reaction_key, []).append(
            ReactionGenerationRow(
                owner=key,
                task_id=item.task_id,
                status=item.resolved_status or item.observed_status,
                pending_replay=True,
            )
        )

    previous_owners = replay_state.generation_owners
    previous_owner_active = replay_state.generation_owner_active
    latest_generation_by_reaction: dict[str, tuple[str, str]] = {}
    latest_owner_active: dict[str, bool] = {}
    for reaction_key, rows in generation_rows.items():
        previous_owner = previous_owners.get(reaction_key)
        selected_owner = _select_generation_owner(
            rows,
            previous_owner=previous_owner,
            previous_owner_was_active=bool(previous_owner_active.get(reaction_key, False)),
            artifacts=_load_artifact_generation(reaction_key),
        )
        if selected_owner is None:
            continue
        latest_generation_by_reaction[reaction_key] = selected_owner
        selected_row = next(row for row in rows if row.owner == selected_owner)
        latest_owner_active[reaction_key] = selected_row.active
    replay_state.generation_owners = latest_generation_by_reaction
    replay_state.generation_owner_active = latest_owner_active
    return current_generation_keys, latest_generation_by_reaction


def _replay_current_terminal_entries(
    cfg: AppConfig,
    after_entries: list[tuple[Path, QueueEntry]],
    before_by_key: Mapping[tuple[str, str], Any],
    previous_statuses: Mapping[tuple[str, str], str],
    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem],
    superseded_replay_keys: set[tuple[str, str]],
    latest_generation_by_reaction: Mapping[str, tuple[str, str]],
) -> dict[tuple[str, str], str]:
    after_statuses: dict[tuple[str, str], str] = {}
    for queue_root, entry in after_entries:
        queue_id = queue_entry_id(entry)
        key = (str(Path(queue_root).expanduser().resolve()), queue_id)
        status = normalized_entry_status(entry)
        after_statuses[key] = status
        if status not in TERMINAL_STATUSES:
            pending_replays.pop(key, None)
            continue
        marker_kind = terminal_replay_marker_kind(entry)
        if (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            pending_replays.pop(key, None)
            continue
        if key in superseded_replay_keys:
            # The newer generation identity is definitive.  Advance the cursor
            # to this row's terminal status so it is not reconsidered forever.
            after_statuses[key] = status
            continue
        before_entry = before_by_key.get(key)
        before_status = normalized_entry_status(before_entry)
        # Replay requires positive evidence: either a durable replay marker (or
        # an in-memory retry snapshot), an active row terminalized during this
        # reconciliation, or an active status observed by the prior poll.  A
        # terminal row first seen after startup is closed history, not proof of a
        # transition; replaying it can rewrite its state/run identity and resend
        # an old notification.
        observed_active_transition = (
            before_status in ACTIVE_STATUSES or previous_statuses.get(key) in ACTIVE_STATUSES
        )
        if key not in pending_replays and not observed_active_transition:
            continue
        reaction_dir = queue_entry_reaction_dir(entry)
        if not reaction_dir:
            continue
        reaction_key = reaction_generation_key(entry) or ""
        if latest_generation_by_reaction.get(reaction_key) != key:
            logger.debug(
                "Skipping terminal replay for superseded or ambiguous ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                queue_id,
                reaction_dir,
            )
            # Ambiguity is retryable: state/report identity may become durable on
            # the next poll without another queue status transition.
            after_statuses[key] = STATUS_RUNNING
            if reaction_key in latest_generation_by_reaction:
                pending_replays.pop(key, None)
            continue

        new_item = new_terminal_replay_work_item(
            queue_root,
            entry,
            reaction_dir=reaction_dir,
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(key)
        if isinstance(
            existing_item, TerminalReplayWorkItem
        ) and _pending_replay_state_is_superseded(existing_item):
            _clear_terminal_replay_marker(existing_item)
            pending_replays.pop(key, None)
            after_statuses[key] = status
            continue
        item = (
            existing_item
            if isinstance(existing_item, TerminalReplayWorkItem)
            and existing_item.reaction_key == new_item.reaction_key
            and existing_item.task_id == new_item.task_id
            else new_item
        )
        pending_replays[key] = item
        try:
            if not item.state_prepared:
                item = _prepare_terminal_replay_work_item(item)
                pending_replays[key] = item
            item = _update_terminal_replay_entry(item)
            pending_replays[key] = item
            finish_terminal_replay(cfg, item)
        except Exception:
            logger.exception(
                "Failed to replay terminal side effects for reconciled ORCA job %s",
                queue_id,
            )
            # Keep this transition pending so the next periodic reconcile
            # retries the idempotent terminal side effects.
            after_statuses[key] = STATUS_RUNNING
        else:
            pending_replays.pop(key, None)
            after_statuses[key] = item.resolved_status
    return after_statuses


def _retry_terminal_replays_without_queue_entries(
    cfg: AppConfig,
    pending_replays: dict[tuple[str, str], TerminalReplayWorkItem],
    current_generation_keys: set[tuple[str, str]],
    latest_generation_by_reaction: Mapping[str, tuple[str, str]],
) -> None:
    # A queue clear can remove the entry after state synthesis but before the
    # record upsert succeeds.  Retry from the immutable snapshot, including
    # a preparation that failed before the entry disappeared, but only after the
    # current owner and artifact generation are revalidated on every attempt.
    for key, item in list(pending_replays.items()):
        if key in current_generation_keys:
            continue
        if _pending_replay_state_is_superseded(item):
            logger.info(
                "Dropping terminal replay superseded by a newer ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                item.queue_id,
                item.reaction_dir,
            )
            pending_replays.pop(key, None)
            continue
        selected_owner = latest_generation_by_reaction.get(item.reaction_key)
        if selected_owner != key:
            if selected_owner is not None and selected_owner != key:
                pending_replays.pop(key, None)
            continue
        try:
            if not item.state_prepared:
                item = _prepare_terminal_replay_work_item(item)
                pending_replays[key] = item
            _run_terminal_replay_side_effects(cfg, item)
        except Exception:
            logger.exception(
                "Failed to retry terminal side effects after queue entry disappeared: %s",
                item.queue_id,
            )
        else:
            pending_replays.pop(key, None)


def reconcile_worker_state(
    cfg: AppConfig,
    *,
    admission_root: str | Path,
    replay_state: OrcaWorkerReplayState,
) -> None:
    """Reconcile orphaned running rows, then replay every observed terminal transition.

    ``replay_state`` is the worker's cursor and retry bookkeeping; it is
    mutated in place so the next pass sees this pass's outcome.
    """
    recover_orphaned_engine_slots(admission_root, strict=False)
    before_entries = queue_entries_with_roots(cfg)
    before_by_key = {
        (str(Path(root).expanduser().resolve()), queue_entry_id(entry)): entry
        for root, entry in before_entries
    }
    previous_statuses = replay_state.reconcile_statuses
    if previous_statuses is None:
        # Process startup has no observed status edge.  Treat the first queue
        # snapshot as the replay cursor instead of inventing RUNNING origins for
        # historical terminal rows.  A terminal row that really has unfinished
        # side effects remains replayable through its durable marker below, while
        # lifecycle reconciliation can still expose a real active -> terminal edge
        # between ``before_entries`` and ``after_entries`` in this same poll.
        previous_statuses = {
            key: normalized_entry_status(entry) for key, entry in before_by_key.items()
        }
    protected_queue_keys, protected_queue_ids = live_queue_slot_keys_for_slots(
        admission_root,
        list_slots_fn=list_slots,
    )
    reconcile_stale_slots(admission_root)
    for root in queue_roots(cfg):
        reconcile_orphaned_running_entries(
            root,
            ignore_worker_pid=True,
            protected_queue_keys=protected_queue_keys,
            protected_queue_ids=protected_queue_ids,
        )
    # Reconciliation can terminalize a job whose original parent died, and an
    # old child can also honor cancellation directly. Replay the normal
    # terminal side effects idempotently so job-location records and one-shot
    # notifications are not lost with the parent process.
    after_entries = queue_entries_with_roots(cfg)
    pending_replays = dict(replay_state.pending_replays)
    replay_state.blocked_marker_keys = _collect_durable_terminal_replays(
        after_entries,
        pending_replays,
        replay_state.blocked_marker_keys,
    )
    superseded_replay_keys = _drop_superseded_terminal_replays(pending_replays)

    current_generation_keys, latest_generation_by_reaction = _select_replay_generation_owners(
        after_entries,
        before_by_key,
        pending_replays,
        replay_state,
    )

    after_statuses = _replay_current_terminal_entries(
        cfg,
        after_entries,
        before_by_key,
        previous_statuses,
        pending_replays,
        superseded_replay_keys,
        latest_generation_by_reaction,
    )

    _retry_terminal_replays_without_queue_entries(
        cfg,
        pending_replays,
        current_generation_keys,
        latest_generation_by_reaction,
    )

    replay_state.pending_replays = pending_replays
    replay_state.reconcile_statuses = after_statuses


__all__ = [
    "OrcaWorkerReplayState",
    "TerminalQueueMarkResult",
    "TerminalReplayWorkItem",
    "child_run_concluded",
    "job_queue_root",
    "mark_terminal_queue_entry",
    "new_terminal_replay_work_item",
    "normalized_entry_status",
    "queue_entry_by_id",
    "reaction_generation_key",
    "reaction_key_for_dir",
    "reconcile_worker_state",
    "prepare_terminal_replay",
    "finish_terminal_replay",
]
