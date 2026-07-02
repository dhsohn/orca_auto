from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import finalize_state, now_utc_iso, state_path, write_report_files
from .statuses import AnalyzerStatus, RunStatus
from .types import (
    RetryNotification,
    RunFinalResult,
    RunFinishedNotification,
    RunStartedNotification,
    RunState,
)

logger = logging.getLogger(__name__)
FINISHED_NOTIFICATION_SENT_AT_KEY = "telegram_finished_notification_sent_at"


@dataclass(frozen=True)
class FinalResultRequest:
    status: RunStatus | str
    analyzer_status: AnalyzerStatus | str
    reason: str
    last_out_path: str | None
    resumed: bool | None = None
    extra: Mapping[str, object] | None = None


@dataclass(frozen=True)
class RetryNotificationRequest:
    reaction_dir: Path
    selected_inp: Path
    current_inp: Path
    out_path: Path
    next_inp: Path
    execution_index: int
    next_retry_number: int
    max_retries: int
    analysis_status: AnalyzerStatus | str
    analysis_reason: str
    patch_actions: list[str]
    resumed: bool


@dataclass(frozen=True)
class RunStartedNotificationRequest:
    reaction_dir: Path
    selected_inp: Path
    current_inp: Path
    state: RunState
    execution_index: int
    max_retries: int
    status: RunStatus | str
    attempt_started_at: str
    resumed: bool


@dataclass(frozen=True)
class RunFinishedNotificationRequest:
    reaction_dir: Path
    selected_inp: Path
    state: RunState
    status: RunStatus | str
    final_result: RunFinalResult


@dataclass(frozen=True)
class FinalizeAndEmitRequest:
    reaction_dir: Path
    state: RunState
    selected_inp: Path
    status: RunStatus | str
    reason: str
    final_result: RunFinalResult
    exit_code: int
    emit: Callable[[dict[str, Any]], None]
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None


@dataclass(frozen=True)
class ExitResultRequest:
    reaction_dir: Path
    state: RunState
    selected_inp: Path
    status: RunStatus | str
    analyzer_status: AnalyzerStatus | str
    reason: str
    last_out_path: str | None
    resumed: bool | None
    exit_code: int
    emit: Callable[[dict[str, Any]], None]
    extra: Mapping[str, object] | None = None
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None


def run_status_text(status: RunStatus | str) -> str:
    return status.value if isinstance(status, RunStatus) else str(status)


def analyzer_status_text(status: AnalyzerStatus | str) -> str:
    return status.value if isinstance(status, AnalyzerStatus) else str(status)


def last_out_path_from_state(state: Mapping[str, Any]) -> str | None:
    attempts = state.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    last = attempts[-1]
    if not isinstance(last, dict):
        return None
    out_path = last.get("out_path")
    if isinstance(out_path, str) and out_path.strip():
        return out_path
    return None


def build_final_result(
    *,
    status: RunStatus | str,
    analyzer_status: AnalyzerStatus | str,
    reason: str,
    last_out_path: str | None,
    resumed: bool | None = None,
    extra: Mapping[str, object] | None = None,
) -> RunFinalResult:
    return _build_final_result(
        FinalResultRequest(
            status=status,
            analyzer_status=analyzer_status,
            reason=reason,
            last_out_path=last_out_path,
            resumed=resumed,
            extra=extra,
        )
    )


def _build_final_result(request: FinalResultRequest) -> RunFinalResult:
    result: RunFinalResult = {
        "status": run_status_text(request.status),
        "analyzer_status": analyzer_status_text(request.analyzer_status),
        "reason": request.reason,
        "completed_at": now_utc_iso(),
        "last_out_path": request.last_out_path,
    }
    if request.resumed is not None:
        result["resumed"] = request.resumed
    if request.extra is not None:
        skipped_execution = request.extra.get("skipped_execution")
        if isinstance(skipped_execution, bool):
            result["skipped_execution"] = skipped_execution
        runner_error = request.extra.get("runner_error")
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
    return _build_retry_notification(
        RetryNotificationRequest(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            current_inp=current_inp,
            out_path=out_path,
            next_inp=next_inp,
            execution_index=execution_index,
            next_retry_number=next_retry_number,
            max_retries=max_retries,
            analysis_status=analysis_status,
            analysis_reason=analysis_reason,
            patch_actions=patch_actions,
            resumed=resumed,
        )
    )


def _build_retry_notification(request: RetryNotificationRequest) -> RetryNotification:
    return {
        "reaction_dir": str(request.reaction_dir),
        "selected_inp": str(request.selected_inp),
        "failed_inp": str(request.current_inp),
        "failed_out": str(request.out_path),
        "next_inp": str(request.next_inp),
        "attempt_index": request.execution_index,
        "retry_number": request.next_retry_number,
        "max_retries": request.max_retries,
        "analyzer_status": analyzer_status_text(request.analysis_status),
        "analyzer_reason": request.analysis_reason,
        "patch_actions": list(request.patch_actions),
        "resumed": request.resumed,
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
    return _build_run_started_notification(
        RunStartedNotificationRequest(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            current_inp=current_inp,
            state=state,
            execution_index=execution_index,
            max_retries=max_retries,
            status=status,
            attempt_started_at=attempt_started_at,
            resumed=resumed,
        )
    )


def _build_run_started_notification(
    request: RunStartedNotificationRequest,
) -> RunStartedNotification:
    return {
        "reaction_dir": str(request.reaction_dir),
        "selected_inp": str(request.selected_inp),
        "current_inp": str(request.current_inp),
        "run_id": str(request.state.get("run_id", "")),
        "attempt_index": request.execution_index,
        "max_retries": request.max_retries,
        "status": run_status_text(request.status),
        "attempt_started_at": request.attempt_started_at,
        "resumed": request.resumed,
    }


def build_run_finished_notification(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    status: RunStatus | str,
    final_result: RunFinalResult,
) -> RunFinishedNotification:
    return _build_run_finished_notification(
        RunFinishedNotificationRequest(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=state,
            status=status,
            final_result=final_result,
        )
    )


def _build_run_finished_notification(
    request: RunFinishedNotificationRequest,
) -> RunFinishedNotification:
    attempts = request.state.get("attempts")
    final_status = str(request.final_result.get("status", run_status_text(request.status)))
    analyzer_status = str(request.final_result.get("analyzer_status", ""))
    reason = str(request.final_result.get("reason", ""))
    completed_at = str(request.final_result.get("completed_at", ""))
    last_out_path = request.final_result.get("last_out_path")
    skipped_execution = bool(request.final_result.get("skipped_execution", False))
    return {
        "reaction_dir": str(request.reaction_dir),
        "selected_inp": str(request.selected_inp),
        "run_id": str(request.state.get("run_id", "")),
        "status": final_status,
        "analyzer_status": analyzer_status,
        "reason": reason,
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
        "max_retries": int(request.state.get("max_retries", 0)),
        "completed_at": completed_at,
        "last_out_path": last_out_path if isinstance(last_out_path, str) else None,
        "resumed": bool(request.final_result.get("resumed", False)),
        "skipped_execution": skipped_execution,
    }


def finished_notification_already_sent(state: Mapping[str, Any]) -> bool:
    final_result = state.get("final_result")
    if not isinstance(final_result, Mapping):
        return False
    return bool(str(final_result.get(FINISHED_NOTIFICATION_SENT_AT_KEY) or "").strip())


def mark_finished_notification_sent(
    reaction_dir: Path,
    state: RunState,
    *,
    sent_at: str | None = None,
) -> None:
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return
    final_result["telegram_finished_notification_sent_at"] = sent_at or now_utc_iso()
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
    return _finalize_and_emit(
        FinalizeAndEmitRequest(
            reaction_dir=reaction_dir,
            state=state,
            selected_inp=selected_inp,
            status=status,
            reason=reason,
            final_result=final_result,
            exit_code=exit_code,
            emit=emit,
            notify_finished=notify_finished,
        )
    )


def _finalize_and_emit(request: FinalizeAndEmitRequest) -> int:
    finalize_state(
        request.reaction_dir,
        request.state,
        status=run_status_text(request.status),
        final_result=request.final_result,
    )
    payload: dict[str, Any] = {
        "status": run_status_text(request.status),
        "reason": request.reason,
        "reaction_dir": str(request.reaction_dir),
        "selected_inp": str(request.selected_inp),
        "run_state": str(state_path(request.reaction_dir)),
    }
    if request.notify_finished is not None:
        notification = build_run_finished_notification(
            reaction_dir=request.reaction_dir,
            selected_inp=request.selected_inp,
            state=request.state,
            status=request.status,
            final_result=request.final_result,
        )
        try:
            notify_result = request.notify_finished(notification)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Finished notification callback failed for reaction_dir=%s",
                request.reaction_dir,
                exc_info=True,
            )
        else:
            if bool(notify_result):
                mark_finished_notification_sent(request.reaction_dir, request.state)
    reports = write_report_files(request.reaction_dir, request.state)
    attempts = request.state.get("attempts")
    payload.update(
        {
            "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
            **reports,
        }
    )
    request.emit(payload)
    return request.exit_code


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
    return _exit_with_result(
        ExitResultRequest(
            reaction_dir=reaction_dir,
            state=state,
            selected_inp=selected_inp,
            status=status,
            analyzer_status=analyzer_status,
            reason=reason,
            last_out_path=last_out_path,
            resumed=resumed,
            exit_code=exit_code,
            emit=emit,
            extra=extra,
            notify_finished=notify_finished,
        )
    )


def _exit_with_result(request: ExitResultRequest) -> int:
    final = build_final_result(
        status=request.status,
        analyzer_status=request.analyzer_status,
        reason=request.reason,
        last_out_path=request.last_out_path,
        resumed=request.resumed,
        extra=request.extra,
    )
    return finalize_and_emit(
        request.reaction_dir,
        request.state,
        request.selected_inp,
        status=request.status,
        reason=request.reason,
        final_result=final,
        exit_code=request.exit_code,
        emit=request.emit,
        notify_finished=request.notify_finished,
    )
