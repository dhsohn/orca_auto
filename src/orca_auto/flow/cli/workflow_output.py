from __future__ import annotations

import json
from typing import Any

from orca_auto import cli_style
from orca_auto.cli_errors import emit_error as _emit_error
from orca_auto.cli_errors import emit_prefixed_error


def emit_json(payload: dict[str, Any], *, pretty: bool) -> None:
    indent = 2 if pretty else None
    print(json.dumps(payload, ensure_ascii=True, indent=indent))


def _emit_json_when_requested(
    payload: dict[str, Any], *, json_mode: bool, pretty: bool = True
) -> bool:
    if not json_mode:
        return False
    emit_json(payload, pretty=pretty)
    return True


def emit_error(message: Any, *, hint: str | None = None) -> None:
    _emit_error(message, hint=hint)


def emit_worker_lock_error(message: Any) -> None:
    emit_prefixed_error("worker_lock_error", message)


def emit_created_workflow(payload: dict[str, Any], *, json_mode: bool) -> int:
    if _emit_json_when_requested(payload, json_mode=json_mode):
        return 0

    print(f"{cli_style.label('workflow_id:')} {payload.get('workflow_id', '-')}")
    print(f"{cli_style.label('template_name:')} {payload.get('template_name', '-')}")
    print(
        f"{cli_style.label('workspace_dir:')} "
        f"{(payload.get('metadata') or {}).get('workspace_dir', '-')}"
    )
    print(f"{cli_style.label('stage_count:')} {len(payload.get('stages', []))}")
    return 0


def emit_restarted_workflow(payload: dict[str, Any], *, json_mode: bool) -> int:
    if _emit_json_when_requested(payload, json_mode=json_mode):
        return 0

    print(f"{cli_style.label('workflow_id:')} {payload.get('workflow_id', '-')}")
    print(f"{cli_style.label('status:')} {cli_style.status_text(payload.get('status', '-'))}")
    print(
        f"{cli_style.label('workflow_status:')} "
        f"{cli_style.status_text(payload.get('workflow_status', '-'))}"
    )
    print(
        f"{cli_style.label('previous_status:')} "
        f"{cli_style.status_text(payload.get('previous_status', '-'))}"
    )
    print(f"{cli_style.label('workspace_dir:')} {payload.get('workspace_dir', '-')}")
    print(f"{cli_style.label('restarted_count:')} {payload.get('restarted_count', 0)}")
    for item in payload.get("restarted_stages", []):
        print(
            f"- restarted {item.get('stage_id', '-')}"
            f" previous_status={item.get('previous_status', '-')}"
            f" previous_task_status={item.get('previous_task_status', '-')}"
        )
    return 0


def emit_worker_payload(payload: dict[str, Any], *, json_mode: bool, single_cycle: bool) -> None:
    if _emit_json_when_requested(payload, json_mode=json_mode, pretty=single_cycle):
        return

    print(
        f"cycle_started_at: {payload.get('cycle_started_at', '-')}"
        f" worker_session_id={payload.get('worker_session_id', '-')}"
        f" discovered={payload.get('discovered_count', 0)}"
        f" advanced={payload.get('advanced_count', 0)}"
        f" skipped={payload.get('skipped_count', 0)}"
        f" failed={payload.get('failed_count', 0)}"
    )
    for item in payload.get("workflow_results", []):
        print(
            f"- {item.get('workflow_id', '-')} template={item.get('template_name', '-')}"
            f" previous={item.get('previous_status', '-')}"
            f" status={item.get('status', '-')}"
            f" advanced={'yes' if item.get('advanced') else 'no'}"
        )
        if item.get("reason"):
            print(f"  reason={item.get('reason')}")


__all__ = [
    "emit_created_workflow",
    "emit_error",
    "emit_json",
    "emit_restarted_workflow",
    "emit_worker_lock_error",
    "emit_worker_payload",
]
