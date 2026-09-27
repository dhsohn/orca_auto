"""Submit side of the queued-record publication lease.

``run_enqueue_publication`` durably enqueues one ORCA row with a PREPARING
lease, publishes its queued job record under the per-row publication lock
and completes the lease. Every failure after the commit parks the row for
the worker repair pass (:mod:`.publication_repair`). The lease protocol and
its invariants are described in :mod:`orca_auto.core.queue.publication`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.admission import admission_dir
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    park_queue_record_repair_pending,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
    queue_record_sync_token,
)
from orca_auto.core.queue.store import (
    QueueAfterCommitError,
    QueueLockTimeoutError,
    QueueStoreCorruptError,
    mutate_entries,
)
from orca_auto.core.queue.transitions import terminal_entry
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import STATUS_QUEUED
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.persistence import timestamped_token

from ..app_ids import ORCA_AUTO_ORCA_APP_NAME, ORCA_ENGINE, ORCA_TASK_KIND
from ..config import AppConfig
from ..run_dir_guard import assert_run_dir_publication_allowed
from . import adapter as queue_adapter
from . import roots
from .entries import TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY, same_generation
from .job_records import upsert_row_job_record
from .orphans import reconcile_dead_running_rows_for_dir

logger = logging.getLogger(__name__)

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


def _run_compensated_cleanup(on_compensated_failure: Callable[[], None]) -> None:
    try:
        on_compensated_failure()
    except BaseException:  # Never mask the enqueue-origin failure.
        logger.exception("ORCA: failed to clean the compensated submission snapshot; retaining it")


def _fence_uncompensated_enqueue(
    queue_root: Path,
    *,
    task_id: str,
    error: QueueAfterCommitError,
    publication_token: str,
) -> None:
    """Terminally fence one exact provisional row without publishing it."""

    entry = error.provisional_result
    metadata = entry.metadata if isinstance(entry, QueueEntry) else {}
    if (
        not isinstance(entry, QueueEntry)
        or entry.task_id != task_id
        or normalize_text(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)) != publication_token
    ):
        logger.error("ORCA: cannot identify the provisional row after compensation failure")
        return
    try:
        # The adapter's administrative fence-only replay marker keeps the
        # fenced generation's terminal ownership.
        fenced = queue_adapter.mark_failed(
            queue_root,
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
            publish_terminal_side_effects=False,
            expected_entry=entry,
            expected_task_id=task_id,
        )
    except BaseException:  # Preserve the guard-origin failure path.
        logger.exception(
            "ORCA: failed to terminally fence the provisional row: queue_id=%s",
            entry.queue_id,
        )
        return
    if not fenced:
        logger.error(
            "ORCA: provisional row was not visible for terminal fencing: queue_id=%s",
            entry.queue_id,
        )


@dataclass(frozen=True)
class _EnqueueAttemptIdentity:
    """Every identity one enqueue attempt stamped on its row.

    The publication token is necessary but deliberately not sufficient by
    itself; the owner pid, owner start, job dir, task id and priority must all
    agree too.
    """

    token: str
    owner_pid: int
    owner_start: str
    job_dir: str
    task_id: str
    priority: int


def _enqueue_attempt_identity(
    enqueue_metadata: dict[str, Any],
    *,
    task_id: str,
    priority: int,
) -> _EnqueueAttemptIdentity | None:
    """Extract the identity this attempt wrote, or ``None`` when it is incomplete."""

    token = normalize_text(enqueue_metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))
    owner_start = normalize_text(enqueue_metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY))
    job_dir = normalize_text(enqueue_metadata.get("reaction_dir"))
    try:
        owner_pid = int(enqueue_metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
    except (TypeError, ValueError):
        return None
    if not token or not job_dir or owner_pid <= 0:
        return None
    return _EnqueueAttemptIdentity(
        token=token,
        owner_pid=owner_pid,
        owner_start=owner_start,
        job_dir=str(Path(job_dir).expanduser().resolve()),
        task_id=task_id,
        priority=int(priority),
    )


def _row_matches_enqueue_attempt(current: QueueEntry, identity: _EnqueueAttemptIdentity) -> bool:
    """Whether ``current`` is a still-PREPARING row carrying this attempt's identity."""

    metadata = current.metadata if isinstance(current.metadata, dict) else {}
    try:
        current_owner_pid = int(metadata.get(QUEUE_RECORD_SYNC_OWNER_PID_KEY, 0) or 0)
        current_priority = int(current.priority)
    except (TypeError, ValueError):
        return False
    current_job_dir = normalize_text(metadata.get("reaction_dir"))
    if current_job_dir:
        current_job_dir = str(Path(current_job_dir).expanduser().resolve())
    return bool(
        current.status == QueueStatus.PENDING
        and not current.cancel_requested
        and normalize_text(current.app_name) == ORCA_AUTO_ORCA_APP_NAME
        and normalize_text(current.task_id) == identity.task_id
        and normalize_text(current.task_kind) == ORCA_TASK_KIND
        and normalize_text(current.engine) == ORCA_ENGINE
        and current_priority == identity.priority
        and queue_record_sync_state(current) == QUEUE_RECORD_SYNC_PREPARING
        and normalize_text(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)) == identity.token
        and current_owner_pid == identity.owner_pid
        and normalize_text(metadata.get(QUEUE_RECORD_SYNC_OWNER_START_KEY)) == identity.owner_start
        and current_job_dir == identity.job_dir
    )


def _fence_ambiguous_rows(entries: list[QueueEntry], matches: list[tuple[int, QueueEntry]]) -> None:
    """Terminally fence every row that claims this attempt's identity.

    None of several matching rows can be trusted, so all of them are
    CANCELLED with the lease ABORTED and no owner left to resume them. The
    administrative fence-only marker keeps a successor generation blocked
    until the duplicates are cleared.
    """

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
        metadata[TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY] = True
        # A deliberate cancel_requested=True: the fence records that
        # this attempt's outcome is unknown and no owner may resume it.
        entries[index] = terminal_entry(
            current,
            status=QueueStatus.CANCELLED,
            error=None,
            finished_at=finished_at,
            metadata=metadata,
            cancel_requested=True,
        )


def _park_recovered_row(
    entries: list[QueueEntry],
    index: int,
    current: QueueEntry,
    *,
    token: str,
) -> QueueEntry:
    """Park the single recovered row REPAIR_PENDING for the worker repair pass."""

    metadata = dict(current.metadata)
    metadata.update(
        queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_REPAIR_PENDING,
            token=token,
            owner_pid=0,
        )
    )
    updated = replace(current, metadata=metadata)
    entries[index] = updated
    return updated


def _recover_committed_enqueue(
    queue_root: Path,
    *,
    task_id: str,
    priority: int,
    enqueue_metadata: dict[str, Any],
) -> QueueEntry | None:
    """Fence an enqueue that may have committed before reporting an error.

    Recover only the single row carrying every identity of this exact enqueue
    attempt; the publication token is necessary but deliberately not
    sufficient by itself. The recovered row is parked REPAIR_PENDING so the
    worker repair pass publishes it; multiple matches are all terminally
    fenced because none of them can be trusted.
    """

    identity = _enqueue_attempt_identity(enqueue_metadata, task_id=task_id, priority=priority)
    if identity is None:
        return None

    ambiguous = False

    def recover(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        nonlocal ambiguous
        matches = [
            (index, current)
            for index, current in enumerate(entries)
            if _row_matches_enqueue_attempt(current, identity)
        ]
        if not matches:
            return None, False
        if len(matches) != 1:
            ambiguous = True
            _fence_ambiguous_rows(entries, matches)
            return None, True
        index, current = matches[0]
        return _park_recovered_row(entries, index, current, token=identity.token), True

    recovered = mutate_entries(queue_root, recover)
    if ambiguous:
        # Multiple rows carry this exact attempt's identity: all of them were
        # terminally fenced, and the true outcome cannot be reported as either
        # success or clean failure (D3).
        raise EnqueuePublicationOutcomeUnknown(
            "ORCA: ambiguous persisted queue rows after an enqueue error were "
            "terminally fenced; the enqueue outcome is unknown"
        )
    return recovered


def _park_repair_pending(queue_root: Path, entry: QueueEntry, *, expected_token: str) -> None:
    """Token-gated CAS parking the owned PREPARING lease back to the repair queue."""

    def park(entries: list[QueueEntry]) -> tuple[None, bool]:
        return park_queue_record_repair_pending(
            entries,
            entry,
            expected_state=QUEUE_RECORD_SYNC_PREPARING,
            expected_token=expected_token,
        )

    try:
        mutate_entries(queue_root, park)
    except BaseException:  # noqa: BLE001 - never mask the original failure
        logger.warning(
            "ORCA: failed to park queued record as repair pending: queue_id=%s",
            getattr(entry, "queue_id", ""),
            exc_info=True,
        )


def _read_row(entries: list[QueueEntry], queue_id: str) -> tuple[QueueEntry | None, bool]:
    """Read-only mutator: the one row with ``queue_id``, or ``None`` when absent/duplicated."""

    owned = [current for current in entries if current.queue_id == queue_id]
    return (owned[0] if len(owned) == 1 else None), False


def _complete_owned_lease(
    entries: list[QueueEntry],
    entry: QueueEntry,
    *,
    publication_token: str,
) -> tuple[QueueEntry | None, bool]:
    """Token-gated CAS from this publisher's PREPARING lease to COMPLETE."""

    for index, row in enumerate(entries):
        if row.queue_id != entry.queue_id:
            continue
        if (
            row.status != QueueStatus.PENDING
            or row.cancel_requested
            or queue_record_sync_state(row) != QUEUE_RECORD_SYNC_PREPARING
            or queue_record_sync_token(row) != publication_token
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


def _ownership_lost(queue_id: str, *, entry: QueueEntry) -> EnqueuePublicationOutcome:
    """Report that another lease owns the row; the worker repair pass reconciles."""

    logger.warning("ORCA: %s: queue_id=%s", _OWNERSHIP_RECONCILE_WARNING, queue_id)
    return EnqueuePublicationOutcome(
        entry=entry,
        published=False,
        warnings=(_OWNERSHIP_RECONCILE_WARNING,),
    )


def _owned_lease_short_circuit(
    entry: QueueEntry,
    current: QueueEntry | None,
    *,
    publication_token: str,
) -> EnqueuePublicationOutcome | None:
    """Re-validate ownership under the lock before any side effect.

    Returns the outcome that ends publication early (ownership lost, cancelled,
    or already COMPLETE by this lease), or ``None`` when this publisher still
    owns a PREPARING lease and must publish.
    """

    if current is None or not same_generation(current, entry):
        return _ownership_lost(entry.queue_id, entry=current if current is not None else entry)
    if current.status == QueueStatus.CANCELLED or current.cancel_requested:
        # Pending cancellation publishes its own terminal artifacts; a
        # late publisher must never write into the shared job dir here.
        return EnqueuePublicationOutcome(entry=current, published=False, cancelled=True)
    sync_state = queue_record_sync_state(current)
    if sync_state == QUEUE_RECORD_SYNC_COMPLETE:
        # Only this lease's COMPLETE counts as this publisher's
        # success; a foreign COMPLETE means ownership was taken over.
        if queue_record_sync_token(current) == publication_token:
            return EnqueuePublicationOutcome(entry=current, published=True)
        return _ownership_lost(entry.queue_id, entry=current)
    if (
        current.status != QueueStatus.PENDING
        or sync_state != QUEUE_RECORD_SYNC_PREPARING
        or queue_record_sync_token(current) != publication_token
    ):
        return _ownership_lost(entry.queue_id, entry=current)
    return None


def _publish_and_complete(
    cfg: AppConfig,
    queue_root: Path,
    current: QueueEntry,
    *,
    publication_token: str,
) -> EnqueuePublicationOutcome:
    """Publish the record, then CAS the lease COMPLETE; a lost CAS parks the row."""

    def complete(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        return _complete_owned_lease(entries, current, publication_token=publication_token)

    upsert_row_job_record(cfg, current, STATUS_QUEUED, require_task_id=True)
    completed = mutate_entries(queue_root, complete)
    if completed is None:
        _park_repair_pending(queue_root, current, expected_token=publication_token)
        return _ownership_lost(current.queue_id, entry=current)
    return EnqueuePublicationOutcome(entry=completed, published=True)


def _publication_failed(entry: QueueEntry, exc: Exception) -> EnqueuePublicationOutcome:
    """Outcome of a publication whose lease was parked after ``exc``."""

    warning = (
        "queued record publication failed; queue submission succeeded and "
        f"worker repair will publish it ({exc.__class__.__name__}: {exc})"
    )
    logger.warning("ORCA: %s: queue_id=%s", warning, entry.queue_id, exc_info=exc)
    return EnqueuePublicationOutcome(entry=entry, published=False, warnings=(warning,))


def _publish_owned_record(
    cfg: AppConfig,
    queue_root: Path,
    entry: QueueEntry,
    *,
    publication_token: str,
) -> EnqueuePublicationOutcome:
    """Publish under the per-row lock; every failure parks REPAIR_PENDING."""

    def read_current(entries: list[QueueEntry]) -> tuple[QueueEntry | None, bool]:
        return _read_row(entries, entry.queue_id)

    try:
        with queue_record_publication_lock(queue_root, entry.queue_id):
            current = mutate_entries(queue_root, read_current)
            short_circuit = _owned_lease_short_circuit(
                entry,
                current,
                publication_token=publication_token,
            )
            if short_circuit is not None:
                return short_circuit
            assert current is not None
            return _publish_and_complete(
                cfg, queue_root, current, publication_token=publication_token
            )
    except BaseException as exc:
        # Even a failed publish or CAS may have half-committed; park the lease
        # (token-gated, so a row this publisher no longer owns is untouched)
        # and let the worker repair pass republish before the row can run.
        _park_repair_pending(queue_root, entry, expected_token=publication_token)
        if not isinstance(exc, Exception):
            raise
        return _publication_failed(entry, exc)


_PRE_COMMIT_ENQUEUE_ERRORS: tuple[type[BaseException], ...] = (
    QueueLockTimeoutError,
    QueueStoreCorruptError,
)


def run_enqueue_publication(
    cfg: AppConfig,
    reaction_dir: Path,
    *,
    task_id: str,
    priority: int,
    force: bool,
    metadata: dict[str, Any],
    on_compensated_failure: Callable[[], None],
) -> EnqueuePublicationOutcome:
    """Durably enqueue one ORCA row and publish its queued record exactly once.

    ``on_compensated_failure`` undoes the caller's preparation when the row
    certainly never committed; it is never called once a row may exist.
    """

    queue_root = roots.queue_root(cfg)
    publication_token = timestamped_token("record_sync", token_bytes=16)
    enqueue_metadata = dict(metadata)
    enqueue_metadata.update(
        queue_record_sync_metadata(
            QUEUE_RECORD_SYNC_PREPARING,
            token=publication_token,
            owner_pid=os.getpid(),
        )
    )
    try:
        # A dead worker's RUNNING row of this directory would reject the
        # submission as a duplicate; its judgement failing is an enqueue
        # failure like any other.
        reconcile_dead_running_rows_for_dir(
            queue_root, str(reaction_dir), admission_root=admission_dir(queue_root)
        )
        entry = queue_adapter.enqueue(
            queue_root,
            str(reaction_dir),
            priority=priority,
            force=force,
            task_id=task_id,
            metadata=enqueue_metadata,
            before_commit_fn=lambda: assert_run_dir_publication_allowed(
                "ORCA durable queue pre-commit"
            ),
            after_commit_fn=lambda: assert_run_dir_publication_allowed(
                "ORCA durable queue post-commit"
            ),
        )
    except QueueAfterCommitError as exc:
        if exc.compensation_succeeded:
            _run_compensated_cleanup(on_compensated_failure)
            raise
        _fence_uncompensated_enqueue(
            queue_root,
            task_id=task_id,
            error=exc,
            publication_token=publication_token,
        )
        raise EnqueuePublicationOutcomeUnknown(
            "ORCA: queue enqueue outcome is unknown after a failed compensation: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    except BaseException as exc:
        if isinstance(exc, _PRE_COMMIT_ENQUEUE_ERRORS):
            # The queue lock is taken before any write and a corrupt store is
            # refused before any mutation, so nothing committed. A recovery
            # scan would fail the same way and turn a certain "not enqueued"
            # into "outcome unknown" while leaving the snapshot uncompensated.
            _run_compensated_cleanup(on_compensated_failure)
            raise
        try:
            recovered = _recover_committed_enqueue(
                queue_root,
                task_id=task_id,
                priority=priority,
                enqueue_metadata=enqueue_metadata,
            )
        except BaseException as recovery_exc:
            if isinstance(recovery_exc, EnqueuePublicationOutcomeUnknown):
                raise
            if not isinstance(recovery_exc, Exception):
                raise
            logger.warning(
                "ORCA: failed to recover a possibly committed enqueue: task_id=%s",
                task_id,
                exc_info=True,
            )
            raise EnqueuePublicationOutcomeUnknown(
                "ORCA: queue enqueue ownership is indeterminate after recovery "
                f"failure: {exc.__class__.__name__}: {exc}"
            ) from exc
        if recovered is None:
            _run_compensated_cleanup(on_compensated_failure)
            raise
        if not isinstance(exc, Exception):
            raise
        warning = (
            "queue enqueue committed but its result was lost; the row is parked "
            f"for worker repair ({exc.__class__.__name__}: {exc})"
        )
        logger.warning(
            "ORCA: %s: queue_id=%s",
            warning,
            recovered.queue_id,
            exc_info=True,
        )
        return EnqueuePublicationOutcome(
            entry=recovered,
            published=False,
            warnings=(warning,),
        )

    return _publish_owned_record(cfg, queue_root, entry, publication_token=publication_token)


__all__ = [
    "EnqueuePublicationOutcome",
    "EnqueuePublicationOutcomeUnknown",
    "run_enqueue_publication",
]
