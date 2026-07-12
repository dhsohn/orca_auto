from __future__ import annotations

from pathlib import Path

from orca_auto.core.messaging.richtext import Severity

EngineEventField = tuple[str, object]

_TERMINAL_PRESENTATION: dict[str, tuple[str, Severity]] = {
    "completed": ("Job finished", "success"),
    "failed": ("Job failed", "error"),
    "cancelled": ("Job cancelled", "warning"),
}


def terminal_headline(status: str) -> str:
    presentation = _TERMINAL_PRESENTATION.get(status)
    return presentation[0] if presentation is not None else "Job status unknown"


def terminal_severity(status: str) -> Severity:
    """Map a structured terminal status to presentation severity, failing closed."""
    presentation = _TERMINAL_PRESENTATION.get(status)
    return presentation[1] if presentation is not None else "info"


def optional_terminal_lines(
    *,
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
) -> list[str]:
    lines: list[str] = []
    if resource_request is not None:
        lines.append(f"resource_request: {resource_request}")
    if resource_actual is not None:
        lines.append(f"resource_actual: {resource_actual}")
    return lines


def job_event_fields(
    *,
    job_id: str,
    queue_id: str,
    job_dir: Path,
    selected_xyz: Path,
    selected_field_name: str,
    detail_fields: list[EngineEventField] | None = None,
    status: str | None = None,
    reason: str | None = None,
    count_field: EngineEventField | None = None,
) -> list[EngineEventField]:
    fields: list[EngineEventField] = [
        ("job_id", job_id),
        ("queue_id", queue_id),
    ]
    if status is not None:
        fields.append(("status", status))
    if reason is not None:
        fields.append(("reason", reason))
    fields.extend(detail_fields or [])
    fields.extend(
        [
            ("job_dir", job_dir.name),
            (selected_field_name, selected_xyz.name),
        ]
    )
    if count_field is not None:
        fields.append(count_field)
    return fields


def event_lines(
    *,
    label: str,
    headline: str,
    fields: list[EngineEventField],
    extra_lines: list[str] | None = None,
) -> list[str]:
    lines = [f"[{label}] {headline}"]
    lines.extend(f"{key}: {value}" for key, value in fields)
    if extra_lines:
        lines.extend(extra_lines)
    return lines
