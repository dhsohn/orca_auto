from __future__ import annotations

from ._engine_transport import channel_line_sender
from .engine_jobs import build_engine_job_notifications

_ENGINE_LINE_SENDER = channel_line_sender()

_XTB_JOB_NOTIFICATIONS = build_engine_job_notifications(
    label="orca_auto_xtb",
    engine="xtb",
    selected_field_name="selected_input_xyz",
    detail_field_names=("job_type", "reaction_key"),
    terminal_count_field="candidate_count",
    send_fn=_ENGINE_LINE_SENDER,
)

notify_xtb_job_queued = _XTB_JOB_NOTIFICATIONS.notify_job_queued
notify_xtb_job_started = _XTB_JOB_NOTIFICATIONS.notify_job_started
notify_xtb_job_terminal = _XTB_JOB_NOTIFICATIONS.notify_job_terminal
notify_xtb_job_finished = _XTB_JOB_NOTIFICATIONS.notify_job_finished

_CREST_JOB_NOTIFICATIONS = build_engine_job_notifications(
    label="orca_auto_crest",
    engine="crest",
    selected_field_name="selected_xyz",
    detail_field_names=("mode",),
    terminal_count_field="retained_conformer_count",
    send_fn=_ENGINE_LINE_SENDER,
)

notify_crest_job_queued = _CREST_JOB_NOTIFICATIONS.notify_job_queued
notify_crest_job_started = _CREST_JOB_NOTIFICATIONS.notify_job_started
notify_crest_job_terminal = _CREST_JOB_NOTIFICATIONS.notify_job_terminal
notify_crest_job_finished = _CREST_JOB_NOTIFICATIONS.notify_job_finished


__all__ = [
    "_CREST_JOB_NOTIFICATIONS",
    "_ENGINE_LINE_SENDER",
    "_XTB_JOB_NOTIFICATIONS",
    "notify_crest_job_finished",
    "notify_crest_job_queued",
    "notify_crest_job_started",
    "notify_crest_job_terminal",
    "notify_xtb_job_finished",
    "notify_xtb_job_queued",
    "notify_xtb_job_started",
    "notify_xtb_job_terminal",
]
