"""The ORCA restart replay pipeline: which terminal generations to settle, and their owners.

``reconcile_terminal_replays`` is the last step of the worker's recovery pass.
It collects durable replay markers, drops superseded items, selects one owner
generation per reaction directory, and settles each observed terminal row
through ``settlement.settle``, the same steps the worker's live completion
and cancellation use. Every function takes its state explicitly (``cfg``,
``replay_state``); the worker that owns that state lives in ``queue/worker.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.queue.types import QueueEntry

from ..config import AppConfig
from . import roots, settlement
from .entries import ACTIVE_STATUSES, TERMINAL_STATUSES, queue_entry_reaction_dir
from .models import OrcaWorkerReplayState, TerminalReplayWorkItem
from .terminal_marker import (
    StateGenerationFingerprint,
    TerminalReplayMarkerKind,
    load_state_generation_fingerprint,
    terminal_replay_is_fence_only,
    terminal_replay_marker_kind,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReactionGenerationRow:
    owner: str
    task_id: str
    status: str
    transitioned_from_active: bool = False
    pending_replay: bool = False
    # Absent from the previous poll.  No row is enqueued for a reaction dir
    # while another is active or has an unfinished replay marker there, so it
    # was enqueued after every generation of its dir that poll saw had closed.
    new_since_previous_poll: bool = False

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


def _select_generation_owner(
    rows: list[ReactionGenerationRow],
    *,
    previous_owner: str | None,
    previous_owner_was_active: bool,
    artifacts: StateGenerationFingerprint,
) -> str | None:
    def choose(candidates: list[ReactionGenerationRow]) -> str | None:
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
        if artifacts.job_id:
            matching_transition = [
                row for row in transition_rows if row.task_id == artifacts.job_id
            ]
            selected = choose(matching_transition)
            if selected is not None:
                return selected
            # State of a generation enqueued since the previous poll is not
            # stale evidence; it owns the artifacts.
            newer_rows = [
                row
                for row in rows
                if row.new_since_previous_poll and row.task_id == artifacts.job_id
            ]
            if newer_rows:
                return choose(newer_rows)
        return choose(transition_rows)

    if not artifacts.readable:
        return None

    # State is authoritative.  A mismatching explicit identity means the visible
    # terminal entries do not own the current reaction-dir generation.
    if artifacts.job_id:
        return choose([row for row in rows if row.task_id == artifacts.job_id])

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


def _collect_durable_terminal_replays(
    queue_root: Path,
    after_entries: list[QueueEntry],
    pending_replays: dict[str, TerminalReplayWorkItem],
    previously_blocked_markers: set[str],
) -> set[str]:
    blocked_marker_keys: set[str] = set()
    for entry in after_entries:
        if entry.status.value not in TERMINAL_STATUSES:
            continue
        queue_id = entry.queue_id
        marker_kind = terminal_replay_marker_kind(entry)
        if marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED:
            blocked_marker_keys.add(queue_id)
            if queue_id not in previously_blocked_markers:
                logger.error(
                    "ORCA terminal replay is repair-blocked by an invalid or unsupported "
                    "durable marker; retaining the queue generation from clear/force: "
                    "queue_id=%s queue_root=%s",
                    queue_id,
                    queue_root,
                )
            continue
        if marker_kind is not TerminalReplayMarkerKind.VALID:
            continue
        reaction_key = settlement.reaction_dir_key(entry)
        if reaction_key is None:
            continue
        durable_item = settlement.new_work_item(
            queue_root,
            entry,
            reaction_dir=queue_entry_reaction_dir(entry),
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(queue_id)
        if not (
            existing_item is not None
            and existing_item.task_id == durable_item.task_id
            and existing_item.reaction_key == durable_item.reaction_key
        ):
            pending_replays[queue_id] = durable_item
    return blocked_marker_keys


def _drop_superseded_terminal_replays(
    pending_replays: dict[str, TerminalReplayWorkItem],
) -> set[str]:
    superseded_replay_keys: set[str] = set()
    for key, item in list(pending_replays.items()):
        if not settlement.is_superseded(item):
            continue
        # Revalidate prepared snapshots before owner selection even while the
        # terminal queue row still exists.  Otherwise a newer state identity
        # can make selection return ``None`` and strand the old snapshot in
        # the pending map forever.
        try:
            settlement.clear_marker(item)
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
    after_entries: list[QueueEntry],
    before_statuses: Mapping[str, str],
    previous_statuses: Mapping[str, str],
    pending_replays: dict[str, TerminalReplayWorkItem],
    replay_state: OrcaWorkerReplayState,
) -> tuple[set[str], dict[str, str], set[str]]:
    generation_rows: dict[str, list[ReactionGenerationRow]] = {}
    current_generation_keys: set[str] = set()
    for entry in after_entries:
        reaction_key = settlement.reaction_dir_key(entry)
        if reaction_key is None:
            continue
        owner = entry.queue_id
        before_status = before_statuses.get(owner, "")
        pending_item = pending_replays.get(owner)
        current_generation_keys.add(owner)
        marker_kind = terminal_replay_marker_kind(entry)
        if entry.status.value in TERMINAL_STATUSES and (
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
                task_id=entry.task_id,
                status=entry.status.value,
                # If state preparation failed after this generation was selected,
                # keep the observed active -> terminal edge with its immutable
                # snapshot.  The next poll otherwise sees a terminal -> terminal
                # row and can incorrectly hand ownership back to stale artifacts.
                # Once preparation succeeds, state identity becomes authoritative
                # and a mismatch correctly supersedes this pending replay.
                transitioned_from_active=(
                    before_status in ACTIVE_STATUSES
                    or (pending_item is not None and not pending_item.state_prepared)
                ),
                pending_replay=pending_item is not None,
                new_since_previous_poll=owner not in previous_statuses,
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
    latest_generation_by_reaction: dict[str, str] = {}
    latest_owner_active: dict[str, bool] = {}
    superseded_generation_keys: set[str] = set()
    for reaction_key, rows in generation_rows.items():
        previous_owner = previous_owners.get(reaction_key)
        artifacts = load_state_generation_fingerprint(Path(reaction_key))
        if not artifacts.readable:
            logger.warning("Failing closed on unreadable ORCA state generation: %s", reaction_key)
        selected_owner = _select_generation_owner(
            rows,
            previous_owner=previous_owner,
            previous_owner_was_active=bool(previous_owner_active.get(reaction_key, False)),
            artifacts=artifacts,
        )
        if selected_owner is None:
            continue
        latest_generation_by_reaction[reaction_key] = selected_owner
        selected_row = next(row for row in rows if row.owner == selected_owner)
        latest_owner_active[reaction_key] = selected_row.active
        if selected_row.new_since_previous_poll:
            # Every generation the previous poll saw closed before this owner
            # was enqueued, so none of them can own the artifacts again.
            superseded_generation_keys.update(
                row.owner for row in rows if not row.new_since_previous_poll
            )
    replay_state.generation_owners = latest_generation_by_reaction
    replay_state.generation_owner_active = latest_owner_active
    return current_generation_keys, latest_generation_by_reaction, superseded_generation_keys


def _replay_current_terminal_entries(
    cfg: AppConfig,
    queue_root: Path,
    after_entries: list[QueueEntry],
    before_statuses: Mapping[str, str],
    previous_statuses: Mapping[str, str],
    previous_retry_keys: set[str],
    pending_replays: dict[str, TerminalReplayWorkItem],
    superseded_replay_keys: set[str],
    latest_generation_by_reaction: Mapping[str, str],
) -> tuple[dict[str, str], set[str]]:
    """Replay each observed terminal row; return the new cursor and the rows to retry."""
    after_statuses: dict[str, str] = {}
    retry_keys: set[str] = set()
    for entry in after_entries:
        queue_id = entry.queue_id
        status = entry.status.value
        after_statuses[queue_id] = status
        if status not in TERMINAL_STATUSES:
            pending_replays.pop(queue_id, None)
            continue
        marker_kind = terminal_replay_marker_kind(entry)
        if (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            pending_replays.pop(queue_id, None)
            continue
        if queue_id in superseded_replay_keys:
            # The newer generation identity is definitive: the row is not retried.
            pending_replays.pop(queue_id, None)
            continue
        # Replay requires positive evidence: either a durable replay marker (or
        # an in-memory retry snapshot), an active row terminalized during this
        # reconciliation, an active status observed by the prior poll, or a
        # transition the prior poll observed but could not replay.  A terminal
        # row first seen after startup is closed history, not proof of a
        # transition; replaying it can rewrite its state/run identity and resend
        # an old notification.
        observed_active_transition = (
            before_statuses.get(queue_id) in ACTIVE_STATUSES
            or previous_statuses.get(queue_id) in ACTIVE_STATUSES
            or queue_id in previous_retry_keys
        )
        if queue_id not in pending_replays and not observed_active_transition:
            continue
        reaction_dir = queue_entry_reaction_dir(entry)
        if not reaction_dir:
            continue
        reaction_key = settlement.reaction_dir_key(entry) or ""
        if latest_generation_by_reaction.get(reaction_key) != queue_id:
            logger.debug(
                "Skipping terminal replay for superseded or ambiguous ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                queue_id,
                reaction_dir,
            )
            # Ambiguity is retryable: state/report identity may become durable on
            # the next poll without another queue status transition.
            retry_keys.add(queue_id)
            if reaction_key in latest_generation_by_reaction:
                pending_replays.pop(queue_id, None)
            continue

        new_item = settlement.new_work_item(
            queue_root,
            entry,
            reaction_dir=reaction_dir,
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(queue_id)
        if existing_item is not None and settlement.is_superseded(existing_item):
            settlement.clear_marker(existing_item)
            pending_replays.pop(queue_id, None)
            continue
        item = (
            existing_item
            if existing_item is not None
            and existing_item.reaction_key == new_item.reaction_key
            and existing_item.task_id == new_item.task_id
            else new_item
        )
        pending_replays[queue_id] = item
        try:
            item = settlement.settle(cfg, item, has_row=True, pending_replays=pending_replays)
        except Exception:
            logger.exception(
                "Failed to replay terminal side effects for reconciled ORCA job %s",
                queue_id,
            )
            # Keep this transition pending so the next periodic reconcile
            # retries the idempotent terminal side effects.
            retry_keys.add(queue_id)
        else:
            pending_replays.pop(queue_id, None)
            after_statuses[queue_id] = item.resolved_status
    return after_statuses, retry_keys


def _retry_terminal_replays_without_queue_entries(
    cfg: AppConfig,
    pending_replays: dict[str, TerminalReplayWorkItem],
    current_generation_keys: set[str],
    latest_generation_by_reaction: Mapping[str, str],
) -> None:
    # A queue clear can remove the entry after state synthesis but before the
    # record upsert succeeds.  Retry from the immutable snapshot, including
    # a preparation that failed before the entry disappeared, but only after the
    # current owner and artifact generation are revalidated on every attempt.
    for key, item in list(pending_replays.items()):
        if key in current_generation_keys:
            continue
        if settlement.is_superseded(item):
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
            settlement.settle(cfg, item, has_row=False, pending_replays=pending_replays)
        except Exception:
            logger.exception(
                "Failed to retry terminal side effects after queue entry disappeared: %s",
                item.queue_id,
            )
        else:
            pending_replays.pop(key, None)


def reconcile_terminal_replays(
    cfg: AppConfig,
    replay_state: OrcaWorkerReplayState,
    before_rows: list[QueueEntry],
) -> None:
    """Replay every observed terminal transition; the last step of the worker's recovery pass.

    ``before_rows`` is the queue as the pass read it before orphaned RUNNING
    rows were reconciled, so a row that reconciliation terminalized shows its
    active -> terminal edge here. ``replay_state`` is the worker's cursor and
    retry bookkeeping; it is mutated in place so the next pass sees this
    pass's outcome.
    """
    queue_root = roots.queue_root(cfg)
    before_statuses = {entry.queue_id: entry.status.value for entry in before_rows}
    # Process startup has no observed status edge.  Treat the first queue
    # snapshot as the replay cursor instead of inventing RUNNING origins for
    # historical terminal rows.  A terminal row that really has unfinished
    # side effects remains replayable through its durable marker below, while
    # lifecycle reconciliation can still expose a real active -> terminal edge
    # between ``before_rows`` and ``after_entries`` in this same poll.
    previous_statuses = (
        before_statuses
        if replay_state.reconcile_statuses is None
        else replay_state.reconcile_statuses
    )
    after_entries = roots.list_orca_rows(cfg)
    pending_replays = dict(replay_state.pending_replays)
    replay_state.blocked_marker_keys = _collect_durable_terminal_replays(
        queue_root,
        after_entries,
        pending_replays,
        replay_state.blocked_marker_keys,
    )
    superseded_replay_keys = _drop_superseded_terminal_replays(pending_replays)

    current_generation_keys, latest_generation_by_reaction, superseded_generation_keys = (
        _select_replay_generation_owners(
            after_entries,
            before_statuses,
            previous_statuses,
            pending_replays,
            replay_state,
        )
    )

    after_statuses, retry_keys = _replay_current_terminal_entries(
        cfg,
        queue_root,
        after_entries,
        before_statuses,
        previous_statuses,
        replay_state.retry_keys,
        pending_replays,
        superseded_replay_keys | superseded_generation_keys,
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
    replay_state.retry_keys = retry_keys


__all__ = ["reconcile_terminal_replays"]
