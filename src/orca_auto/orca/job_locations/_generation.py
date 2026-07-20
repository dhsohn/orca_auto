from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.metadata import mapping_metadata_value
from orca_auto.core.utils import normalize_text


def _queue_generation(queue_entry: dict[str, Any] | None) -> tuple[str, str]:
    queue = queue_entry or {}
    return (
        normalize_text(queue.get("task_id")),
        normalize_text(mapping_metadata_value(queue, "run_id")),
    )


def _has_generation_provenance(payload: dict[str, Any]) -> bool:
    provenance = payload.get("execution_provenance")
    engine_payload = payload.get("engine_payload")
    if not isinstance(provenance, Mapping) and isinstance(engine_payload, Mapping):
        provenance = engine_payload.get("execution_provenance")
    if not isinstance(provenance, Mapping):
        return False
    identity = provenance.get("execution_dir_identity")
    selected_identity = provenance.get("bound_selected_identity")
    owner_token = normalize_text(provenance.get("generation_owner_token"))
    execution_dir_text = normalize_text(provenance.get("execution_dir"))
    if (
        not isinstance(identity, Mapping)
        or not isinstance(selected_identity, Mapping)
        or not owner_token
        or not execution_dir_text
        or not normalize_text(selected_identity.get("path"))
    ):
        return False
    try:
        device = int(identity.get("device", -1))
        inode = int(identity.get("inode", -1))
        execution_dir = Path(execution_dir_text)
    except (OSError, TypeError, ValueError):
        return False
    return (
        device >= 0
        and inode > 0
        and execution_dir.is_absolute()
        and is_visible_generation_name(execution_dir.name)
    )


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

    A missing queue entry is accepted only for a self-identifying artifact;
    callers use that form after verifying the artifact's execution-generation
    provenance. An existing legacy queue row without either identity is
    unsupported and fails closed instead of adopting nearby artifacts.
    """

    queue_task_id, queue_run_id = _queue_generation(queue_entry)
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
    if queue_entry is None:
        return (
            len(payload_job_ids) == 1
            and len(payload_run_ids) == 1
            and _has_generation_provenance(payload)
        )
    if not queue_task_id and not queue_run_id:
        return False
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
]
