from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig


@dataclass(frozen=True)
class RankingDeps:
    now_utc_iso: Callable[[], str]
    resource_request_dict: Callable[[AppConfig, dict[str, Any]], dict[str, int]]
    resource_actual_dict: Callable[[dict[str, int]], dict[str, int]]
    run_candidate_sp_job: Callable[..., Any]
    extract_sp_energy: Callable[[Path, Path], tuple[float | None, str]]
    result_cls: type[Any]


@dataclass(frozen=True)
class RankingRunContext:
    job_dir: Path
    started_at: str
    candidate_paths: list[Path]
    inputs: dict[str, Any]
    top_n: int
    resource_request: dict[str, int]
    resource_actual: dict[str, int]


@dataclass(frozen=True)
class RankingCollectedResults:
    candidate_results: list[dict[str, Any]]
    command_summary: list[list[str]]


@dataclass(frozen=True)
class RankingLogPaths:
    stdout: Path
    stderr: Path


@dataclass(frozen=True)
class RankingSelection:
    candidate_details: list[dict[str, Any]]
    selected_paths: list[str]
    selected: list[dict[str, Any]]
    failed_count: int
    best: dict[str, Any]


__all__ = [
    "RankingCollectedResults",
    "RankingDeps",
    "RankingLogPaths",
    "RankingRunContext",
    "RankingSelection",
]
