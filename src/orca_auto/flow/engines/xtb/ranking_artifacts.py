from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import XTB_JOB_MANIFEST_FILE
from orca_auto.core.engine_process import atomic_write_confined_bytes

from .ranking_models import RankingLogPaths, RankingRunContext


def _write_text(path: Path, text: str) -> str:
    atomic_write_confined_bytes(
        path.parent,
        path,
        text.encode("utf-8"),
        label="xTB ranking summary log",
    )
    return str(path.resolve())


def _ranking_manifest_path(context: RankingRunContext) -> str:
    return str(
        context.inputs.get("manifest_path") or (context.job_dir / XTB_JOB_MANIFEST_FILE).resolve()
    )


def write_ranking_terminal_logs(
    job_dir: Path,
    *,
    stdout_text: str,
    stderr_text: str,
) -> RankingLogPaths:
    summary_stdout = job_dir / "ranking.stdout.log"
    summary_stderr = job_dir / "ranking.stderr.log"
    _write_text(summary_stdout, stdout_text)
    _write_text(summary_stderr, stderr_text)
    return RankingLogPaths(stdout=summary_stdout, stderr=summary_stderr)


def ranking_result_payload(
    context: RankingRunContext,
    *,
    status: str,
    reason: str,
    command: tuple[str, ...],
    exit_code: int,
    logs: RankingLogPaths,
    finished_at: str,
    selected_input_xyz: str,
    candidate_count: int,
    selected_candidate_paths: tuple[str, ...],
    candidate_details: tuple[dict[str, Any], ...],
    analysis_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "command": command,
        "exit_code": exit_code,
        "started_at": context.started_at,
        "finished_at": finished_at,
        "stdout_log": str(logs.stdout.resolve()),
        "stderr_log": str(logs.stderr.resolve()),
        "selected_input_xyz": selected_input_xyz,
        "job_type": "ranking",
        "reaction_key": str(context.inputs["reaction_key"]),
        "input_summary": dict(context.inputs["input_summary"]),
        "candidate_count": candidate_count,
        "selected_candidate_paths": selected_candidate_paths,
        "candidate_details": candidate_details,
        "analysis_summary": analysis_summary,
        "manifest_path": _ranking_manifest_path(context),
        "resource_request": context.resource_request,
        "resource_actual": context.resource_actual,
    }


def write_ranking_success_logs(
    job_dir: Path,
    *,
    candidate_results: list[dict[str, Any]],
    selected_paths: list[str],
    usable_count: int,
    failed_count: int,
    best: dict[str, Any],
) -> RankingLogPaths:
    summary_stdout = job_dir / "ranking.stdout.log"
    summary_stderr = job_dir / "ranking.stderr.log"
    stdout_lines = [
        f"ranking completed: evaluated={len(candidate_results)} selected={len(selected_paths)}",
        f"best_candidate: {best['candidate_path']}",
        f"best_total_energy: {best['total_energy']}",
    ]
    if failed_count:
        stdout_lines.append(f"failed_candidates: {failed_count}")
    stdout_lines.append(f"usable_candidates: {usable_count}")
    _write_text(summary_stdout, "\n".join(stdout_lines) + "\n")
    _write_text(summary_stderr, "")
    return RankingLogPaths(stdout=summary_stdout, stderr=summary_stderr)


__all__ = [
    "_ranking_manifest_path",
    "_write_text",
    "ranking_result_payload",
    "write_ranking_success_logs",
    "write_ranking_terminal_logs",
]
