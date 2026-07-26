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


def _payload_generation(payload: dict[str, Any]) -> tuple[str, str] | None:
    job = payload.get("job")
    job = job if isinstance(job, dict) else {}
    engine_payload = payload.get("engine_payload")
    engine_payload = engine_payload if isinstance(engine_payload, dict) else {}
    job_ids = {
        value
        for raw in (
            payload.get("job_id"),
            job.get("id"),
            job.get("task_id"),
            engine_payload.get("job_id"),
        )
        if (value := normalize_text(raw))
    }
    run_ids = {
        value
        for raw in (payload.get("run_id"), engine_payload.get("run_id"))
        if (value := normalize_text(raw))
    }
    if len(job_ids) != 1 or len(run_ids) != 1:
        return None
    return next(iter(job_ids)), next(iter(run_ids))


def payload_generation_provenance(payload: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    provenance = payload.get("execution_provenance")
    if isinstance(provenance, Mapping):
        candidates.append(dict(provenance))
    engine_payload = payload.get("engine_payload")
    if isinstance(engine_payload, Mapping):
        nested = engine_payload.get("execution_provenance")
        if isinstance(nested, Mapping):
            candidates.append(dict(nested))
    if not candidates or any(candidate != candidates[0] for candidate in candidates[1:]):
        return None
    canonical = candidates[0]
    provenance = canonical
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
        return None
    try:
        device = int(identity.get("device", -1))
        inode = int(identity.get("inode", -1))
        execution_dir = Path(execution_dir_text)
    except (OSError, TypeError, ValueError):
        return None
    if not (
        device >= 0
        and inode > 0
        and execution_dir.is_absolute()
        and is_visible_generation_name(execution_dir.name)
    ):
        return None
    return canonical


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
    provenance. An existing queue row without either identity fails closed
    instead of adopting nearby artifacts.
    """

    queue_task_id, queue_run_id = _queue_generation(queue_entry)
    payload_generation = _payload_generation(payload)
    if payload_generation is None:
        return False
    payload_job_id, payload_run_id = payload_generation
    if queue_entry is None:
        return payload_generation_provenance(payload) is not None
    # Every supported queue row has a task identity from submission onward.
    # A run-only or identity-less row cannot safely adopt a nearby artifact.
    if not queue_task_id:
        return False
    if payload_job_id != queue_task_id:
        return False
    if queue_run_id and payload_run_id != queue_run_id:
        return False
    return True


def current_generation_payloads(
    queue_entry: dict[str, Any] | None,
    state: dict[str, Any],
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_state = state if payload_matches_queue_generation(queue_entry, state) else {}
    current_report = report if payload_matches_queue_generation(queue_entry, report) else {}
    if (
        current_state
        and current_report
        and (
            _payload_generation(current_state) != _payload_generation(current_report)
            or payload_generation_provenance(current_state)
            != payload_generation_provenance(current_report)
        )
    ):
        return {}, {}
    return current_state, current_report


__all__ = [
    "current_generation_payloads",
    "payload_generation_provenance",
    "payload_matches_queue_generation",
]
