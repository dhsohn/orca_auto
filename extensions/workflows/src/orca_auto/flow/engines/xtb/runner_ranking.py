from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.engine_process import recreate_confined_directory

from .ranking_execution import collect_ranking_candidate_results
from .ranking_inputs import ranking_candidate_paths, ranking_context
from .ranking_models import RankingCollectedResults, RankingDeps
from .ranking_results import (
    ranking_cancelled_result,
    ranking_completed_result,
    ranking_failed_result,
)
from .ranking_selection import ranking_was_cancelled, usable_ranking_candidates


def run_ranking_job(
    cfg: AppConfig,
    *,
    job_dir: Path,
    manifest: dict[str, Any],
    inputs: dict[str, Any],
    should_cancel: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    on_running_job: Callable[[Any | None], None] | None = None,
    terminate_process: Callable[[subprocess.Popen[str]], bool] | None = None,
    deps: RankingDeps,
) -> Any:
    candidate_paths = ranking_candidate_paths(inputs)
    if not candidate_paths:
        raise ValueError(f"No ranking candidates available in job directory: {job_dir}")

    ranking_root = job_dir / ".ranking_runs"
    recreate_confined_directory(job_dir, ranking_root, label="xTB ranking directory")
    context = ranking_context(
        cfg,
        job_dir=job_dir,
        manifest=manifest,
        inputs=inputs,
        candidate_paths=candidate_paths,
        deps=deps,
    )

    collected = RankingCollectedResults(
        *collect_ranking_candidate_results(
            cfg,
            ranking_root=ranking_root,
            manifest=manifest,
            candidate_paths=candidate_paths,
            should_cancel=should_cancel,
            prepare_running_job=prepare_running_job,
            on_running_job=on_running_job,
            terminate_process=terminate_process,
            deps=deps,
        )
    )
    if ranking_was_cancelled(collected, should_cancel=should_cancel):
        return ranking_cancelled_result(context, collected, deps=deps)

    usable = usable_ranking_candidates(collected.candidate_results)
    if not usable:
        return ranking_failed_result(context, collected, deps=deps)

    return ranking_completed_result(context, collected, usable=usable, deps=deps)


__all__ = [
    "RankingDeps",
    "run_ranking_job",
]
