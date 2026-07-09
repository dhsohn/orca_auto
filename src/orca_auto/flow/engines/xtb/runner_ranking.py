from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig

from .ranking_artifacts import (
    ranking_result_payload,
    write_ranking_success_logs,
    write_ranking_terminal_logs,
)
from .ranking_execution import (
    collect_ranking_candidate_results,
    ranking_candidate_result,
)
from .ranking_inputs import (
    ranking_candidate_paths,
    ranking_candidate_run_dir,
    ranking_context,
    ranking_top_n,
    safe_rank_name,
)
from .ranking_models import (
    RankingCollectedResults,
    RankingDeps,
    RankingLogPaths,
    RankingRunContext,
    RankingSelection,
)
from .ranking_results import (
    ranking_cancelled_result,
    ranking_completed_result,
    ranking_failed_result,
    ranking_terminal_result,
)
from .ranking_selection import (
    rank_usable_candidates,
    ranking_failure_analysis,
    ranking_success_analysis,
    ranking_success_command,
    ranking_success_selection,
    ranking_unsuccessful_detail,
    ranking_was_cancelled,
    usable_ranking_candidates,
)


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
    ranking_root.mkdir(parents=True, exist_ok=True)
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
    "RankingCollectedResults",
    "RankingDeps",
    "RankingLogPaths",
    "RankingRunContext",
    "RankingSelection",
    "collect_ranking_candidate_results",
    "rank_usable_candidates",
    "ranking_candidate_paths",
    "ranking_candidate_result",
    "ranking_candidate_run_dir",
    "ranking_cancelled_result",
    "ranking_completed_result",
    "ranking_context",
    "ranking_failed_result",
    "ranking_failure_analysis",
    "ranking_result_payload",
    "ranking_success_analysis",
    "ranking_success_command",
    "ranking_success_selection",
    "ranking_terminal_result",
    "ranking_top_n",
    "ranking_unsuccessful_detail",
    "ranking_was_cancelled",
    "run_ranking_job",
    "safe_rank_name",
    "usable_ranking_candidates",
    "write_ranking_success_logs",
    "write_ranking_terminal_logs",
]
