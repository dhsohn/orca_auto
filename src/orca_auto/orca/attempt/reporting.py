from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..state import finalize_state, now_utc_iso, state_path, write_report_files
from ..statuses import AnalyzerStatus, RunStatus
from ..types import (
    RetryNotification,
    RunFinalResult,
    RunFinishedNotification,
    RunStartedNotification,
    RunState,
)

logger = logging.getLogger(__name__)
FINISHED_NOTIFICATION_SENT_AT_KEY = "finished_notification_sent_at"
LEGACY_FINISHED_NOTIFICATION_SENT_AT_KEY = "telegram_finished_notification_sent_at"


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


def build_retry_notification(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    current_inp: Path,
    out_path: Path,
    next_inp: Path,
    execution_index: int,
    next_retry_number: int,
    max_retries: int,
    analysis_status: AnalyzerStatus | str,
    analysis_reason: str,
    patch_actions: list[str],
    resumed: bool,
) -> RetryNotification:
    return {
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "failed_inp": str(current_inp),
        "failed_out": str(out_path),
        "next_inp": str(next_inp),
        "attempt_index": execution_index,
        "retry_number": next_retry_number,
        "max_retries": max_retries,
        "analyzer_status": analyzer_status_text(analysis_status),
        "analyzer_reason": analysis_reason,
        "patch_actions": list(patch_actions),
        "resumed": resumed,
    }


def build_run_started_notification(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    current_inp: Path,
    state: RunState,
    execution_index: int,
    max_retries: int,
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
        "max_retries": max_retries,
        "status": run_status_text(status),
        "attempt_started_at": attempt_started_at,
        "resumed": resumed,
    }


def build_run_finished_notification(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    status: RunStatus | str,
    final_result: RunFinalResult,
) -> RunFinishedNotification:
    attempts = state.get("attempts")
    final_status = str(final_result.get("status", run_status_text(status)))
    analyzer_status = str(final_result.get("analyzer_status", ""))
    reason = str(final_result.get("reason", ""))
    completed_at = str(final_result.get("completed_at", ""))
    last_out_path = final_result.get("last_out_path")
    skipped_execution = bool(final_result.get("skipped_execution", False))
    return {
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "run_id": str(state.get("run_id", "")),
        "status": final_status,
        "analyzer_status": analyzer_status,
        "reason": reason,
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "max_retries": int(state.get("max_retries", 0)),
        "completed_at": completed_at,
        "last_out_path": last_out_path if isinstance(last_out_path, str) else None,
        "resumed": bool(final_result.get("resumed", False)),
        "skipped_execution": skipped_execution,
    }


def finished_notification_already_sent(state: Mapping[str, Any]) -> bool:
    final_result = state.get("final_result")
    if not isinstance(final_result, Mapping):
        return False
    return any(
        bool(str(final_result.get(key) or "").strip())
        for key in (
            FINISHED_NOTIFICATION_SENT_AT_KEY,
            LEGACY_FINISHED_NOTIFICATION_SENT_AT_KEY,
        )
    )


def mark_finished_notification_sent(
    reaction_dir: Path,
    state: RunState,
    *,
    sent_at: str | None = None,
) -> None:
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return
    timestamp = sent_at or now_utc_iso()
    # Dual-write for one compatibility window: new readers use the neutral key
    # while a rollback to the previous release still sees the legacy marker and
    # does not resend an already delivered terminal notification.
    final_result["finished_notification_sent_at"] = timestamp
    final_result["telegram_finished_notification_sent_at"] = timestamp
    state["final_result"] = final_result
    finalize_state(
        reaction_dir,
        state,
        status=str(state.get("status") or final_result.get("status") or ""),
        final_result=final_result,
    )
    write_report_files(reaction_dir, state)


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
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None,
) -> int:
    finalize_state(
        reaction_dir,
        state,
        status=run_status_text(status),
        final_result=final_result,
    )
    payload: dict[str, Any] = {
        "status": run_status_text(status),
        "reason": reason,
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(selected_inp),
        "run_state": str(state_path(reaction_dir)),
    }
    if notify_finished is not None:
        notification = build_run_finished_notification(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=state,
            status=status,
            final_result=final_result,
        )
        try:
            notify_result = notify_finished(notification)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Finished notification callback failed for reaction_dir=%s",
                reaction_dir,
                exc_info=True,
            )
        else:
            if bool(notify_result):
                mark_finished_notification_sent(reaction_dir, state)
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
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None,
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
        notify_finished=notify_finished,
    )
