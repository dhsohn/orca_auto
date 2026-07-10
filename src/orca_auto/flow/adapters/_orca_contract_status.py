from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from orca_auto.orca.job_locations._generation import current_generation_payloads


class SafeIntFn(Protocol):
    def __call__(self, value: Any, *, default: int = 0) -> int: ...


@dataclass(frozen=True)
class StatusPayload:
    status: str
    analyzer_status: str
    reason: str
    completed_at: str


def attempt_count_impl(
    state: dict[str, Any],
    report: dict[str, Any],
    *,
    safe_int_fn: SafeIntFn,
) -> int:
    report_count = safe_int_fn(report.get("attempt_count"), default=-1)
    if report_count >= 0:
        return report_count
    attempts = state.get("attempts")
    if isinstance(attempts, list):
        return len(attempts)
    return 0


def max_retries_impl(
    state: dict[str, Any],
    report: dict[str, Any],
    *,
    safe_int_fn: SafeIntFn,
) -> int:
    report_value = safe_int_fn(report.get("max_retries"), default=-1)
    if report_value >= 0:
        return report_value
    return safe_int_fn(state.get("max_retries"), default=0)


def coerce_attempts_impl(
    state: dict[str, Any],
    report: dict[str, Any],
    *,
    normalize_text_fn: Callable[[Any], str],
    safe_int_fn: SafeIntFn,
) -> tuple[dict[str, Any], ...]:
    raw_attempts = report.get("attempts")
    if not isinstance(raw_attempts, list):
        raw_attempts = state.get("attempts")
    if not isinstance(raw_attempts, list):
        return ()

    attempts: list[dict[str, Any]] = []
    for raw in raw_attempts:
        if not isinstance(raw, dict):
            continue
        index = safe_int_fn(raw.get("index"), default=0)
        attempt_number = max(0, index - 1) if index > 0 else 0
        attempts.append(
            {
                "index": index,
                "attempt_number": attempt_number,
                "inp_path": normalize_text_fn(raw.get("inp_path")),
                "out_path": normalize_text_fn(raw.get("out_path")),
                "return_code": safe_int_fn(raw.get("return_code"), default=0),
                "analyzer_status": normalize_text_fn(raw.get("analyzer_status")),
                "analyzer_reason": normalize_text_fn(raw.get("analyzer_reason")),
                "markers": _coerce_markers(raw.get("markers")),
                "patch_actions": list(raw["patch_actions"])
                if isinstance(raw.get("patch_actions"), list)
                else [],
                "started_at": normalize_text_fn(raw.get("started_at")),
                "ended_at": normalize_text_fn(raw.get("ended_at")),
            }
        )
    return tuple(attempts)


def _coerce_markers(value: Any) -> dict[str, Any] | list[Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, list):
        return list(value)
    return []


def final_result_payload_impl(state: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    payload = report.get("final_result")
    if not isinstance(payload, dict):
        payload = state.get("final_result")
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items()}


def status_from_payloads_impl(
    *,
    queue_entry: dict[str, Any] | None,
    state: dict[str, Any],
    report: dict[str, Any],
    normalize_text_fn: Callable[[Any], str],
    normalize_bool_fn: Callable[[Any], bool],
) -> tuple[str, str, str, str]:
    payload = status_payload(
        queue_entry=queue_entry,
        state=state,
        report=report,
        normalize_text_fn=normalize_text_fn,
        normalize_bool_fn=normalize_bool_fn,
    )
    return payload.status, payload.analyzer_status, payload.reason, payload.completed_at


def status_payload(
    *,
    queue_entry: dict[str, Any] | None,
    state: dict[str, Any],
    report: dict[str, Any],
    normalize_text_fn: Callable[[Any], str],
    normalize_bool_fn: Callable[[Any], bool],
) -> StatusPayload:
    state, report = current_generation_payloads(queue_entry, state, report)
    queue = queue_entry or {}
    queue_status = normalize_text_fn(queue.get("status")).lower()
    cancel_requested = normalize_bool_fn(queue.get("cancel_requested"))
    state_status = normalize_text_fn(state.get("status")).lower()
    report_status = normalize_text_fn(report.get("status")).lower()
    final = final_status_source(state, report)
    final_status = normalize_text_fn(final.get("status")).lower()
    analyzer_status = normalize_text_fn(final.get("analyzer_status"))
    reason = normalize_text_fn(final.get("reason"))
    completed_at = normalize_text_fn(final.get("completed_at"))
    status = resolve_status(
        final_status, queue_status, cancel_requested, state_status, report_status
    )
    if status == "cancelled" and not reason:
        reason = "cancelled"
    return StatusPayload(status, analyzer_status, reason, completed_at)


def final_status_source(state: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    report_final = report.get("final_result")
    final_result = report_final if isinstance(report_final, dict) else state.get("final_result")
    return final_result if isinstance(final_result, dict) else {}


def resolve_status(
    final_status: str,
    queue_status: str,
    cancel_requested: bool,
    state_status: str,
    report_status: str,
) -> str:
    if final_status in {"completed", "failed"}:
        return final_status
    queue_status_map = {
        "cancelled": "cancelled",
        "pending": "queued",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
    }
    if queue_status == "running" and cancel_requested:
        return "cancel_requested"
    if queue_status in queue_status_map:
        return queue_status_map[queue_status]
    if state_status in {"completed", "failed"}:
        return state_status
    if state_status in {"created", "running", "retrying"}:
        return "running"
    if report_status in {"completed", "failed"}:
        return report_status
    return queue_status or state_status or "unknown"
