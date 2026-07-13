from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .ranking_models import RankingCollectedResults, RankingRunContext, RankingSelection


def ranking_unsuccessful_detail(item: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "kind": "ranking_candidate",
        "path": item["candidate_path"],
        "candidate_run_dir_path": item["candidate_run_dir_path"],
        "energy_source": item["energy_source"],
        "status": item["status"],
        "reason": item["reason"],
        "exit_code": item["exit_code"],
        "selected": False,
    }


def ranking_failure_analysis(
    *,
    candidate_results: list[dict[str, Any]],
    top_n: int,
    failure_reason: str,
) -> dict[str, Any]:
    return {
        "ranking_metric": "total_energy",
        "evaluated_candidate_count": len(candidate_results),
        "candidate_paths": [item["candidate_path"] for item in candidate_results],
        "candidate_run_dir_paths": [item["candidate_run_dir_path"] for item in candidate_results],
        "candidate_results": candidate_results,
        "top_n": top_n,
        "failure_reason": failure_reason,
    }


def rank_usable_candidates(
    candidate_results: list[dict[str, Any]],
    *,
    top_n: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    finite_candidates = [
        item for item in candidate_results if math.isfinite(float(item["total_energy"]))
    ]
    ranked = sorted(finite_candidates, key=lambda item: float(item["total_energy"]))
    candidate_details: list[dict[str, Any]] = []
    selected_paths: list[str] = []
    for rank, item in enumerate(ranked, start=1):
        is_selected = rank <= top_n
        candidate_details.append(
            {
                "rank": rank,
                "kind": "ranking_candidate",
                "path": item["candidate_path"],
                "candidate_run_dir_path": item["candidate_run_dir_path"],
                "energy_source": item["energy_source"],
                "total_energy": item["total_energy"],
                "score": round(-float(item["total_energy"]), 6),
                "status": item["status"],
                "reason": item["reason"],
                "exit_code": item["exit_code"],
                "selected": is_selected,
            }
        )
        if is_selected:
            selected_paths.append(item["candidate_path"])
    return ranked, candidate_details, selected_paths


def ranking_was_cancelled(
    collected: RankingCollectedResults,
    *,
    should_cancel: Callable[[], bool] | None,
) -> bool:
    return any(item["status"] == "cancelled" for item in collected.candidate_results) or (
        should_cancel is not None and should_cancel()
    )


def usable_ranking_candidates(candidate_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable: list[dict[str, Any]] = []
    for item in candidate_results:
        if item.get("total_energy") is None or item["status"] != "completed":
            continue
        if isinstance(item["total_energy"], bool):
            continue
        try:
            energy = float(item["total_energy"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(energy):
            usable.append(item)
    return usable


def ranking_success_analysis(
    *,
    candidate_results: list[dict[str, Any]],
    usable_count: int,
    failed_count: int,
    best: dict[str, Any],
    top_n: int,
    selected_paths: list[str],
    command_summary: list[list[str]],
) -> dict[str, Any]:
    return {
        "ranking_metric": "total_energy",
        "evaluated_candidate_count": len(candidate_results),
        "usable_candidate_count": usable_count,
        "failed_candidate_count": failed_count,
        "candidate_paths": [item["candidate_path"] for item in candidate_results],
        "candidate_run_dir_paths": [item["candidate_run_dir_path"] for item in candidate_results],
        "candidate_results": candidate_results,
        "best_candidate_path": best["candidate_path"],
        "best_total_energy": best["total_energy"],
        "top_n": top_n,
        "selected_candidate_paths": list(selected_paths),
        "command_summary": command_summary,
    }


def ranking_success_command(
    *,
    selected: list[dict[str, Any]],
    command_summary: list[list[str]],
) -> tuple[str, ...]:
    if selected:
        return tuple(selected[0]["command"])
    if command_summary:
        return tuple(command_summary[0])
    return tuple()


def ranking_success_selection(
    context: RankingRunContext,
    collected: RankingCollectedResults,
    usable: list[dict[str, Any]],
) -> RankingSelection:
    ranked, candidate_details, selected_paths = rank_usable_candidates(
        usable,
        top_n=context.top_n,
    )
    selected = ranked[: context.top_n]
    return RankingSelection(
        candidate_details=candidate_details,
        selected_paths=selected_paths,
        selected=selected,
        failed_count=len(collected.candidate_results) - len(usable),
        best=ranked[0],
    )


__all__ = [
    "rank_usable_candidates",
    "ranking_failure_analysis",
    "ranking_success_analysis",
    "ranking_success_command",
    "ranking_success_selection",
    "ranking_unsuccessful_detail",
    "ranking_was_cancelled",
    "usable_ranking_candidates",
]
