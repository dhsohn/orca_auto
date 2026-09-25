"""Project queued/running location records from captured queue metadata.

Submission and publication repair consume the same durable row. Projection never
reopens a mutable source input to reconstruct submission-time facts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue.resource_requests import coerce_resource_request
from orca_auto.core.statuses import STATUS_QUEUED

from ..config import AppConfig
from ..job_locations import resource_dict, upsert_job_record
from .entries import queue_entry_metadata, queue_entry_reaction_dir, queue_entry_task_id


def upsert_queued_job_record(
    cfg: AppConfig,
    entry: Any,
) -> None:
    task_id = queue_entry_task_id(entry)
    if not task_id:
        raise ValueError("ORCA publication repair requires a queue task_id")
    reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser().resolve()
    selected_input, job_type, molecule_key, requested, actual = tracking_metadata_from_queue_entry(
        cfg,
        entry,
    )
    upsert_job_record(
        cfg,
        job_id=task_id,
        status=STATUS_QUEUED,
        job_dir=reaction_dir,
        job_type=job_type,
        selected_input_xyz=selected_input,
        molecule_key=molecule_key,
        resource_request=requested,
        resource_actual=actual,
    )


def tracking_metadata_from_queue_entry(
    cfg: AppConfig,
    entry: Any,
) -> tuple[str, str, str, dict[str, int], dict[str, int]]:
    metadata = queue_entry_metadata(entry)
    selected_inp = str(metadata.get("selected_inp") or "").strip()
    selected_xyz = str(metadata.get("selected_input_xyz") or "").strip()
    selected_input = str(
        selected_xyz
        or metadata.get("selected_input_path")
        or metadata.get("source_selected_inp")
        or selected_inp
    ).strip()
    # An older row may lack captured labels. Do not invent historical facts
    # from whatever input bytes happen to occupy the path now.
    job_type = str(metadata.get("job_type") or "").strip() or "other"
    molecule_key = str(metadata.get("molecule_key") or "").strip() or "unknown"

    requested = coerce_resource_request(metadata.get("resource_request"))
    snapshot = metadata.get("execution_snapshot")
    if not requested and isinstance(snapshot, dict):
        requested = coerce_resource_request(snapshot.get("resource_request"))
    if not requested:
        requested = resource_dict(
            cfg.resources.max_cores_per_task,
            cfg.resources.max_memory_gb_per_task,
        )

    actual = coerce_resource_request(metadata.get("resource_actual")) or dict(requested)
    return selected_input, job_type, molecule_key, requested, actual
