from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..completion_rules import detect_completion_mode
from ..orca_runner import WorkerShutdownInterrupt
from ..out_analyzer import OutAnalysis, analyze_output
from ..retry_policy import RetryRecipeName, effective_max_retries
from ..state import now_utc_iso, save_state
from ..state_machine import decide_attempt_outcome
from ..statuses import AnalyzerStatus, RunStatus
from ..types import (
    AttemptRecord,
    RetryNotification,
    RunFinishedNotification,
    RunStartedNotification,
    RunState,
)
from .notifications import AttemptStartedNotification, notify_attempt_started
from .reporting import exit_with_result as _exit_with_result
from .reporting import last_out_path_from_state as _last_out_path_from_state
from .resume import resolve_execution_input, resume_terminal_decision
from .retry import (
    RetryAttemptRequest,
    prepare_resumed_checkpoint_input,
    prepare_retry_attempt,
    retry_recipe_step,
    state_pending_scants_reverse_after_endpoint_scan,
)

logger = logging.getLogger(__name__)


class RunResultLike(Protocol):
    out_path: str
    return_code: int


class RunnerLike(Protocol):
    def run(self, inp_path: Path) -> RunResultLike: ...


@dataclass(frozen=True)
class AttemptRunContext:
    reaction_dir: Path
    selected_inp: Path
    state: RunState
    resumed: bool
    runner: RunnerLike
    max_retries: int
    retry_inp_path: Callable[[Path, int], Path]
    to_resolved_local: Callable[[str], Path]
    emit: Callable[[dict[str, Any]], None]
    notify_started: Callable[[RunStartedNotification], Any] | None
    notify_finished: Callable[[RunFinishedNotification], Any] | None
    notify_retry: Callable[[RetryNotification], Any] | None


@dataclass
class AttemptLoopState:
    execution_index: int
    first_execution_index: int

    @property
    def retries_used(self) -> int:
        return self.execution_index - 1

    def advance(self) -> None:
        self.execution_index += 1


@dataclass(frozen=True)
class ResolvedAttemptInput:
    inp_path: Path | None
    missing_reason: str | None
    patch_actions: list[str]


@dataclass(frozen=True)
class AttemptStep:
    current_inp: Path
    patch_actions: list[str]


@dataclass(frozen=True)
class RecordedAttemptResult:
    current_inp: Path
    out_path: Path
    analysis: OutAnalysis


def _retry_recipe_step(retry_number: int) -> RetryRecipeName:
    return retry_recipe_step(retry_number)


def _mark_attempt_started(
    reaction_dir: Path,
    state: RunState,
    *,
    retries_used: int,
) -> tuple[str, RunStatus]:
    started_at = now_utc_iso()
    current_status = RunStatus.RUNNING if retries_used == 0 else RunStatus.RETRYING
    state["status"] = current_status.value
    save_state(reaction_dir, state)
    return started_at, current_status


def _run_and_record_attempt(
    reaction_dir: Path,
    state: RunState,
    *,
    current_inp: Path,
    execution_index: int,
    started_at: str,
    runner: RunnerLike,
    patch_actions: list[str] | None = None,
) -> tuple[Path, OutAnalysis]:
    logger.info("Attempt %d starting: %s", execution_index, current_inp)
    run_result = runner.run(current_inp)
    out_path = Path(run_result.out_path)

    mode = detect_completion_mode(current_inp)
    analysis = analyze_output(out_path, mode)
    attempt: AttemptRecord = {
        "index": execution_index,
        "inp_path": str(current_inp),
        "out_path": str(out_path),
        "return_code": run_result.return_code,
        "analyzer_status": analysis.status,
        "analyzer_reason": analysis.reason,
        "markers": dict(analysis.markers),
        "patch_actions": list(patch_actions or []),
        "started_at": started_at,
        "ended_at": now_utc_iso(),
    }
    state["attempts"].append(attempt)
    save_state(reaction_dir, state)

    logger.info(
        "Attempt %d finished: return_code=%d, status=%s",
        execution_index,
        run_result.return_code,
        analysis.status,
    )
    return out_path, analysis


def _finish_attempt(
    ctx: AttemptRunContext,
    *,
    status: RunStatus,
    analyzer_status: AnalyzerStatus | str,
    reason: str,
    last_out_path: str | None,
    exit_code: int,
    extra: dict[str, Any] | None = None,
) -> int:
    return _exit_with_result(
        ctx.reaction_dir,
        ctx.state,
        ctx.selected_inp,
        status=status,
        analyzer_status=analyzer_status,
        reason=reason,
        last_out_path=last_out_path,
        resumed=ctx.resumed,
        exit_code=exit_code,
        emit=ctx.emit,
        extra=extra,
        notify_finished=ctx.notify_finished,
    )


def _resume_attempts_if_terminal(ctx: AttemptRunContext) -> int | None:
    return resume_terminal_decision(
        reaction_dir=ctx.reaction_dir,
        selected_inp=ctx.selected_inp,
        state=ctx.state,
        resumed=ctx.resumed,
        max_retries=ctx.max_retries,
        last_out_path_from_state=_last_out_path_from_state,
        exit_with_result=_exit_with_result,
        emit=ctx.emit,
        notify_finished=ctx.notify_finished,
    )


def _resolve_current_attempt_input(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
) -> ResolvedAttemptInput:
    current_inp, missing_reason = resolve_execution_input(
        reaction_dir=ctx.reaction_dir,
        selected_inp=ctx.selected_inp,
        state=ctx.state,
        execution_index=loop.execution_index,
        retries_used=loop.retries_used,
        retry_inp_path=ctx.retry_inp_path,
        retry_recipe_step=_retry_recipe_step,
        to_resolved_local=ctx.to_resolved_local,
        save_state=save_state,
    )
    if current_inp is None:
        return ResolvedAttemptInput(None, missing_reason, [])

    resume_inp, patch_actions = prepare_resumed_checkpoint_input(
        resumed=ctx.resumed,
        current_inp=current_inp,
        reaction_dir=ctx.reaction_dir,
    )
    if resume_inp is not None:
        return ResolvedAttemptInput(resume_inp, None, patch_actions)
    return ResolvedAttemptInput(current_inp, None, [])


def _finish_missing_attempt_input(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
    *,
    missing_reason: str | None,
) -> int:
    return _finish_attempt(
        ctx,
        status=RunStatus.FAILED,
        analyzer_status=AnalyzerStatus.INCOMPLETE,
        reason=missing_reason or f"missing_input_for_attempt_{loop.execution_index}",
        last_out_path=None,
        exit_code=1,
    )


def _finish_attempt_exception(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
    current_inp: Path,
    exc: BaseException,
) -> int:
    if isinstance(exc, KeyboardInterrupt):
        logger.warning("Interrupted by user during attempt %d", loop.execution_index)
        return _finish_attempt(
            ctx,
            status=RunStatus.FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="interrupted_by_user",
            last_out_path=str(current_inp.with_suffix(".out")),
            exit_code=130,
        )

    logger.exception("ORCA runner crashed during attempt %d: %s", loop.execution_index, exc)
    return _finish_attempt(
        ctx,
        status=RunStatus.FAILED,
        analyzer_status=AnalyzerStatus.INCOMPLETE,
        reason="runner_exception",
        last_out_path=str(current_inp.with_suffix(".out")),
        exit_code=1,
        extra={"runner_error": str(exc)},
    )


def _mark_and_notify_attempt_started(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
    current_inp: Path,
) -> str:
    started_at, current_status = _mark_attempt_started(
        ctx.reaction_dir,
        ctx.state,
        retries_used=loop.retries_used,
    )
    notify_attempt_started(
        AttemptStartedNotification(
            reaction_dir=ctx.reaction_dir,
            selected_inp=ctx.selected_inp,
            current_inp=current_inp,
            state=ctx.state,
            execution_index=loop.execution_index,
            first_execution_index=loop.first_execution_index,
            max_retries=ctx.max_retries,
            status=current_status,
            attempt_started_at=started_at,
            resumed=ctx.resumed,
            notify_started=ctx.notify_started,
        )
    )
    return started_at


def _finish_retry_limit_if_needed(ctx: AttemptRunContext, loop: AttemptLoopState) -> int | None:
    if loop.retries_used > ctx.max_retries:
        return _finish_attempt(
            ctx,
            status=RunStatus.FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="retry_limit_reached",
            last_out_path=_last_out_path_from_state(ctx.state),
            exit_code=1,
        )
    return None


def _resolve_attempt_step(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
) -> tuple[AttemptStep | None, int | None]:
    resolved_input = _resolve_current_attempt_input(ctx, loop)
    current_inp = resolved_input.inp_path
    if current_inp is None:
        return None, _finish_missing_attempt_input(
            ctx,
            loop,
            missing_reason=resolved_input.missing_reason,
        )
    return AttemptStep(current_inp=current_inp, patch_actions=resolved_input.patch_actions), None


def _run_attempt_step(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
    step: AttemptStep,
) -> tuple[RecordedAttemptResult | None, int | None]:
    started_at = _mark_and_notify_attempt_started(ctx, loop, step.current_inp)
    try:
        out_path, analysis = _run_and_record_attempt(
            ctx.reaction_dir,
            ctx.state,
            current_inp=step.current_inp,
            execution_index=loop.execution_index,
            started_at=started_at,
            runner=ctx.runner,
            patch_actions=step.patch_actions,
        )
    except WorkerShutdownInterrupt:
        logger.warning("Interrupted by worker shutdown during attempt %d", loop.execution_index)
        raise
    except (KeyboardInterrupt, Exception) as exc:  # noqa: BLE001
        return None, _finish_attempt_exception(ctx, loop, step.current_inp, exc)
    return (
        RecordedAttemptResult(
            current_inp=step.current_inp,
            out_path=out_path,
            analysis=analysis,
        ),
        None,
    )


def _finish_decision_or_prepare_retry(
    ctx: AttemptRunContext,
    loop: AttemptLoopState,
    result: RecordedAttemptResult,
) -> int | None:
    analysis = result.analysis
    if (
        analysis.status == AnalyzerStatus.COMPLETED
        and state_pending_scants_reverse_after_endpoint_scan(ctx.state)
    ):
        decision = None
    else:
        decision = decide_attempt_outcome(
            analyzer_status=analysis.status,
            analyzer_reason=analysis.reason,
            retries_used=loop.retries_used,
            max_retries=ctx.max_retries,
        )
    if decision is not None:
        return _finish_attempt(
            ctx,
            status=decision.run_status,
            analyzer_status=analysis.status,
            reason=decision.reason,
            last_out_path=str(result.out_path),
            exit_code=decision.exit_code,
        )

    retry_exit = prepare_retry_attempt(
        RetryAttemptRequest(
            reaction_dir=ctx.reaction_dir,
            selected_inp=ctx.selected_inp,
            state=ctx.state,
            resumed=ctx.resumed,
            current_inp=result.current_inp,
            out_path=result.out_path,
            execution_index=loop.execution_index,
            retries_used=loop.retries_used,
            max_retries=ctx.max_retries,
            analysis=analysis,
            retry_inp_path=ctx.retry_inp_path,
            emit=ctx.emit,
            notify_finished=ctx.notify_finished,
            notify_retry=ctx.notify_retry,
        )
    )
    if retry_exit is not None:
        return retry_exit
    loop.advance()
    return None


def _run_attempt_cycle(ctx: AttemptRunContext, loop: AttemptLoopState) -> int | None:
    retry_limit_exit = _finish_retry_limit_if_needed(ctx, loop)
    if retry_limit_exit is not None:
        return retry_limit_exit

    step, step_exit = _resolve_attempt_step(ctx, loop)
    if step_exit is not None:
        return step_exit
    assert step is not None

    result, run_exit = _run_attempt_step(ctx, loop, step)
    if run_exit is not None:
        return run_exit
    assert result is not None

    return _finish_decision_or_prepare_retry(ctx, loop, result)


def run_attempts(
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    *,
    resumed: bool,
    runner: RunnerLike,
    max_retries: int,
    retry_inp_path: Callable[[Path, int], Path],
    to_resolved_local: Callable[[str], Path],
    emit: Callable[[dict[str, Any]], None],
    notify_started: Callable[[RunStartedNotification], Any] | None = None,
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None,
    notify_retry: Callable[[RetryNotification], Any] | None = None,
) -> int:
    policy_max_retries = effective_max_retries(
        selected_inp,
        configured_max_retries=max_retries,
    )
    if resumed:
        stored_max_retries = max(0, int(state.get("max_retries", policy_max_retries)))
        # Retry policy controls new calculation-failure retries.  Resumed crashed
        # or interrupted runs keep their persisted retry budget so recovery can
        # continue the in-flight run rather than being reclassified as a fresh
        # no-retry Opt/Freq failure.
        policy_max_retries = max(policy_max_retries, stored_max_retries)
    if state.get("max_retries") != policy_max_retries:
        state["max_retries"] = policy_max_retries
        save_state(reaction_dir, state)
    ctx = AttemptRunContext(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        state=state,
        resumed=resumed,
        runner=runner,
        max_retries=policy_max_retries,
        retry_inp_path=retry_inp_path,
        to_resolved_local=to_resolved_local,
        emit=emit,
        notify_started=notify_started,
        notify_finished=notify_finished,
        notify_retry=notify_retry,
    )
    resumed_exit = _resume_attempts_if_terminal(ctx)
    if resumed_exit is not None:
        return resumed_exit

    loop = AttemptLoopState(
        execution_index=len(ctx.state["attempts"]) + 1,
        first_execution_index=len(ctx.state["attempts"]) + 1,
    )
    while True:
        cycle_exit = _run_attempt_cycle(ctx, loop)
        if cycle_exit is not None:
            return cycle_exit
