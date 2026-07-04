from __future__ import annotations

from pathlib import Path

EngineEventField = tuple[str, object]


def terminal_headline(status: str) -> str:
    return {
        "completed": "Job finished",
        "failed": "Job failed",
        "cancelled": "Job cancelled",
    }.get(status, "Job finished")


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
