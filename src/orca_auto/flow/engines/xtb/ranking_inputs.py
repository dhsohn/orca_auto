from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig

from .job_inputs import MAX_RANKING_CANDIDATES
from .ranking_models import RankingDeps, RankingRunContext


def ranking_top_n(manifest: dict[str, Any]) -> int:
    raw = manifest.get("top_n", 3)
    if isinstance(raw, bool):
        raise ValueError("xTB ranking top_n must be a positive integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        raise ValueError("xTB ranking top_n must be a positive integer")
    if not 1 <= value <= MAX_RANKING_CANDIDATES:
        raise ValueError(f"xTB ranking top_n must be between 1 and {MAX_RANKING_CANDIDATES}")
    return value


def safe_rank_name(name: str, *, fallback: str) -> str:
    collapsed = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return collapsed or fallback


def ranking_candidate_run_dir(ranking_root: Path, index: int, candidate_path: Path) -> Path:
    name = safe_rank_name(candidate_path.stem, fallback=f"candidate_{index:02d}")
    return ranking_root / f"{index:02d}_{name}"


def ranking_candidate_paths(inputs: dict[str, Any]) -> list[Path]:
    return [
        Path(path)
        for path in inputs.get("input_summary", {}).get("candidate_paths", [])
        if str(path).strip()
    ]


def ranking_context(
    cfg: AppConfig,
    *,
    job_dir: Path,
    manifest: dict[str, Any],
    inputs: dict[str, Any],
    candidate_paths: list[Path],
    deps: RankingDeps,
) -> RankingRunContext:
    resource_request = deps.resource_request_dict(cfg, manifest)
    return RankingRunContext(
        job_dir=job_dir,
        started_at=deps.now_utc_iso(),
        candidate_paths=candidate_paths,
        inputs=inputs,
        top_n=ranking_top_n(manifest),
        resource_request=resource_request,
        resource_actual=deps.resource_actual_dict(resource_request),
    )


__all__ = [
    "ranking_candidate_paths",
    "ranking_candidate_run_dir",
    "ranking_context",
    "ranking_top_n",
    "safe_rank_name",
]
