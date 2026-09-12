from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig

from .ranking_inputs import ranking_candidate_run_dir
from .ranking_models import RankingDeps


def _run_ranking_candidate(
    cfg: AppConfig,
    *,
    candidate_path: Path,
    candidate_run_dir: Path,
    manifest: dict[str, Any],
    should_cancel: Callable[[], bool] | None,
    prepare_running_job: Callable[[], None] | None,
    on_running_job: Callable[[Any | None], None] | None,
    terminate_process: Callable[[subprocess.Popen[str]], bool] | None,
    deps: RankingDeps,
) -> Any:
    if (
        should_cancel is None
        and prepare_running_job is None
        and on_running_job is None
        and terminate_process is None
    ):
        return deps.run_candidate_sp_job(
            cfg,
            candidate_xyz=candidate_path,
            candidate_run_dir=candidate_run_dir,
            manifest=manifest,
        )
    run_kwargs: dict[str, Any] = {
        "should_cancel": should_cancel,
        "on_running_job": on_running_job,
        "terminate_process": terminate_process,
    }
    if prepare_running_job is not None:
        run_kwargs["prepare_running_job"] = prepare_running_job
    return deps.run_candidate_sp_job(
        cfg,
        candidate_xyz=candidate_path,
        candidate_run_dir=candidate_run_dir,
        manifest=manifest,
        **run_kwargs,
    )


def ranking_candidate_result(
    *,
    candidate_path: Path,
    candidate_run_dir: Path,
    result: Any,
    energy: float | None,
    energy_source: str,
    energy_evidence_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_path": str(candidate_path.resolve()),
        "candidate_run_dir_path": str(candidate_run_dir.resolve()),
        "status": result.status,
        "reason": result.reason,
        "exit_code": result.exit_code,
        "selected_input_xyz": result.selected_input_xyz,
        "total_energy": energy,
        "energy_source": energy_source,
        "energy_evidence_identity": dict(energy_evidence_identity or {}),
        "command": list(result.command),
        "analysis_summary": dict(result.analysis_summary),
    }


def collect_ranking_candidate_results(
    cfg: AppConfig,
    *,
    ranking_root: Path,
    manifest: dict[str, Any],
    candidate_paths: list[Path],
    should_cancel: Callable[[], bool] | None,
    prepare_running_job: Callable[[], None] | None,
    on_running_job: Callable[[Any | None], None] | None,
    terminate_process: Callable[[subprocess.Popen[str]], bool] | None,
    deps: RankingDeps,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    candidate_results: list[dict[str, Any]] = []
    command_summary: list[list[str]] = []
    for index, candidate_path in enumerate(candidate_paths, start=1):
        if should_cancel is not None and should_cancel():
            break
        candidate_run_dir = ranking_candidate_run_dir(ranking_root, index, candidate_path)
        result = _run_ranking_candidate(
            cfg,
            candidate_path=candidate_path,
            candidate_run_dir=candidate_run_dir,
            manifest=manifest,
            should_cancel=should_cancel,
            prepare_running_job=prepare_running_job,
            on_running_job=on_running_job,
            terminate_process=terminate_process,
            deps=deps,
        )
        evidence_path = candidate_run_dir / "xtbout.json"
        evidence_before: dict[str, Any] | None = None
        if evidence_path.exists():
            try:
                evidence_before = _engine_runner.confined_output_identity(
                    candidate_run_dir,
                    evidence_path,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                evidence_before = None
        energy, energy_source = deps.extract_sp_energy(candidate_run_dir, candidate_path)
        evidence_after: dict[str, Any] | None = None
        if evidence_before is not None:
            try:
                evidence_after = _engine_runner.confined_output_identity(
                    candidate_run_dir,
                    evidence_path,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                evidence_after = None
        if evidence_before != evidence_after:
            energy, energy_source = None, ""
        command_summary.append(list(result.command))
        candidate_results.append(
            ranking_candidate_result(
                candidate_path=candidate_path,
                candidate_run_dir=candidate_run_dir,
                result=result,
                energy=energy,
                energy_source=energy_source,
                energy_evidence_identity=evidence_after,
            )
        )
        if result.status == "cancelled":
            break
    return candidate_results, command_summary


__all__ = [
    "_run_ranking_candidate",
    "collect_ranking_candidate_results",
    "ranking_candidate_result",
]
