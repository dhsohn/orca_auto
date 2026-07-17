from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..inp_rewriter import rewrite_for_retry
from ..scants import highest_scants_surface_point, input_uses_scants
from ..state_machine import decide_attempt_outcome
from ..statuses import AnalyzerStatus, RunStatus
from ..types import AttemptRecord, RunFinishedNotification, RunState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MissingRetryInputRecoveryRequest:
    reaction_dir: Path
    state: RunState
    selected_inp: Path
    current_inp: Path
    retries_used: int
    to_resolved_local: Callable[[str], Path]
    save_state: Callable[[Path, RunState], Path]


@dataclass(frozen=True)
class ExecutionInputRequest:
    reaction_dir: Path
    selected_inp: Path
    state: RunState
    execution_index: int
    retries_used: int
    retry_inp_path: Callable[[Path, int], Path]
    to_resolved_local: Callable[[str], Path]
    save_state: Callable[[Path, RunState], Path]


@dataclass(frozen=True)
class ResumeTerminalDecisionRequest:
    reaction_dir: Path
    selected_inp: Path
    state: RunState
    resumed: bool
    max_retries: int
    last_out_path_from_state: Callable[[RunState], str | None]
    exit_with_result: Callable[..., int]
    emit: Callable[[dict[str, Any]], None]
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None


def _ensure_patch_actions_list(attempt: AttemptRecord) -> list[str]:
    existing = attempt.get("patch_actions")
    if isinstance(existing, list):
        return existing
    attempt["patch_actions"] = []
    return attempt["patch_actions"]


def _as_non_empty_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def recover_missing_retry_input(
    *,
    reaction_dir: Path,
    state: RunState,
    selected_inp: Path,
    current_inp: Path,
    retries_used: int,
    to_resolved_local: Callable[[str], Path],
    save_state: Callable[[Path, RunState], Path],
) -> tuple[bool, str]:
    return _recover_missing_retry_input(
        MissingRetryInputRecoveryRequest(
            reaction_dir=reaction_dir,
            state=state,
            selected_inp=selected_inp,
            current_inp=current_inp,
            retries_used=retries_used,
            to_resolved_local=to_resolved_local,
            save_state=save_state,
        )
    )


def _recover_missing_retry_input(request: MissingRetryInputRecoveryRequest) -> tuple[bool, str]:
    attempts = request.state.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return False, "resume_attempts_missing"

    last_attempt = attempts[-1]
    if not isinstance(last_attempt, dict):
        return False, "resume_last_attempt_invalid"

    source_inp_text = last_attempt.get("inp_path")
    if not isinstance(source_inp_text, str) or not source_inp_text.strip():
        return False, "resume_source_input_missing"

    source_inp = request.to_resolved_local(source_inp_text)
    if source_inp.resolve() == request.current_inp.resolve():
        source_inp = request.selected_inp.resolve()
        if not source_inp.exists():
            return False, "resume_fallback_source_missing"
    elif not source_inp.exists():
        return False, "resume_source_input_not_found"

    patch_actions = rewrite_for_retry(
        source_inp=source_inp,
        target_inp=request.current_inp,
        max_memory_gb=request.state.get("max_memory_gb_per_task"),
    )
    actions = _ensure_patch_actions_list(last_attempt)
    actions.append(f"resume_recreated_missing_input:{request.current_inp.name}")
    actions.extend([f"resume_{action}" for action in patch_actions])
    request.save_state(request.reaction_dir, request.state)
    return True, "resume_recovered"


def resolve_execution_input(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    execution_index: int,
    retries_used: int,
    retry_inp_path: Callable[[Path, int], Path],
    to_resolved_local: Callable[[str], Path],
    save_state: Callable[[Path, RunState], Path],
) -> tuple[Path | None, str | None]:
    return _resolve_execution_input(
        ExecutionInputRequest(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=state,
            execution_index=execution_index,
            retries_used=retries_used,
            retry_inp_path=retry_inp_path,
            to_resolved_local=to_resolved_local,
            save_state=save_state,
        )
    )


def _resolve_execution_input(request: ExecutionInputRequest) -> tuple[Path | None, str | None]:
    current_inp = (
        request.selected_inp
        if request.execution_index == 1
        else request.retry_inp_path(request.selected_inp, request.retries_used)
    )
    if current_inp.exists():
        return current_inp, None

    reason = f"missing_input_for_attempt_{request.execution_index}"
    if request.execution_index == 1:
        return None, reason

    try:
        recovered, recovery_reason = recover_missing_retry_input(
            reaction_dir=request.reaction_dir,
            state=request.state,
            selected_inp=request.selected_inp,
            current_inp=current_inp,
            retries_used=request.retries_used,
            to_resolved_local=request.to_resolved_local,
            save_state=request.save_state,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed while recovering missing retry input for attempt %d",
            request.execution_index,
            exc_info=True,
        )
        recovered = False
        recovery_reason = "resume_recovery_exception"

    if recovered and not current_inp.exists():
        recovered = False
        recovery_reason = "resume_recovery_no_output"
    if not recovered:
        return None, f"{reason}:{recovery_reason}"
    return current_inp, None


def resume_terminal_decision(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    resumed: bool,
    max_retries: int,
    last_out_path_from_state: Callable[[RunState], str | None],
    exit_with_result: Callable[..., int],
    emit: Callable[[dict[str, Any]], None],
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None,
) -> int | None:
    return _resume_terminal_decision(
        ResumeTerminalDecisionRequest(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=state,
            resumed=resumed,
            max_retries=max_retries,
            last_out_path_from_state=last_out_path_from_state,
            exit_with_result=exit_with_result,
            emit=emit,
            notify_finished=notify_finished,
        )
    )


def _scants_surface_exhausted_on_resume(state: RunState, last_attempt: Mapping[str, Any]) -> bool:
    """Match the live surface-table exhaustion shield during crash/resume.

    A finished ScanTS scan (its output has an actual-energy surface table) that
    did not verify a TS ends the run with ``scants_recipes_exhausted`` in the
    live retry path; without this, resume would treat ``ts_not_found`` as
    non-terminal and recover a missing retry input, re-running after an
    already-finished scan. Excludes the fresh zero-distance case, which still
    gets its one OptTS refinement fallback.
    """
    # Both the live-retry fallback (``scants_fallback_to_optts``) and the resume-path
    # conversion (``scants_resume_to_optts``, recorded as ``resume_scants_resume_to_optts``
    # once the resume-checkpoint prefix is applied) convert a finished ScanTS scan
    # into a one-shot OptTS attempt. Match either (substring, to tolerate the
    # ``resume_`` prefix).
    fallback_used = any(
        "scants_fallback_to_optts" in str(action) or "scants_resume_to_optts" in str(action)
        for attempt in state.get("attempts", [])
        if isinstance(attempt, dict)
        for action in (attempt.get("patch_actions") or [])
    )
    inp_raw = _as_non_empty_text(last_attempt.get("inp_path"))
    out_raw = _as_non_empty_text(last_attempt.get("out_path"))
    last_uses_scants = inp_raw is not None and input_uses_scants(Path(inp_raw))

    # The OptTS conversion is only *spent* once its attempt has actually run and been
    # recorded -- i.e. the last recorded attempt runs the converted OptTS input, no
    # longer ScanTS. Recognize exhaustion then, mirroring the live retry path (which
    # raises scants_recipes_exhausted after the OptTS fails): that attempt's input is
    # non-ScanTS and its output has no surface table, so the checks below would
    # otherwise return False and resume would re-run it. But if the last recorded
    # attempt is still the ScanTS scan, the prepared one-shot OptTS was interrupted
    # before completing -- fall through so the loop re-runs it exactly once, as the
    # live path does, instead of exhausting a fallback that never actually ran.
    if fallback_used and inp_raw is not None and not last_uses_scants:
        return True

    if inp_raw is None or out_raw is None:
        return False
    if not last_uses_scants:
        return False
    if highest_scants_surface_point(Path(out_raw)) is None:
        return False
    markers = last_attempt.get("markers")
    zero_distance = (
        bool(markers.get("geometry_zero_distance")) if isinstance(markers, dict) else (False)
    )
    # A fresh zero-distance abort (no fallback used yet) still deserves the one-shot
    # OptTS fallback, so it is not yet exhausted.
    return not zero_distance


def _resume_terminal_decision(request: ResumeTerminalDecisionRequest) -> int | None:
    if not request.resumed:
        return None

    attempts = request.state.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    last_attempt = attempts[-1]
    if not isinstance(last_attempt, dict):
        return None

    retries_used = len(attempts) - 1
    analyzer_status = (
        _as_non_empty_text(last_attempt.get("analyzer_status")) or AnalyzerStatus.INCOMPLETE.value
    )
    analyzer_reason = (
        _as_non_empty_text(last_attempt.get("analyzer_reason")) or "resume_last_attempt"
    )
    if decide_attempt_outcome(
        analyzer_status=analyzer_status,
        analyzer_reason=analyzer_reason,
        retries_used=retries_used,
        max_retries=request.max_retries,
    ) is None and _scants_surface_exhausted_on_resume(request.state, last_attempt):
        last_out_path = _as_non_empty_text(
            last_attempt.get("out_path")
        ) or request.last_out_path_from_state(request.state)
        logger.info("Resume finalizing finished ScanTS scan as scants_recipes_exhausted")
        return request.exit_with_result(
            request.reaction_dir,
            request.state,
            request.selected_inp,
            status=RunStatus.FAILED,
            analyzer_status=analyzer_status,
            reason="scants_recipes_exhausted",
            last_out_path=last_out_path,
            resumed=request.resumed,
            exit_code=1,
            emit=request.emit,
            notify_finished=request.notify_finished,
        )
    decision = decide_attempt_outcome(
        analyzer_status=analyzer_status,
        analyzer_reason=analyzer_reason,
        retries_used=retries_used,
        max_retries=request.max_retries,
    )
    if decision is None:
        return None

    logger.info(
        "Resume detected terminal previous attempt: analyzer_status=%s, reason=%s",
        analyzer_status,
        decision.reason,
    )
    last_out_path = _as_non_empty_text(
        last_attempt.get("out_path")
    ) or request.last_out_path_from_state(request.state)
    return request.exit_with_result(
        request.reaction_dir,
        request.state,
        request.selected_inp,
        status=decision.run_status,
        analyzer_status=analyzer_status,
        reason=decision.reason,
        last_out_path=last_out_path,
        resumed=request.resumed,
        exit_code=decision.exit_code,
        emit=request.emit,
        notify_finished=request.notify_finished,
    )
