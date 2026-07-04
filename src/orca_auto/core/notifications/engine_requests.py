from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EngineJobLifecycleRequest:
    headline: str
    job_id: str
    queue_id: str
    job_dir: Path
    selected_xyz: Path
    detail_values: Mapping[str, object]


@dataclass(frozen=True)
class EngineJobTerminalRequest:
    headline: str
    job_id: str
    queue_id: str
    status: str
    reason: str
    job_dir: Path
    selected_xyz: Path
    count_value: int
    detail_values: Mapping[str, object]
    extra_lines: list[str] | None = None


@dataclass(frozen=True)
class EngineJobFinishedRequest:
    job_id: str
    queue_id: str
    status: str
    reason: str
    job_dir: Path
    selected_xyz: Path
    count_value: int
    detail_values: Mapping[str, object]
    resource_request: dict[str, int] | None = None
    resource_actual: dict[str, int] | None = None


__all__ = [
    "EngineJobFinishedRequest",
    "EngineJobLifecycleRequest",
    "EngineJobTerminalRequest",
]
