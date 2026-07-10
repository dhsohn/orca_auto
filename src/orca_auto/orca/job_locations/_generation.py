from __future__ import annotations

from typing import Any

from orca_auto.core.queue.metadata import mapping_metadata_value
from orca_auto.core.utils import normalize_text


def _queue_generation(queue_entry: dict[str, Any] | None) -> tuple[str, str]:
    queue = queue_entry or {}
    return (
        normalize_text(queue.get("task_id")),
        normalize_text(mapping_metadata_value(queue, "run_id")),
    )


def queue_has_generation_identity(queue_entry: dict[str, Any] | None) -> bool:
    """Return whether a queue entry can identify one execution generation."""

    return any(_queue_generation(queue_entry))


def payload_matches_queue_generation(
    queue_entry: dict[str, Any] | None,
    payload: dict[str, Any],
) -> bool:
    """Return whether an artifact payload belongs to the selected queue entry.

    Queue ``task_id`` is allocated at submission and is copied to the ORCA
    state's ``job_id`` before execution.  ``run_id`` is added to the queue
    entry during terminal finalization.  Comparing both identities lets a
    freshly submitted force-restart ignore artifacts from the previous run,
    while still accepting a terminal state that wins the natural race against
    the queue worker's terminal update.

    Old queue payloads without either identity retain the legacy behavior;
    there is no reliable generation boundary to enforce for them.
    """

    queue_task_id, queue_run_id = _queue_generation(queue_entry)
    if not queue_task_id and not queue_run_id:
        return True

    job = payload.get("job")
    job = job if isinstance(job, dict) else {}
    engine_payload = payload.get("engine_payload")
    engine_payload = engine_payload if isinstance(engine_payload, dict) else {}
    payload_job_ids = {
        value for raw in (payload.get("job_id"), job.get("id")) if (value := normalize_text(raw))
    }
    payload_run_ids = {
        value
        for raw in (payload.get("run_id"), engine_payload.get("run_id"))
        if (value := normalize_text(raw))
    }
    if queue_task_id and payload_job_ids != {queue_task_id}:
        return False
    if queue_run_id and payload_run_ids != {queue_run_id}:
        return False
    return True


def current_generation_payloads(
    queue_entry: dict[str, Any] | None,
    state: dict[str, Any],
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_state = state if payload_matches_queue_generation(queue_entry, state) else {}
    current_report = report if payload_matches_queue_generation(queue_entry, report) else {}
    return current_state, current_report


__all__ = [
    "current_generation_payloads",
    "payload_matches_queue_generation",
    "queue_has_generation_identity",
]
