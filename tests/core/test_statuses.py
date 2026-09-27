"""Single-definition guarantees for the shared status vocabularies."""

from __future__ import annotations

from orca_auto.core import statuses as core_statuses
from orca_auto.core.queue.types import (
    ACTIVE_QUEUE_STATUSES,
    TERMINAL_QUEUE_STATUSES,
    QueueEntry,
    QueueStatus,
    effective_queue_status,
)


def _entry(status: QueueStatus, *, cancel_requested: bool = False) -> QueueEntry:
    return QueueEntry(
        queue_id="q1",
        app_name="app",
        task_id="t1",
        task_kind="kind",
        engine="orca",
        status=status,
        cancel_requested=cancel_requested,
    )


def test_queue_status_sets_partition_the_enum() -> None:
    assert ACTIVE_QUEUE_STATUSES | TERMINAL_QUEUE_STATUSES == frozenset(QueueStatus)
    assert not (ACTIVE_QUEUE_STATUSES & TERMINAL_QUEUE_STATUSES)
    assert {s.value for s in TERMINAL_QUEUE_STATUSES} == core_statuses.TERMINAL_STATUSES
    assert {s.value for s in ACTIVE_QUEUE_STATUSES} == core_statuses.ACTIVE_STATUSES


def test_display_sets_only_contain_values_a_store_can_hold() -> None:
    from orca_auto.orca.statuses import RunStatus

    persisted = (
        {s.value for s in QueueStatus}
        | {s.value for s in RunStatus}
        | {core_statuses.STATUS_QUEUED}
    )
    derived = {core_statuses.STATUS_CANCEL_REQUESTED}
    assert core_statuses.QUEUE_ACTIVE_STATUSES <= persisted | derived
    assert core_statuses.TERMINAL_STATUSES <= persisted
    assert core_statuses.ACTIVE_SIMULATION_STATUSES <= core_statuses.QUEUE_ACTIVE_STATUSES


def test_every_status_lands_in_one_summary_bucket() -> None:
    buckets = {key for key, _label, _status in core_statuses.SUMMARY_BUCKETS}
    assert core_statuses.summary_bucket("RUNNING") == "running"
    assert {
        core_statuses.summary_bucket(status) for status in ("created", "queued", "retrying")
    } == {"pending"}
    assert core_statuses.summary_bucket("cancel_requested") == "pending"
    assert core_statuses.summary_bucket("completed") == "done"
    assert core_statuses.summary_bucket("repair_blocked") == "failed"
    assert core_statuses.summary_bucket("cancelled") == "cancelled"
    assert core_statuses.summary_bucket(None) == "other"
    assert core_statuses.summary_bucket("submitted") in buckets


def test_effective_queue_status_overlays_cancel_request_on_running_only() -> None:
    assert effective_queue_status(_entry(QueueStatus.RUNNING)) == "running"
    assert (
        effective_queue_status(_entry(QueueStatus.RUNNING, cancel_requested=True))
        == core_statuses.STATUS_CANCEL_REQUESTED
    )
    assert effective_queue_status(_entry(QueueStatus.PENDING, cancel_requested=True)) == "pending"
    assert (
        effective_queue_status(_entry(QueueStatus.CANCELLED, cancel_requested=True)) == "cancelled"
    )
