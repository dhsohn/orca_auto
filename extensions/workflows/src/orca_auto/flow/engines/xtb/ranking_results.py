from __future__ import annotations

from typing import Any

from .ranking_artifacts import (
    ranking_result_payload,
    write_ranking_success_logs,
    write_ranking_terminal_logs,
)
from .ranking_models import RankingCollectedResults, RankingDeps, RankingRunContext
from .ranking_selection import (
    ranking_failure_analysis,
    ranking_success_analysis,
    ranking_success_command,
    ranking_success_selection,
    ranking_unsuccessful_detail,
)


def ranking_terminal_result(
    context: RankingRunContext,
    *,
    status: str,
    reason: str,
    command: tuple[str, ...],
    stdout_text: str,
    stderr_text: str,
    candidate_results: list[dict[str, Any]],
    deps: RankingDeps,
) -> Any:
    logs = write_ranking_terminal_logs(
        context.job_dir,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    payload = ranking_result_payload(
        context,
        status=status,
        reason=reason,
        command=command,
        exit_code=1,
        logs=logs,
        finished_at=deps.now_utc_iso(),
        selected_input_xyz=str(context.candidate_paths[0].resolve()),
        candidate_count=len(context.candidate_paths),
        selected_candidate_paths=(),
        candidate_details=tuple(
            ranking_unsuccessful_detail(item, idx + 1) for idx, item in enumerate(candidate_results)
        ),
        analysis_summary=ranking_failure_analysis(
            candidate_results=candidate_results,
            top_n=context.top_n,
            failure_reason=reason,
        ),
    )
    return deps.result_cls(**payload)


def ranking_cancelled_result(
    context: RankingRunContext,
    collected: RankingCollectedResults,
    *,
    deps: RankingDeps,
) -> Any:
    return ranking_terminal_result(
        context,
        status="cancelled",
        reason="cancel_requested",
        command=tuple(collected.command_summary[0]) if collected.command_summary else tuple(),
        stdout_text="ranking cancelled: cancel_requested\n",
        stderr_text="",
        candidate_results=collected.candidate_results,
        deps=deps,
    )


def ranking_failed_result(
    context: RankingRunContext,
    collected: RankingCollectedResults,
    *,
    deps: RankingDeps,
) -> Any:
    failure_reason = "ranking_no_usable_energy"
    return ranking_terminal_result(
        context,
        status="failed",
        reason=failure_reason,
        command=tuple(),
        stdout_text=f"ranking failed: {failure_reason}\n",
        stderr_text="no candidate produced a usable xTB energy\n",
        candidate_results=collected.candidate_results,
        deps=deps,
    )


def ranking_completed_result(
    context: RankingRunContext,
    collected: RankingCollectedResults,
    *,
    usable: list[dict[str, Any]],
    deps: RankingDeps,
) -> Any:
    selection = ranking_success_selection(context, collected, usable)
    logs = write_ranking_success_logs(
        context.job_dir,
        candidate_results=collected.candidate_results,
        selected_paths=selection.selected_paths,
        usable_count=len(usable),
        failed_count=selection.failed_count,
        best=selection.best,
    )
    payload = ranking_result_payload(
        context,
        status="completed",
        reason="completed",
        command=ranking_success_command(
            selected=selection.selected,
            command_summary=collected.command_summary,
        ),
        exit_code=0,
        logs=logs,
        finished_at=deps.now_utc_iso(),
        selected_input_xyz=str(selection.best["candidate_path"]),
        candidate_count=len(collected.candidate_results),
        selected_candidate_paths=tuple(selection.selected_paths),
        candidate_details=tuple(selection.candidate_details),
        analysis_summary=ranking_success_analysis(
            candidate_results=collected.candidate_results,
            usable_count=len(usable),
            failed_count=selection.failed_count,
            best=selection.best,
            top_n=context.top_n,
            selected_paths=selection.selected_paths,
            command_summary=collected.command_summary,
        ),
    )
    return deps.result_cls(**payload)


__all__ = [
    "ranking_cancelled_result",
    "ranking_completed_result",
    "ranking_failed_result",
    "ranking_terminal_result",
]
