from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import STATUS_RUNNING

from ..config import AppConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrcaQueueWorkerTrackingCallbacks:
    build_run_finished_notification: Callable[..., Any]
    coerce_resource_request: Callable[[Any], dict[str, int] | None]
    finished_notification_already_sent: Callable[[Any], bool]
    load_report_json: Callable[[Path], Any]
    load_state: Callable[[Path], Any]
    mark_finished_notification_sent: Callable[..., Any]
    notify_run_finished_event: Callable[..., bool]
    queue_entry_metadata: Callable[[Any], dict[str, Any]]
    queue_entry_reaction_dir: Callable[[Any], str]
    queue_entry_task_id: Callable[[Any], str | None]
    read_resource_request_from_input: Callable[[Path], dict[str, int]]
    record_from_artifacts: Callable[..., Any]
    resolve_job_metadata: Callable[[str, Path], tuple[str, str]]
    resource_dict: Callable[[Any, Any], dict[str, int]]
    selected_input_artifacts: Callable[[str], Any]
    upsert_job_record: Callable[..., Any]


def get_run_id_from_state(
    reaction_dir: str,
    *,
    callbacks: OrcaQueueWorkerTrackingCallbacks,
) -> str | None:
    """Try to read run_id from the reaction_dir's job_state.json."""
    state = callbacks.load_state(Path(reaction_dir))
    if state:
        return state.get("run_id")
    return None


def upsert_running_job_record(
    cfg: AppConfig,
    entry: Any,
    *,
    callbacks: OrcaQueueWorkerTrackingCallbacks,
) -> None:
    task_id = callbacks.queue_entry_task_id(entry)
    if not task_id:
        return
    reaction_dir = Path(callbacks.queue_entry_reaction_dir(entry)).expanduser().resolve()
    selected_input, job_type, molecule_key, requested, actual = tracking_metadata_from_queue_entry(
        cfg,
        entry,
        reaction_dir=reaction_dir,
        callbacks=callbacks,
    )
    callbacks.upsert_job_record(
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


def tracking_metadata_from_queue_entry(
    cfg: AppConfig,
    entry: Any,
    *,
    reaction_dir: Path,
    callbacks: OrcaQueueWorkerTrackingCallbacks,
) -> tuple[str, str, str, dict[str, int], dict[str, int]]:
    metadata = callbacks.queue_entry_metadata(entry)
    selected_inp = str(metadata.get("selected_inp") or "").strip()
    selected_xyz = str(metadata.get("selected_input_xyz") or "").strip()
    selected_input = str(
        selected_xyz
        or metadata.get("selected_input_path")
        or callbacks.selected_input_artifacts(selected_inp).selected_input_path
    ).strip()
    job_type = str(metadata.get("job_type") or "").strip()
    molecule_key = str(metadata.get("molecule_key") or "").strip()
    if not job_type or not molecule_key:
        derived_job_type, derived_molecule_key = callbacks.resolve_job_metadata(
            selected_inp or selected_input,
            reaction_dir,
        )
        job_type = job_type or derived_job_type
        molecule_key = molecule_key or derived_molecule_key

    requested = callbacks.coerce_resource_request(metadata.get("resource_request"))
    resource_inp = selected_inp or selected_input
    if not requested and resource_inp.lower().endswith(".inp"):
        selected_inp_path = Path(resource_inp).expanduser().resolve()
        if selected_inp_path.exists():
            requested = callbacks.read_resource_request_from_input(selected_inp_path)
    if not requested:
        requested = callbacks.resource_dict(
            cfg.resources.max_cores_per_task,
            cfg.resources.max_memory_gb_per_task,
        )

    actual = callbacks.coerce_resource_request(metadata.get("resource_actual")) or dict(requested)
    return selected_input, job_type, molecule_key, requested, actual


def upsert_terminal_job_record(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    fallback_job_id: str | None = None,
    callbacks: OrcaQueueWorkerTrackingCallbacks,
) -> None:
    job_dir = Path(reaction_dir).expanduser().resolve()
    state = callbacks.load_state(job_dir)
    record = callbacks.record_from_artifacts(
        job_dir=job_dir,
        state=dict(state) if state is not None else None,
        report=callbacks.load_report_json(job_dir),
        fallback_job_id=fallback_job_id or "",
    )
    if record is None:
        return
    callbacks.upsert_job_record(
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


def notify_terminal_job_from_state(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    callbacks: OrcaQueueWorkerTrackingCallbacks,
) -> bool:
    if not cfg.telegram.enabled:
        return False

    job_dir = Path(reaction_dir).expanduser().resolve()
    state = callbacks.load_state(job_dir)
    if not state:
        logger.warning("Skipping terminal Telegram notification; state missing for %s", job_dir)
        return False
    if callbacks.finished_notification_already_sent(state):
        return False

    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        logger.warning(
            "Skipping terminal Telegram notification; final_result missing for %s",
            job_dir,
        )
        return False

    selected_inp_text = str(state.get("selected_inp") or "").strip()
    selected_inp = Path(selected_inp_text) if selected_inp_text else job_dir / "-"
    status = str(final_result.get("status") or state.get("status") or "").strip()
    notification = callbacks.build_run_finished_notification(
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        state=state,
        status=status,
        final_result=final_result,
    )
    sent = callbacks.notify_run_finished_event(cfg.telegram, notification)
    if sent:
        callbacks.mark_finished_notification_sent(job_dir, state)
        logger.info("Terminal Telegram notification sent by queue worker: %s", job_dir)
        return True

    logger.warning("Terminal Telegram notification failed in queue worker: %s", job_dir)
    return False


__all__ = [
    "OrcaQueueWorkerTrackingCallbacks",
    "get_run_id_from_state",
    "notify_terminal_job_from_state",
    "tracking_metadata_from_queue_entry",
    "upsert_running_job_record",
    "upsert_terminal_job_record",
]
