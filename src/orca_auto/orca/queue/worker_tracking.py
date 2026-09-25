from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import (
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    normalize_status,
)

from ..config import AppConfig
from ..job_locations import (
    record_from_artifacts,
    upsert_job_record,
)
from ..notifications import (
    build_run_finished_notification,
    dispatch_notification,
    finished_notification_already_sent,
    notification_channel,
    notify_run_finished_event,
)
from ..run_lock import acquire_run_lock
from ..state import now_utc_iso, save_state
from ..state_reading import load_state, state_payload_job_id
from .entries import queue_entry_reaction_dir, queue_entry_task_id
from .job_records import tracking_metadata_from_queue_entry

logger = logging.getLogger(__name__)


def payload_matches_expected_job_id(payload: Any, expected_job_id: str | None) -> bool:
    expected = str(expected_job_id or "").strip()
    return not expected or state_payload_job_id(payload) == expected


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
    expected_run_id: str | None = None,
) -> bool:
    """Claim one best-effort notification, then dispatch an immutable message.

    A crash or saturated sender after the claim may lose an advisory message.
    Transport never writes state: the job directory may already hold a successor.
    """
    channel = notification_channel(cfg)
    if not channel.enabled:
        return False

    job_dir = Path(reaction_dir).expanduser().resolve()
    with acquire_run_lock(job_dir):
        state = load_state(job_dir)
        if not state or not payload_matches_expected_job_id(state, expected_job_id):
            return False
        if not state.get("run_id") or (expected_run_id and state.get("run_id") != expected_run_id):
            return False
        final_result = state.get("final_result")
        if (
            normalize_status(state.get("status")) not in TERMINAL_STATUSES
            or not isinstance(final_result, dict)
            or finished_notification_already_sent(state)
            or final_result.get("finished_notification_claimed_at")
        ):
            return False
        selected_inp_text = str(state.get("selected_inp") or "").strip()
        notification = build_run_finished_notification(
            reaction_dir=job_dir,
            selected_inp=Path(selected_inp_text) if selected_inp_text else job_dir / "-",
            state=state,
            status=str(final_result.get("status") or state.get("status") or "").strip(),
            final_result=final_result,
        )
        final_result["finished_notification_claimed_at"] = now_utc_iso()
        save_state(job_dir, state)
    return dispatch_notification(
        lambda: notify_run_finished_event(channel, notification), kind="terminal"
    )


__all__ = [
    "get_run_id_from_state",
    "notify_terminal_job_from_state",
    "payload_matches_expected_job_id",
    "upsert_running_job_record",
    "upsert_terminal_job_record",
]
