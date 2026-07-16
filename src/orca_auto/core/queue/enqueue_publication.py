"""Shared driver for the durable enqueue-publication protocol.

One committed queue row must always end up with its queued job artifact
published exactly once, no matter where the publisher crashes. Three engines
(standalone xTB-MD, workflow xtb/crest, ORCA) grew separate copies of that
protocol; this module is the single implementation they converge on. Engine
differences stay in the :class:`EnqueuePublicationSpec` — commit guards,
duplicate policy, the publish callback, snapshot-intent finalization, and
the generation comparator — while the crash-safety state machine lives here.

Protocol invariants:

- A row is enqueued with a PREPARING sync lease owned by the publishing
  process; the row is unclaimable until the lease is COMPLETE.
- Publication happens under the per-row publication lock: re-validate
  ownership, publish, then CAS the lease to COMPLETE. The COMPLETE
  short-circuit is token-verified — a COMPLETE written by another lease is
  ownership loss, never this publisher's success.
- Every failure after the durable commit parks the row as REPAIR_PENDING
  (owner 0) instead of publishing blind or failing terminally; the worker's
  pre-claim repair pass republishes it before the row can run.
- An enqueue whose commit outcome cannot be determined is reported as
  :class:`EnqueuePublicationOutcomeUnknown`, never as an ordinary failure.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from orca_auto.core.queue.generation import queue_entries_same_generation
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.store import (
    QueueAfterCommitError,
    enqueue,
    mark_failed,
    mutate_entries,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.persistence import timestamped_token

logger = logging.getLogger(__name__)

REPAIRABLE_SYNC_STATES = frozenset(
    {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }
)

_OWNERSHIP_RECONCILE_WARNING = (
    "queued record publication ownership changed; worker repair will reconcile"
)


class EnqueuePublicationOutcomeUnknown(RuntimeError):
    """The enqueue may or may not have committed; the caller must not retry blindly."""


@dataclass(frozen=True)
class EnqueuePublicationOutcome:
    entry: QueueEntry
    published: bool
    cancelled: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(kw_only=True)
class EnqueuePublicationSpec:
    queue_root: Path
    app_name: str
    task_id: str
    task_kind: str
    engine: str
    priority: int
    metadata: dict[str, Any]
    label: str
    publish: Callable[[QueueEntry], None]
    duplicate_policy: Callable[..., None] | None = None
    before_commit_fn: Callable[[], Any] | None = None
    after_commit_fn: Callable[[], Any] | None = None
    finalize_intent: Callable[[QueueEntry], None] | None = None
    on_compensated_failure: Callable[[], None] | None = None
    enqueue_fn: Callable[..., Any] | None = None
    mark_failed_fn: Callable[..., Any] | None = None
    job_dir_metadata_key: str = "job_dir"
    ambiguous_fence_metadata: dict[str, Any] | None = None
    same_generation: Callable[[QueueEntry, QueueEntry], bool] = field(
        default=queue_entries_same_generation
    )


@dataclass(frozen=True)
class RepairOutcome:
    """One repair attempt's result; ``reason`` names the branch taken.

    ``published`` means this call claimed the lease and published the queued
    record. ``complete``/``cancelled``/``running``/``terminal``/``missing``
    mean there was nothing left for this repair to do. ``invalid_state`` and
    ``identity_changed`` are refusals; ``failed``/``claim_failed`` mean the
    attempt raised (``error`` carries the exception) and the lease was parked
    back to REPAIR_PENDING.
    """

    reason: str
    entry: QueueEntry | None = None
    error: BaseException | None = None

    @property
    def repaired(self) -> bool:
        return self.reason in {
            "published",
            "complete",
            "cancelled",
            "running",
            "terminal",
            "missing",
        }


def _entry_token(entry: QueueEntry) -> str:
    return normalize_text(entry.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))


def _run_compensated_cleanup(spec: EnqueuePublicationSpec) -> None:
    if spec.on_compensated_failure is None:
        return
    try:
        spec.on_compensated_failure()
    except BaseException:  # noqa: BLE001 - never mask the enqueue-origin failure
        logger.exception(
            "%s: failed to clean the compensated submission snapshot; retaining it",
            spec.label,
        )


def _fence_uncompensated_enqueue(
    spec: EnqueuePublicationSpec,
    *,
    error: QueueAfterCommitError,
    publication_token: str,
) -> None:
    """Terminally fence one exact provisional row without publishing it."""

    entry = error.provisional_result
    metadata = entry.metadata if isinstance(entry, QueueEntry) else {}
    if (
        not isinstance(entry, QueueEntry)
        or entry.task_id != spec.task_id
        or normalize_text(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)) != publication_token
    ):
        logger.error(
            "%s: cannot identify the provisional row after compensation failure",
            spec.label,
        )
        return
    mark_failed_fn = spec.mark_failed_fn if spec.mark_failed_fn is not None else mark_failed
    try:
        fenced = mark_failed_fn(
            spec.queue_root,
            entry.queue_id,
            error=(
                "queue_after_commit_guard_failed:"
                f"compensation={error.compensation_outcome}:"
                f"{error.after_commit_error.__class__.__name__}:"
                f"{error.after_commit_error}"
            ),
            metadata_update=queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_ABORTED,
                token=publication_token,
                owner_pid=0,
            ),
            expected_entry=entry,
            expected_task_id=spec.task_id,
        )
    except BaseException:  # noqa: BLE001 - preserve the guard-origin failure path
        logger.exception(
            "%s: failed to terminally fence the provisional row: queue_id=%s",
            spec.label,
            entry.queue_id,
        )
        return
    if not fenced:
        logger.error(
            "%s: provisional row was not visible for terminal fencing: queue_id=%s",
            spec.label,
            entry.queue_id,
        )


def _recover_committed_enqueue(
    spec: EnqueuePublicationSpec,
    *,
    enqueue_metadata: dict[str, Any],
) -> QueueEntry | None:
    """Fence an enqueue that may have committed before reporting an error.

    Recover only the single row carrying every identity of this exact enqueue
    attempt; the publication token is necessary but deliberately not
    sufficient by itself. The recovered row is parked REPAIR_PENDING so the
    worker repair pass publishes it; multiple matches are all terminally
    fenced because none of them can be trusted.
    """

    expected_token = normalize_text(enqueue_metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))
    expected_owner_start = normalize_text(enqueue_metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY))
    expected_job_dir = normalize_text(enqueue_metadata.get(spec.job_dir_metadata_key))
    try:
        expected_owner_pid = int(enqueue_metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
    except (TypeError, ValueError):
        return None
    if not expected_token or not expected_job_dir or expected_owner_pid <= 0:
        return None
    expected_job_dir = str(Path(expected_job_dir).expanduser().resolve())

    def identity_matches(current: QueueEntry) -> bool:
        metadata = current.metadata if isinstance(current.metadata, dict) else {}
        try:
            current_owner_pid = int(metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
            current_priority = int(current.priority)
        except (TypeError, ValueError):
            return False
        current_job_dir = normalize_text(metadata.get(spec.job_dir_metadata_key))
        if current_job_dir:
            current_job_dir = str(Path(current_job_dir).expanduser().resolve())
        return bool(
            current.status == QueueStatus.PENDING
            and not current.cancel_requested
            and normalize_text(current.app_name) == spec.app_name
            and normalize_text(current.task_id) == spec.task_id
            and normalize_text(current.task_kind) == spec.task_kind
            and normalize_text(current.engine) == spec.engine
            and current_priority == int(spec.priority)
            and queue_record_sync_state(current) == QUEUE_RECORD_SYNC_PREPARING
            and normalize_text(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)) == expected_token
            and current_owner_pid == expected_owner_pid
            and normalize_text(metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY))
            == expected_owner_start
            and current_job_dir == expected_job_dir
        )

    ambiguous = False

    def recover(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        nonlocal ambiguous
        matches = [
            (index, current) for index, current in enumerate(entries) if identity_matches(current)
        ]
        if not matches:
            return None, False
        if len(matches) != 1:
            ambiguous = True
            finished_at = now_utc_iso()
            for index, current in matches:
                metadata = dict(current.metadata)
                metadata.update(
                    queue_record_sync_metadata(
                        QUEUE_RECORD_SYNC_ABORTED,
                        token="",
                        owner_pid=0,
                    )
                )
                if spec.ambiguous_fence_metadata:
                    metadata.update(spec.ambiguous_fence_metadata)
                entries[index] = replace(
                    current,
                    status=QueueStatus.CANCELLED,
                    cancel_requested=True,
                    finished_at=finished_at,
                    metadata=metadata,
                )
            return None, True
        index, current = matches[0]
        metadata = dict(current.metadata)
        metadata.update(
            queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_REPAIR_PENDING,
                token=expected_token,
                owner_pid=0,
            )
        )
        updated = replace(current, metadata=metadata)
        entries[index] = updated
        return updated, True

    recovered = mutate_entries(spec.queue_root, recover)
    if ambiguous:
        # Multiple rows carry this exact attempt's identity: all of them were
        # terminally fenced, and the true outcome cannot be reported as either
        # success or clean failure (D3).
        raise EnqueuePublicationOutcomeUnknown(
            f"{spec.label}: ambiguous persisted queue rows after an enqueue error were "
            "terminally fenced; the enqueue outcome is unknown"
        )
    return recovered


def _park_repair_pending(
    spec: EnqueuePublicationSpec,
    entry: QueueEntry,
    *,
    expected_token: str,
    expected_state: str,
) -> None:
    """Token-gated CAS parking the owned lease back to the explicit repair queue."""

    def park(entries: list[QueueEntry]) -> tuple[None, bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if (
                current.status != QueueStatus.PENDING
                or queue_record_sync_state(current) != expected_state
                or _entry_token(current) != expected_token
            ):
                return None, False
            metadata = dict(current.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIR_PENDING,
                    token=expected_token,
                    owner_pid=0,
                )
            )
            entries[index] = replace(current, metadata=metadata)
            return None, True
        return None, False

    try:
        mutate_entries(spec.queue_root, park)
    except BaseException:  # noqa: BLE001 - never mask the original failure
        logger.warning(
            "%s: failed to park queued record as repair pending: queue_id=%s",
            spec.label,
            getattr(entry, "queue_id", ""),
            exc_info=True,
        )


def _publish_owned_record(
    spec: EnqueuePublicationSpec,
    entry: QueueEntry,
    *,
    publication_token: str,
) -> EnqueuePublicationOutcome:
    """Publish under the per-row lock; every failure parks REPAIR_PENDING."""

    def read_current(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        owned = [current for current in entries if current.queue_id == entry.queue_id]
        return (owned[0] if len(owned) == 1 else None), False

    def complete(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        for index, row in enumerate(entries):
            if row.queue_id != entry.queue_id:
                continue
            if (
                row.status != QueueStatus.PENDING
                or row.cancel_requested
                or queue_record_sync_state(row) != QUEUE_RECORD_SYNC_PREPARING
                or _entry_token(row) != publication_token
            ):
                return None, False
            metadata = dict(row.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE,
                    token=publication_token,
                    owner_pid=0,
                )
            )
            updated = replace(row, metadata=metadata)
            entries[index] = updated
            return updated, True
        return None, False

    try:
        with queue_record_publication_lock(spec.queue_root, entry.queue_id):
            current = mutate_entries(spec.queue_root, read_current)
            if current is None or not spec.same_generation(current, entry):
                logger.warning(
                    "%s: %s: queue_id=%s",
                    spec.label,
                    _OWNERSHIP_RECONCILE_WARNING,
                    entry.queue_id,
                )
                return EnqueuePublicationOutcome(
                    entry=current if current is not None else entry,
                    published=False,
                    warnings=(_OWNERSHIP_RECONCILE_WARNING,),
                )
            if current.status == QueueStatus.CANCELLED or current.cancel_requested:
                # Pending cancellation publishes its own terminal artifacts; a
                # late publisher must never write into the shared job dir here.
                return EnqueuePublicationOutcome(
                    entry=current,
                    published=False,
                    cancelled=True,
                )
            sync_state = queue_record_sync_state(current)
            if sync_state == QUEUE_RECORD_SYNC_COMPLETE:
                # Only this lease's COMPLETE counts as this publisher's
                # success; a foreign COMPLETE means ownership was taken over.
                if _entry_token(current) == publication_token:
                    return EnqueuePublicationOutcome(entry=current, published=True)
                logger.warning(
                    "%s: %s: queue_id=%s",
                    spec.label,
                    _OWNERSHIP_RECONCILE_WARNING,
                    entry.queue_id,
                )
                return EnqueuePublicationOutcome(
                    entry=current,
                    published=False,
                    warnings=(_OWNERSHIP_RECONCILE_WARNING,),
                )
            if (
                current.status != QueueStatus.PENDING
                or sync_state != QUEUE_RECORD_SYNC_PREPARING
                or _entry_token(current) != publication_token
            ):
                logger.warning(
                    "%s: %s: queue_id=%s",
                    spec.label,
                    _OWNERSHIP_RECONCILE_WARNING,
                    entry.queue_id,
                )
                return EnqueuePublicationOutcome(
                    entry=current,
                    published=False,
                    warnings=(_OWNERSHIP_RECONCILE_WARNING,),
                )
            spec.publish(current)
            completed = mutate_entries(spec.queue_root, complete)
            if completed is None:
                _park_repair_pending(
                    spec,
                    current,
                    expected_token=publication_token,
                    expected_state=QUEUE_RECORD_SYNC_PREPARING,
                )
                logger.warning(
                    "%s: %s: queue_id=%s",
                    spec.label,
                    _OWNERSHIP_RECONCILE_WARNING,
                    entry.queue_id,
                )
                return EnqueuePublicationOutcome(
                    entry=current,
                    published=False,
                    warnings=(_OWNERSHIP_RECONCILE_WARNING,),
                )
            return EnqueuePublicationOutcome(entry=completed, published=True)
    except BaseException as exc:  # noqa: BLE001
        # Even a failed publish or CAS may have half-committed; park the lease
        # (token-gated, so a row this publisher no longer owns is untouched)
        # and let the worker repair pass republish before the row can run.
        _park_repair_pending(
            spec,
            entry,
            expected_token=publication_token,
            expected_state=QUEUE_RECORD_SYNC_PREPARING,
        )
        if not isinstance(exc, Exception):
            raise
        warning = (
            "queued record publication failed; queue submission succeeded and "
            f"worker repair will publish it ({exc.__class__.__name__}: {exc})"
        )
        logger.warning("%s: %s: queue_id=%s", spec.label, warning, entry.queue_id, exc_info=True)
        return EnqueuePublicationOutcome(
            entry=entry,
            published=False,
            warnings=(warning,),
        )


def run_enqueue_publication(spec: EnqueuePublicationSpec) -> EnqueuePublicationOutcome:
    """Durably enqueue one row and publish its queued record exactly once."""

    publication_token = timestamped_token("record_sync", token_bytes=16)
    metadata = dict(spec.metadata)
    metadata.update(
        queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING,
            token=publication_token,
            owner_pid=os.getpid(),
        )
    )
    enqueue_fn = spec.enqueue_fn if spec.enqueue_fn is not None else enqueue
    try:
        entry = enqueue_fn(
            spec.queue_root,
            app_name=spec.app_name,
            task_id=spec.task_id,
            task_kind=spec.task_kind,
            engine=spec.engine,
            priority=spec.priority,
            metadata=metadata,
            duplicate_policy=spec.duplicate_policy,
            before_commit_fn=spec.before_commit_fn,
            after_commit_fn=spec.after_commit_fn,
        )
    except QueueAfterCommitError as exc:
        if exc.compensation_succeeded:
            _run_compensated_cleanup(spec)
            raise
        _fence_uncompensated_enqueue(
            spec,
            error=exc,
            publication_token=publication_token,
        )
        raise EnqueuePublicationOutcomeUnknown(
            f"{spec.label}: queue enqueue outcome is unknown after a failed compensation: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    except BaseException as exc:
        try:
            recovered = _recover_committed_enqueue(spec, enqueue_metadata=metadata)
        except BaseException as recovery_exc:  # noqa: BLE001
            if isinstance(recovery_exc, EnqueuePublicationOutcomeUnknown):
                raise
            if not isinstance(recovery_exc, Exception):
                raise
            logger.warning(
                "%s: failed to recover a possibly committed enqueue: task_id=%s",
                spec.label,
                spec.task_id,
                exc_info=True,
            )
            raise EnqueuePublicationOutcomeUnknown(
                f"{spec.label}: queue enqueue ownership is indeterminate after recovery "
                f"failure: {exc.__class__.__name__}: {exc}"
            ) from exc
        if recovered is None:
            _run_compensated_cleanup(spec)
            raise
        if not isinstance(exc, Exception):
            raise
        warning = (
            "queue enqueue committed but its result was lost; the row is parked "
            f"for worker repair ({exc.__class__.__name__}: {exc})"
        )
        logger.warning(
            "%s: %s: queue_id=%s",
            spec.label,
            warning,
            recovered.queue_id,
            exc_info=True,
        )
        return EnqueuePublicationOutcome(
            entry=recovered,
            published=False,
            warnings=(warning,),
        )

    if spec.finalize_intent is not None:
        try:
            spec.finalize_intent(entry)
        except BaseException as exc:  # noqa: BLE001
            _park_repair_pending(
                spec,
                entry,
                expected_token=publication_token,
                expected_state=QUEUE_RECORD_SYNC_PREPARING,
            )
            if not isinstance(exc, Exception):
                raise
            warning = (
                "snapshot intent finalization failed; queue submission succeeded and "
                f"worker repair will publish the queued record ({exc.__class__.__name__}: {exc})"
            )
            logger.warning(
                "%s: %s: queue_id=%s",
                spec.label,
                warning,
                entry.queue_id,
                exc_info=True,
            )
            return EnqueuePublicationOutcome(
                entry=entry,
                published=False,
                warnings=(warning,),
            )

    return _publish_owned_record(spec, entry, publication_token=publication_token)


def repair_enqueue_publication(
    queue_root: Path,
    entry: QueueEntry,
    *,
    publish: Callable[[QueueEntry], None],
    label: str,
    same_generation: Callable[[QueueEntry, QueueEntry], bool] = queue_entries_same_generation,
    repair_missing_lease: bool = False,
) -> bool:
    """Boolean form of :func:`repair_enqueue_publication_outcome`."""

    return repair_enqueue_publication_outcome(
        queue_root,
        entry,
        publish=publish,
        label=label,
        same_generation=same_generation,
        repair_missing_lease=repair_missing_lease,
    ).repaired


def repair_enqueue_publication_outcome(
    queue_root: Path,
    entry: QueueEntry,
    *,
    publish: Callable[[QueueEntry], None],
    label: str,
    same_generation: Callable[[QueueEntry, QueueEntry], bool] = queue_entries_same_generation,
    repair_missing_lease: bool = False,
) -> RepairOutcome:
    """Re-publish one committed row whose queued record never landed.

    Claims the row under the publication lock with a fresh token (the lock,
    not a live PID in the row, is the authoritative ownership proof), writes
    the queued job artifact, and marks the sync lease COMPLETE. Any failure
    parks the row as REPAIR_PENDING so it stays unclaimable rather than
    running without its published record. ``repair_missing_lease`` extends
    the claim to legacy rows carrying no sync lease at all.
    """

    repair_token = timestamped_token("record_sync", token_bytes=16)

    def claim(entries: list[QueueEntry]) -> tuple[tuple[str, QueueEntry | None], bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if not same_generation(current, entry):
                return ("identity_changed", current), False
            if current.cancel_requested:
                return ("cancelled", current), False
            sync_state = queue_record_sync_state(current)
            if sync_state == QUEUE_RECORD_SYNC_COMPLETE:
                return ("complete", current), False
            if current.status == QueueStatus.RUNNING:
                return ("running", current), False
            if current.status != QueueStatus.PENDING:
                return ("terminal", current), False
            if sync_state not in REPAIRABLE_SYNC_STATES and (
                sync_state or not repair_missing_lease
            ):
                return ("invalid_state", current), False
            metadata = dict(current.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIRING,
                    token=repair_token,
                    owner_pid=os.getpid(),
                )
            )
            updated = replace(current, metadata=metadata)
            entries[index] = updated
            return ("claimed", updated), True
        return ("missing", None), False

    def complete(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        for index, row in enumerate(entries):
            if row.queue_id != entry.queue_id:
                continue
            # The COMPLETE transition belongs to this repair lease only: any
            # other sync state or token here (for example a cancel fence
            # written concurrently) must never be overwritten.
            if (
                row.status != QueueStatus.PENDING
                or row.cancel_requested
                or queue_record_sync_state(row) != QUEUE_RECORD_SYNC_REPAIRING
                or _entry_token(row) != repair_token
            ):
                raise RuntimeError(
                    f"{label}: queued record repair lost publication ownership: "
                    f"queue_id={entry.queue_id}"
                )
            metadata = dict(row.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE,
                    token=repair_token,
                    owner_pid=0,
                )
            )
            updated = replace(row, metadata=metadata)
            entries[index] = updated
            return updated, True
        raise RuntimeError(f"{label}: queue entry disappeared during repair: {entry.queue_id}")

    def park_repair_lease(entries: list[QueueEntry]) -> tuple[None, bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if (
                current.status != QueueStatus.PENDING
                or queue_record_sync_state(current) != QUEUE_RECORD_SYNC_REPAIRING
                or _entry_token(current) != repair_token
            ):
                return None, False
            metadata = dict(current.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIR_PENDING,
                    token=repair_token,
                    owner_pid=0,
                )
            )
            entries[index] = replace(current, metadata=metadata)
            return None, True
        return None, False

    claimed = False
    try:
        # One lock acquisition covers claim, publication, and completion, so no
        # cancel fence or foreign publication can interleave between them.
        with queue_record_publication_lock(queue_root, entry.queue_id):
            outcome, current = mutate_entries(queue_root, claim)
            if outcome != "claimed":
                if outcome == "invalid_state":
                    logger.error(
                        "%s: cannot repair queue publication with invalid state %r: queue_id=%s",
                        label,
                        queue_record_sync_state(current),
                        entry.queue_id,
                    )
                elif outcome == "identity_changed":
                    logger.warning(
                        "%s: queued record repair refused a changed queue generation: queue_id=%s",
                        label,
                        entry.queue_id,
                    )
                # complete / cancelled / running / terminal / missing: nothing
                # left for this repair to do.
                return RepairOutcome(reason=outcome, entry=current)
            claimed = True
            assert current is not None
            publish(current)
            completed = mutate_entries(queue_root, complete)
    except BaseException as exc:  # noqa: BLE001
        # Even a failed claim may have committed before its durability barrier
        # reported failure; park the lease so the row cannot strand in
        # REPAIRING (the park CAS is token-gated, so it never touches a row
        # this repair does not own).
        try:
            mutate_entries(queue_root, park_repair_lease)
        except BaseException:  # noqa: BLE001 - never mask the original failure
            logger.warning(
                "%s: failed to park queued record as repair pending: queue_id=%s",
                label,
                getattr(entry, "queue_id", ""),
                exc_info=True,
            )
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "%s: queued record repair %s: queue_id=%s",
            label,
            "failed" if claimed else "claim failed",
            entry.queue_id,
            exc_info=True,
        )
        return RepairOutcome(
            reason="failed" if claimed else "claim_failed",
            entry=entry,
            error=exc,
        )
    logger.info("%s: repaired queued record publication: queue_id=%s", label, entry.queue_id)
    return RepairOutcome(reason="published", entry=completed)


__all__ = [
    "REPAIRABLE_SYNC_STATES",
    "EnqueuePublicationOutcome",
    "EnqueuePublicationOutcomeUnknown",
    "EnqueuePublicationSpec",
    "RepairOutcome",
    "repair_enqueue_publication",
    "repair_enqueue_publication_outcome",
    "run_enqueue_publication",
]
