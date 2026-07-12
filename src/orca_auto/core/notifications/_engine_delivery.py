from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.messaging import Severity

from ._engine_rendering import EngineEventField, event_lines

EngineLineSender = Callable[[Any, list[str], Severity], bool]


def is_workflow_child(job_dir: Path, *, engine: str) -> bool:
    parts = tuple(part for part in job_dir.parts if part)
    return any(part.endswith(f"_{engine}") for part in parts)


def send_job_event(
    cfg: Any,
    *,
    label: str,
    engine: str,
    job_dir: Path,
    headline: str,
    severity: Severity,
    fields: list[EngineEventField],
    send_fn: EngineLineSender,
    extra_lines: list[str] | None = None,
) -> bool:
    if is_workflow_child(job_dir, engine=engine):
        return True
    return send_fn(
        cfg,
        event_lines(
            label=label,
            headline=headline,
            fields=fields,
            extra_lines=extra_lines,
        ),
        severity,
    )
