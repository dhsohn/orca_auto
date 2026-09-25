from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..report.publication import write_report_files
from ..state import finalize_state, now_utc_iso
from ..state_reading import state_path
from ..statuses import AnalyzerStatus, RunStatus
from ..types import (
    RunFinalResult,
    RunStartedNotification,
    RunState,
)


def run_status_text(status: RunStatus | str) -> str:
    return status.value if isinstance(status, RunStatus) else str(status)


def analyzer_status_text(status: AnalyzerStatus | str) -> str:
    return status.value if isinstance(status, AnalyzerStatus) else str(status)


def last_out_path_from_state(state: Mapping[str, Any]) -> str | None:
    best_index = -1
    best_path: str | None = None
    attempts = state.get("attempts")
    if isinstance(attempts, list):
        for position, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, Mapping):
                continue
            out_path = attempt.get("out_path")
            if isinstance(out_path, str) and out_path.strip():
                raw_index = attempt.get("index")
                index = raw_index if type(raw_index) is int and raw_index > 0 else position
                if index >= best_index:
                    best_index = index
                    best_path = out_path
    publications = state.get("scratch_publications")
    if isinstance(publications, list):
        for publication_record in publications:
            if not isinstance(publication_record, Mapping):
                continue
            inp_path = publication_record.get("inp_path")
            publication = publication_record.get("publication")
            if isinstance(inp_path, str) and isinstance(publication, Mapping):
                published_files = publication.get("published_files")
                expected_name = Path(inp_path).with_suffix(".out").name
                if isinstance(published_files, list) and expected_name in published_files:
                    raw_index = publication_record.get("attempt_index")
                    index = raw_index if type(raw_index) is int and raw_index > 0 else 0
                    if index >= best_index:
                        best_index = index
                        best_path = str(Path(inp_path).with_suffix(".out"))
    return best_path


def build_final_result(
    *,
    status: RunStatus | str,
    analyzer_status: AnalyzerStatus | str,
    reason: str,
    last_out_path: str | None,
    resumed: bool | None = None,
    extra: Mapping[str, object] | None = None,
) -> RunFinalResult:
    result: RunFinalResult = {
        "status": run_status_text(status),
        "analyzer_status": analyzer_status_text(analyzer_status),
        "reason": reason,
        "completed_at": now_utc_iso(),
        "last_out_path": last_out_path,
    }
    if resumed is not None:
        result["resumed"] = resumed
    if extra is not None:
        skipped_execution = extra.get("skipped_execution")
        if isinstance(skipped_execution, bool):
            result["skipped_execution"] = skipped_execution
        runner_error = extra.get("runner_error")
        if isinstance(runner_error, str) and runner_error:
            result["runner_error"] = runner_error
    return result


def build_run_started_notification(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    current_inp: Path,
    state: RunState,
    execution_index: int,
    status: RunStatus | str,
    attempt_started_at: str,
    resumed: bool,
) -> RunStartedNotification:
    return {
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "current_inp": str(current_inp),
        "run_id": str(state.get("run_id", "")),
        "attempt_index": execution_index,
        "status": run_status_text(status),
        "attempt_started_at": attempt_started_at,
        "resumed": resumed,
    }


def finalize_and_emit(
    reaction_dir: Path,
    state: RunState,
    selected_inp: Path,
    *,
    status: RunStatus | str,
    reason: str,
    final_result: RunFinalResult,
    exit_code: int,
    emit: Callable[[dict[str, Any]], None],
) -> int:
    """Publish the terminal result; the parent worker owns completion delivery."""
    finalize_state(
        reaction_dir,
        state,
        status=status,
        final_result=final_result,
    )
    payload: dict[str, Any] = {
        "status": run_status_text(status),
        "reason": reason,
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "run_state": str(state_path(reaction_dir)),
    }
    reports = write_report_files(reaction_dir, state)
    attempts = state.get("attempts")
    payload.update(
        {
            "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
            **reports,
        }
    )
    emit(payload)
    return exit_code


def exit_with_result(
    reaction_dir: Path,
    state: RunState,
    selected_inp: Path,
    *,
    status: RunStatus | str,
    analyzer_status: AnalyzerStatus | str,
    reason: str,
    last_out_path: str | None,
    resumed: bool | None,
    exit_code: int,
    emit: Callable[[dict[str, Any]], None],
    extra: Mapping[str, object] | None = None,
) -> int:
    final = build_final_result(
        status=status,
        analyzer_status=analyzer_status,
        reason=reason,
        last_out_path=last_out_path,
        resumed=resumed,
        extra=extra,
    )
    return finalize_and_emit(
        reaction_dir,
        state,
        selected_inp,
        status=status,
        reason=reason,
        final_result=final,
        exit_code=exit_code,
        emit=emit,
    )
