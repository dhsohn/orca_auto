"""The terminal decision of a run's one attempt and its publication."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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


def parse_analyzer_status(status_text: AnalyzerStatus | str) -> AnalyzerStatus | None:
    if isinstance(status_text, AnalyzerStatus):
        return status_text
    try:
        return AnalyzerStatus(str(status_text))
    except ValueError:
        return None


@dataclass(frozen=True)
class AttemptDecision:
    run_status: RunStatus
    reason: str
    exit_code: int


def decide_attempt_outcome(
    *,
    analyzer_status: AnalyzerStatus | str,
    analyzer_reason: str,
) -> AttemptDecision:
    """The run's terminal status and exit code from its one attempt's verdict."""
    if parse_analyzer_status(analyzer_status) == AnalyzerStatus.COMPLETED:
        return AttemptDecision(run_status=RunStatus.COMPLETED, reason=analyzer_reason, exit_code=0)
    return AttemptDecision(run_status=RunStatus.FAILED, reason=analyzer_reason, exit_code=1)


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


def _print_run_summary(payload: Mapping[str, Any]) -> None:
    fields = [
        ("status", "status"),
        ("job_dir", "job_dir"),
        ("reaction_dir", "job_dir"),
        ("selected_inp", "selected_inp"),
        ("attempt_count", "attempt_count"),
        ("reason", "reason"),
        ("run_state", "run_state"),
        ("report_json", "report_json"),
    ]
    printed_labels: set[str] = set()
    for key, label in fields:
        if key not in payload or label in printed_labels:
            continue
        print(f"{label}: {payload[key]}")
        printed_labels.add(label)


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
    extra: Mapping[str, object] | None = None,
) -> int:
    """Publish the terminal result and reports, print the run summary, return ``exit_code``.

    The parent worker owns completion delivery.
    """
    final_result = build_final_result(
        status=status,
        analyzer_status=analyzer_status,
        reason=reason,
        last_out_path=last_out_path,
        resumed=resumed,
        extra=extra,
    )
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
    _print_run_summary(payload)
    return exit_code
