"""ORCA run-lifecycle notifications.

Builders turn a lifecycle event into a messenger-neutral
:class:`~orca_auto.core.messaging.Message`; the ``notify_*`` helpers deliver it
through a :class:`~orca_auto.core.messaging.MessageChannel` resolved from config.
Each per-messenger renderer owns the native markup, so switching the active
messenger changes only where the message goes, not what these builders emit.
The identity is carried on ``Message.author`` (the Discord embed author line),
keeping it out of the title.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.messaging import (
    Group,
    Message,
    MessageChannel,
    Severity,
    bold,
    code,
    field_row,
    group,
    line,
    raw,
    text,
    title_heading,
)

if TYPE_CHECKING:
    from .dft.monitor import ScanReport
    from .types import (
        QueueEnqueuedNotification,
        RetryNotification,
        RunFinishedNotification,
        RunStartedNotification,
    )

logger = logging.getLogger(__name__)

_MONITOR_DIVIDER = "─" * 28


def _status_icon(status: str) -> str:
    return activity_status_icon(status)


# --------------------------------------------------------------------------- #
# Run lifecycle message builders
# --------------------------------------------------------------------------- #
def run_started_message(event: RunStartedNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    current_inp = Path(event["current_inp"])
    status = str(event["status"]).strip().lower()
    resumed = bool(event.get("resumed"))
    title = "ORCA resumed" if resumed else "ORCA started"

    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row(
            "Attempt", raw(f"#{event['attempt_index']} ("), code(status or "running"), raw(")")
        ),
        field_row("Input", code(current_inp.name)),
        field_row("Max retries", text(event["max_retries"])),
    ]
    if resumed:
        fields.append(field_row("Mode", text("resumed run")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title=title,
        severity="info",
        groups=(group(*fields, heading=title_heading(title)),),
        author="orca_auto",
    )


def retry_message(event: RetryNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    failed_inp = Path(event["failed_inp"])
    next_inp = Path(event["next_inp"])

    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row(
            "Attempt",
            raw(
                f"{event['attempt_index']} failed; "
                f"retry {event['retry_number']}/{event['max_retries']} is starting"
            ),
        ),
        field_row(
            "Reason",
            code(event["analyzer_status"]),
            raw(" ("),
            text(event["analyzer_reason"]),
            raw(")"),
        ),
        field_row("Failed input", code(failed_inp.name)),
        field_row("Restart input", code(next_inp.name)),
    ]
    patch_summary = _format_patch_actions(event.get("patch_actions", []))
    if patch_summary:
        fields.append(field_row("Applied patches", text(patch_summary)))
    if event.get("resumed"):
        fields.append(field_row("Mode", text("resumed run")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title="ORCA retry",
        severity="warning",
        groups=(group(*fields, heading=title_heading("ORCA retry")),),
        author="orca_auto",
    )


def run_finished_message(event: RunFinishedNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    status = str(event["status"]).strip().lower()
    title = {
        "completed": "ORCA completed",
        "cancelled": "ORCA cancelled",
    }.get(status, "ORCA failed")
    if status == "completed":
        severity: Severity = "success"
    elif status == "cancelled":
        severity = "warning"
    else:
        severity = "error"
    status_text = status or "unknown"

    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row("Result", code(status_text)),
        field_row("Attempts", text(event["attempt_count"])),
        field_row("Reason", code(event["reason"])),
        field_row("Analyzer", code(event["analyzer_status"])),
    ]
    last_out_path = event.get("last_out_path")
    if isinstance(last_out_path, str) and last_out_path.strip():
        fields.append(field_row("Output", code(Path(last_out_path).name)))
    if event.get("skipped_execution"):
        fields.append(field_row("Mode", text("reused existing output")))
    elif event.get("resumed"):
        fields.append(field_row("Mode", text("resumed run")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title=title,
        severity=severity,
        groups=(group(*fields, heading=title_heading(title)),),
        author="orca_auto",
    )


def queue_enqueued_message(event: QueueEnqueuedNotification) -> Message:
    reaction_dir = Path(event["reaction_dir"])
    fields = [
        field_row("Job", text(reaction_dir.name or reaction_dir.as_posix())),
        field_row("Queue ID", code(event["queue_id"])),
        field_row("Priority", text(event["priority"])),
    ]
    if event.get("force"):
        fields.append(field_row("Mode", text("force re-enqueue")))
    fields.append(field_row("Directory", code(event["reaction_dir"])))
    return Message(
        title="ORCA queued",
        severity="info",
        groups=(group(*fields, heading=title_heading("ORCA queued")),),
        author="orca_auto",
    )


# --------------------------------------------------------------------------- #
# Monitor scan report
# --------------------------------------------------------------------------- #
def _notifiable_monitor_results(report: ScanReport) -> list:
    return [
        result for result in report.new_results if str(result.status).strip().lower() != "running"
    ]


def has_monitor_updates(report: ScanReport) -> bool:
    return bool(_notifiable_monitor_results(report) or report.failures)


def _monitor_dft_groups(report: ScanReport) -> list[Group]:
    results = _notifiable_monitor_results(report)
    if not results:
        return []
    groups: list[Group] = [
        group(
            line(raw("\U0001f9ea "), bold("New Calculations Detected"), raw(f"  ({len(results)})"))
        )
    ]
    for result in results:
        icon = _status_icon(str(result.status))
        calc_label = result.calc_type.upper() if result.calc_type else "-"
        items = [
            line(raw(f"{icon} "), bold(result.formula), raw("  ["), text(calc_label), raw("]")),
            line(raw("   \U0001f9ec "), text(result.method_basis)),
            line(raw("   ⚡ "), text(result.energy)),
            line(raw("   \U0001f4c2 "), code(result.path)),
        ]
        if result.note:
            items.append(line(raw("   ⚠️ "), text(result.note.strip("() "))))
        groups.append(group(*items))
    return groups


def _monitor_failure_groups(report: ScanReport) -> list[Group]:
    if not report.failures:
        return []
    count = len(report.failures)
    groups: list[Group] = [group(line(raw("⚠️ "), bold("Scan Parse Failures"), raw(f"  ({count})")))]
    for failure in report.failures[:5]:
        groups.append(
            group(
                line(raw("❌ "), code(failure.path)),
                line(raw("   "), text(failure.error_type), raw(": "), text(failure.error)),
            )
        )
    if count > 5:
        groups.append(group(line(raw(f"   ... and {count - 5} more"))))
    return groups


def monitor_message(report: ScanReport, *, now: datetime | None = None) -> Message:
    if now is None:
        current_time = datetime.now().astimezone()
    elif now.tzinfo is None:
        current_time = now.astimezone()
    else:
        current_time = now
    timestamp = current_time.strftime("%Y-%m-%d %H:%M %Z")

    groups: list[Group] = [
        group(line(raw("⚙️ "), bold("scan-notify"), raw("  "), code(timestamp))),
        group(line(raw(_MONITOR_DIVIDER))),
        group(
            line(raw("\U0001f50d "), bold("Scope")),
            line(
                raw("Filesystem discovery only. Use run-dir alerts for immediate lifecycle events.")
            ),
        ),
    ]
    groups.extend(_monitor_dft_groups(report))
    groups.extend(_monitor_failure_groups(report))
    return Message(title="scan-notify", severity="info", groups=tuple(groups), author="orca_auto")


# --------------------------------------------------------------------------- #
# Patch-action humanisation (shared with retry rendering)
# --------------------------------------------------------------------------- #
def _format_patch_actions(actions: list[str]) -> str | None:
    rendered: list[str] = []
    for action in actions[:4]:
        action_text = action.strip()
        if not action_text:
            continue
        rendered.append(_humanize_patch_action(action_text))
    if not rendered:
        return None
    if len(actions) > len(rendered):
        rendered.append("...")
    return ", ".join(rendered)


def _humanize_patch_action(action: str) -> str:
    labels = {
        "route_add_tightscf_slowconv": "TightSCF + SlowConv",
        "scf_maxiter_300": "SCF MaxIter 300",
        "geom_hessian_and_maxiter": "Geom Hessian + MaxIter 300",
        "geom_hessian_and_maxiter_500": "Geom Hessian + MaxIter 500",
        "maxcore_increased": "MaxCore increased",
        "route_add_looseopt": "LooseOpt",
        "geometry_restart_not_applied": "geometry restart not applied",
        "no_previous_xyz_file_found": "no previous xyz file found",
        "no_geometry_file_found": "no geometry file found",
        "no_recipe_applied": "no retry recipe applied",
    }
    if action.startswith("geometry_restart_from_"):
        source = action.removeprefix("geometry_restart_from_")
        return f"geometry restart from {source}"
    return labels.get(action, action.replace("_", " "))


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def notify_run_started_event(channel: MessageChannel, event: RunStartedNotification) -> bool:
    if not channel.enabled:
        logger.debug("run_started_notification_disabled")
        return False
    sent = channel.send(run_started_message(event)).sent
    _log_delivery(
        "run_started", sent, reaction_dir=event["reaction_dir"], attempt=event["attempt_index"]
    )
    return sent


def notify_retry_event(channel: MessageChannel, event: RetryNotification) -> bool:
    if not channel.enabled:
        logger.debug("retry_notification_disabled")
        return False
    sent = channel.send(retry_message(event)).sent
    _log_delivery("retry", sent, reaction_dir=event["reaction_dir"], retry=event["retry_number"])
    return sent


def notify_run_finished_event(channel: MessageChannel, event: RunFinishedNotification) -> bool:
    if not channel.enabled:
        logger.debug("run_finished_notification_disabled")
        return False
    sent = channel.send(run_finished_message(event)).sent
    _log_delivery("run_finished", sent, reaction_dir=event["reaction_dir"], status=event["status"])
    return sent


def notify_queue_enqueued_event(channel: MessageChannel, event: QueueEnqueuedNotification) -> bool:
    if not channel.enabled:
        logger.debug("queue_enqueued_notification_disabled")
        return False
    sent = channel.send(queue_enqueued_message(event)).sent
    _log_delivery(
        "queue_enqueued", sent, queue_id=event["queue_id"], reaction_dir=event["reaction_dir"]
    )
    return sent


def notify_monitor_report(channel: MessageChannel, report: ScanReport) -> bool:
    if not has_monitor_updates(report):
        return False
    return channel.send(monitor_message(report)).sent


def _log_delivery(kind: str, sent: bool, **context: object) -> None:
    detail = " ".join(f"{key}={value}" for key, value in context.items())
    if sent:
        logger.info("%s_notification_sent: %s", kind, detail)
    else:
        logger.warning("%s_notification_failed: %s", kind, detail)


__all__ = [
    "has_monitor_updates",
    "monitor_message",
    "notify_monitor_report",
    "notify_queue_enqueued_event",
    "notify_retry_event",
    "notify_run_finished_event",
    "notify_run_started_event",
    "queue_enqueued_message",
    "retry_message",
    "run_finished_message",
    "run_started_message",
]
