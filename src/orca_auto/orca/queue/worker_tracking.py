from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.messaging import build_channel
from orca_auto.core.queue.engine.execution import coerce_resource_request
from orca_auto.core.statuses import (
    STATUS_QUEUED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    normalize_status,
)

from ..attempt.reporting import (
    build_run_finished_notification,
    finished_notification_already_sent,
    mark_finished_notification_sent,
)
from ..config import AppConfig
from ..inp_rewriter import read_resource_request_from_input
from ..input_artifacts import selected_input_artifacts
from ..job_locations import (
    record_from_artifacts,
    resolve_job_metadata,
    resource_dict,
    upsert_job_record,
)
from ..notifications import notify_run_finished_event
from ..state import load_state
from .entries import queue_entry_metadata, queue_entry_reaction_dir, queue_entry_task_id

logger = logging.getLogger(__name__)


def payload_job_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    job = payload.get("job")
    job = job if isinstance(job, dict) else {}
    return str(payload.get("job_id") or job.get("id") or "").strip()


def payload_matches_expected_job_id(payload: Any, expected_job_id: str | None) -> bool:
    expected = str(expected_job_id or "").strip()
    return not expected or payload_job_id(payload) == expected


def get_run_id_from_state(
    reaction_dir: str,
    *,
    expected_job_id: str | None = None,
) -> str | None:
    """Try to read run_id from the reaction_dir's job_state.json."""
    state = load_state(Path(reaction_dir))
    if state and payload_matches_expected_job_id(state, expected_job_id):
        return state.get("run_id")
    return None


def upsert_running_job_record(
    cfg: AppConfig,
    entry: Any,
) -> None:
    task_id = queue_entry_task_id(entry)
    if not task_id:
        return
    reaction_dir = Path(queue_entry_reaction_dir(entry)).expanduser().resolve()
    selected_input, job_type, molecule_key, requested, actual = tracking_metadata_from_queue_entry(
        cfg,
        entry,
        reaction_dir=reaction_dir,
    )
    upsert_job_record(
        cfg,
        job_id=task_id,
        status=STATUS_RUNNING,
        job_dir=reaction_dir,
        job_type=job_type,
        selected_input_xyz=selected_input,
        molecule_key=molecule_key,
        resource_request=requested,
        resource_actual=actual,
    )


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
        reaction_dir=reaction_dir,
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
    *,
    reaction_dir: Path,
) -> tuple[str, str, str, dict[str, int], dict[str, int]]:
    metadata = queue_entry_metadata(entry)
    selected_inp = str(metadata.get("selected_inp") or "").strip()
    selected_xyz = str(metadata.get("selected_input_xyz") or "").strip()
    selected_input = str(
        selected_xyz
        or metadata.get("selected_input_path")
        or selected_input_artifacts(selected_inp).selected_input_path
    ).strip()
    job_type = str(metadata.get("job_type") or "").strip()
    molecule_key = str(metadata.get("molecule_key") or "").strip()
    if not job_type or not molecule_key:
        derived_job_type, derived_molecule_key = resolve_job_metadata(
            selected_inp or selected_input,
            reaction_dir,
        )
        job_type = job_type or derived_job_type
        molecule_key = molecule_key or derived_molecule_key

    requested = coerce_resource_request(metadata.get("resource_request"))
    resource_inp = selected_inp or selected_input
    if not requested and resource_inp.lower().endswith(".inp"):
        selected_inp_path = Path(resource_inp).expanduser().resolve()
        if selected_inp_path.exists():
            requested = read_resource_request_from_input(selected_inp_path)
    if not requested:
        requested = resource_dict(
            cfg.resources.max_cores_per_task,
            cfg.resources.max_memory_gb_per_task,
        )

    actual = coerce_resource_request(metadata.get("resource_actual")) or dict(requested)
    return selected_input, job_type, molecule_key, requested, actual


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


def notify_terminal_job_from_state(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    expected_job_id: str | None = None,
) -> bool:
    channel = build_channel(cfg.messenger, logger=logger)
    if not channel.enabled:
        return False

    job_dir = Path(reaction_dir).expanduser().resolve()
    state = load_state(job_dir)
    if not state:
        logger.warning("Skipping terminal messenger notification; state missing for %s", job_dir)
        return False
    if not payload_matches_expected_job_id(state, expected_job_id):
        logger.warning(
            "Skipping terminal messenger notification; state generation mismatch for %s "
            "(expected_job_id=%s state_job_id=%s)",
            job_dir,
            str(expected_job_id or "").strip(),
            payload_job_id(state),
        )
        return False
    if finished_notification_already_sent(state):
        return False

    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        logger.warning(
            "Skipping terminal messenger notification; final_result missing for %s",
            job_dir,
        )
        return False

    selected_inp_text = str(state.get("selected_inp") or "").strip()
    selected_inp = Path(selected_inp_text) if selected_inp_text else job_dir / "-"
    status = str(final_result.get("status") or state.get("status") or "").strip()
    notification = build_run_finished_notification(
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        state=state,
        status=status,
        final_result=final_result,
    )
    sent = notify_run_finished_event(channel, notification)
    if sent:
        mark_finished_notification_sent(job_dir, state)
        logger.info("Terminal messenger notification sent by queue worker: %s", job_dir)
        return True

    logger.warning("Terminal messenger notification failed in queue worker: %s", job_dir)
    return False


__all__ = [
    "get_run_id_from_state",
    "notify_terminal_job_from_state",
    "payload_job_id",
    "payload_matches_expected_job_id",
    "tracking_metadata_from_queue_entry",
    "upsert_queued_job_record",
    "upsert_running_job_record",
    "upsert_terminal_job_record",
]
