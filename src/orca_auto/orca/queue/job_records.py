"""Project the location record of an ORCA queue row, and of its terminal state.

Submission, publication repair and the worker's attach project a queued or
running record from the captured metadata of the durable row; projection never
reopens a mutable source input to reconstruct submission-time facts. A terminal
record is projected from the generation's own terminal ``job_state.json``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.config.schema import positive_int_mapping
from orca_auto.core.statuses import TERMINAL_STATUSES, normalize_status

from ..config import AppConfig
from ..job_locations import record_from_artifacts, resource_dict, upsert_job_record
from ..state_reading import load_state, payload_matches_expected_job_id
from .entries import queue_entry_metadata, queue_entry_reaction_dir, queue_entry_task_id


def upsert_row_job_record(
    cfg: AppConfig,
    entry: Any,
    status: str,
    *,
    require_task_id: bool,
) -> None:
    """Upsert the row's location record with ``status`` from its captured metadata.

    A row without a task id raises when ``require_task_id`` (publication) and
    is skipped otherwise (the worker's advisory running record).
    """
    task_id = queue_entry_task_id(entry)
    if not task_id:
        if require_task_id:
            raise ValueError("ORCA publication repair requires a queue task_id")
        return
    reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser().resolve()
    selected_input, job_type, molecule_key, requested, actual = tracking_metadata_from_queue_entry(
        cfg,
        entry,
    )
    upsert_job_record(
        cfg,
        job_id=task_id,
        status=status,
        job_dir=reaction_dir,
        job_type=job_type,
        selected_input_xyz=selected_input,
        molecule_key=molecule_key,
        resource_request=requested,
        resource_actual=actual,
    )


def upsert_terminal_job_record(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    fallback_job_id: str | None = None,
    expected_job_id: str | None = None,
) -> bool:
    job_dir = Path(reaction_dir).expanduser().resolve()
    expected = str(expected_job_id or fallback_job_id or "").strip()
    state = load_state(job_dir)
    if expected and not payload_matches_expected_job_id(state, expected):
        state = None
    record = record_from_artifacts(
        job_dir=job_dir,
        state=dict(state) if state is not None else None,
        report=None,
        fallback_job_id=fallback_job_id or "",
    )
    if record is None or normalize_status(record.status) not in TERMINAL_STATUSES:
        return False
    upsert_job_record(
        cfg,
        job_id=record.job_id,
        status=record.status,
        job_dir=Path(record.original_run_dir).expanduser().resolve(),
        job_type=record.job_type,
        selected_input_xyz=record.selected_input_xyz,
        molecule_key=record.molecule_key,
        resource_request=dict(record.resource_request),
        resource_actual=dict(record.resource_actual),
    )
    return True


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

    requested = positive_int_mapping(metadata.get("resource_request"))
    snapshot = metadata.get("execution_snapshot")
    if not requested and isinstance(snapshot, dict):
        requested = positive_int_mapping(snapshot.get("resource_request"))
    if not requested:
        requested = resource_dict(
            cfg.resources.max_cores_per_task,
            cfg.resources.max_memory_gb_per_task,
        )

    actual = positive_int_mapping(metadata.get("resource_actual")) or dict(requested)
    return selected_input, job_type, molecule_key, requested, actual
