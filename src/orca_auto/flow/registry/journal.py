from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import (
    open_confined_log,
    read_confined_tail_lines,
    read_confined_text,
)
from orca_auto.core.utils import (
    coerce_mapping as _coerce_mapping,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.core.utils import (
    now_utc_iso,
    timestamped_token,
)
from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.persistence import fsync_directory

from . import _notifications as _notifications
from .store import _registry_lock_path

LOGGER = logging.getLogger(__name__)

WORKFLOW_JOURNAL_FILE_NAME = "workflow_registry.journal.jsonl"
CALLER_EVENT_LOOKUP_MAX_BYTES = 8 * 1024 * 1024
CALLER_EVENT_LOOKUP_MAX_LINES = 10_000


def workflow_journal_path(workflow_root: str | Path) -> Path:
    root = Path(workflow_root).expanduser().resolve()
    return root / WORKFLOW_JOURNAL_FILE_NAME


def _maybe_notify_journal_event(event: dict[str, Any], workflow_root: str | Path) -> None:
    event_type = _normalize_text(event.get("event_type"))
    if not _notifications.journal_notification_enabled(event_type):
        return
    try:
        channel = _notifications.messenger_channel_from_env()
        if channel is None:
            return
        result = channel.send(_notifications.journal_event_message(event, workflow_root))
        if not result.sent and not result.skipped:
            LOGGER.warning(
                "workflow_journal_notification_failed: workflow_root=%s event_type=%s error=%s",
                workflow_root,
                event_type,
                result.error or "unknown_error",
            )
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug(
            "workflow_journal_notification_failed: workflow_root=%s event_type=%s error=%s",
            workflow_root,
            event_type,
            exc,
        )
        return


def _find_caller_owned_event(
    lines: list[str],
    *,
    event_id: str,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    existing_event: dict[str, Any] | None = None
    for line in lines:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or raw.get("event_id") != event_id:
            continue
        candidate = {str(key): value for key, value in raw.items()}
        if candidate != event:
            raise ValueError("journal event id already exists with different content")
        existing_event = candidate
    return existing_event


def append_workflow_journal_event(
    workflow_root: str | Path,
    *,
    event_id: str = "",
    occurred_at: str = "",
    event_type: str,
    workflow_id: str = "",
    template_name: str = "",
    status: str = "",
    previous_status: str = "",
    reason: str = "",
    worker_session_id: str = "",
    stage_id: str = "",
    engine: str = "",
    task_kind: str = "",
    stage_status: str = "",
    previous_stage_status: str = "",
    reaction_handoff_status: str = "",
    previous_reaction_handoff_status: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_root = Path(workflow_root).expanduser().resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    resolved_event_id = _normalize_text(event_id)
    resolved_occurred_at = _normalize_text(occurred_at)
    if bool(resolved_event_id) != bool(resolved_occurred_at):
        raise ValueError("caller-owned journal events require both event_id and occurred_at")
    event = {
        "event_id": resolved_event_id or timestamped_token("wf_evt"),
        "occurred_at": resolved_occurred_at or now_utc_iso(),
        "event_type": _normalize_text(event_type),
        "workflow_id": _normalize_text(workflow_id),
        "template_name": _normalize_text(template_name),
        "status": _normalize_text(status),
        "previous_status": _normalize_text(previous_status),
        "reason": _normalize_text(reason),
        "worker_session_id": _normalize_text(worker_session_id),
        "stage_id": _normalize_text(stage_id),
        "engine": _normalize_text(engine),
        "task_kind": _normalize_text(task_kind),
        "stage_status": _normalize_text(stage_status),
        "previous_stage_status": _normalize_text(previous_stage_status),
        "reaction_handoff_status": _normalize_text(reaction_handoff_status),
        "previous_reaction_handoff_status": _normalize_text(previous_reaction_handoff_status),
        "metadata": _coerce_mapping(metadata),
    }
    existing_event: dict[str, Any] | None = None
    with file_lock(_registry_lock_path(resolved_root)):
        path = workflow_journal_path(resolved_root)
        if resolved_event_id and path.exists():
            lookup_lines = read_confined_tail_lines(
                resolved_root,
                path,
                label="workflow journal",
                max_lines=CALLER_EVENT_LOOKUP_MAX_LINES,
                max_bytes=CALLER_EVENT_LOOKUP_MAX_BYTES,
            )
            existing_event = _find_caller_owned_event(
                lookup_lines,
                event_id=resolved_event_id,
                event=event,
            )
            if existing_event is None:
                # Caller-owned events are normally the newest pending transition.
                # If the bounded suffix misses one, accept only a bounded whole
                # journal before appending; oversized journals fail closed rather
                # than risk duplicating a stable event ID.
                existing_event = _find_caller_owned_event(
                    read_confined_text(
                        resolved_root,
                        path,
                        label="workflow journal",
                        max_bytes=CALLER_EVENT_LOOKUP_MAX_BYTES,
                    ).splitlines(),
                    event_id=resolved_event_id,
                    event=event,
                )
            if existing_event is not None:
                with open_confined_log(
                    resolved_root,
                    path,
                    label="workflow journal",
                    append=True,
                ) as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                fsync_directory(path.parent)
        if existing_event is None:
            with open_confined_log(
                resolved_root,
                path,
                label="workflow journal",
                append=True,
                readable=True,
            ) as handle:
                file_size = os.fstat(handle.fileno()).st_size
                if file_size > 0 and os.pread(handle.fileno(), 1, file_size - 1) != b"\n":
                    handle.write("\n")
                handle.write(json.dumps(event, ensure_ascii=True))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(path.parent)
    if existing_event is not None:
        return existing_event
    _maybe_notify_journal_event(event, resolved_root)
    return event


def list_workflow_journal(workflow_root: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    resolved_root = Path(workflow_root).expanduser().resolve()
    path = workflow_journal_path(resolved_root)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with file_lock(_registry_lock_path(resolved_root)):
        if 0 < limit <= 1_000:
            lines = read_confined_tail_lines(
                resolved_root,
                path,
                label="workflow journal",
                max_lines=10_000,
            )
        else:
            lines = read_confined_text(
                resolved_root,
                path,
                label="workflow journal",
            ).splitlines()
        for line_number, line in enumerate(lines, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                LOGGER.debug(
                    "workflow_journal_row_parse_failed: path=%s line=%d error=%s",
                    path,
                    line_number,
                    exc,
                )
                continue
            if isinstance(raw, dict):
                rows.append({str(key): value for key, value in raw.items()})
    newest_first = list(reversed(rows))
    if limit > 0:
        return newest_first[:limit]
    return newest_first


__all__ = [
    "WORKFLOW_JOURNAL_FILE_NAME",
    "append_workflow_journal_event",
    "list_workflow_journal",
    "workflow_journal_path",
]
