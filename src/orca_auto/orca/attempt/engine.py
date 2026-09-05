from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ...core.engine_runner import (
    confined_output_identity,
    verify_confined_output_identity,
)
from ...core.engine_scratch import (
    attach_scratch_provenance_mapping_to_exception,
    scratch_provenance_from_exception,
)
from ..completion_rules import detect_completion_mode
from ..orca_runner import WorkerShutdownInterrupt
from ..out_analyzer import OutAnalysis, analyze_output
from ..state import now_utc_iso, save_state
from ..state_machine import decide_attempt_outcome
from ..statuses import AnalyzerStatus, RunStatus
from ..types import (
    AttemptRecord,
    RunFinishedNotification,
    RunStartedNotification,
    RunState,
)
from .notifications import AttemptStartedNotification, notify_attempt_started
from .reporting import exit_with_result as _exit_with_result
from .reporting import last_out_path_from_state as _last_out_path_from_state
from .resume import prepare_resumed_checkpoint_input, resume_terminal_decision

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
    emit: Callable[[dict[str, Any]], None]
    notify_started: Callable[[RunStartedNotification], Any] | None
    notify_finished: Callable[[RunFinishedNotification], Any] | None


@dataclass
class AttemptExecution:
    execution_index: int
    first_execution_index: int


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


def _mark_attempt_started(
    reaction_dir: Path,
    state: RunState,
) -> tuple[str, RunStatus]:
    started_at = now_utc_iso()
    current_status = RunStatus.RUNNING
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
    scratch_provenance = getattr(run_result, "scratch_provenance", None)
    try:
        out_path = Path(run_result.out_path)

        runner_output_identity = getattr(run_result, "output_identity", None)
        if isinstance(runner_output_identity, dict) and runner_output_identity:
            verify_confined_output_identity(
                reaction_dir,
                runner_output_identity,
                path_override=out_path,
            )
        output_identity_before = confined_output_identity(reaction_dir, out_path)

        mode = detect_completion_mode(current_inp)
        analysis = analyze_output(out_path, mode)
        output_identity = confined_output_identity(reaction_dir, out_path)
        if output_identity != output_identity_before:
            raise RuntimeError(f"ORCA output changed while it was analyzed: {out_path}")
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
        command = getattr(run_result, "command", ())
        if isinstance(command, (list, tuple)) and all(isinstance(part, str) for part in command):
            attempt["command"] = list(command)
        input_identity = getattr(run_result, "input_identity", None)
        if isinstance(input_identity, dict):
            attempt["input_identity"] = dict(input_identity)
        executable_identity = getattr(run_result, "executable_identity", None)
        if isinstance(executable_identity, dict):
            attempt["executable_identity"] = dict(executable_identity)
        if isinstance(scratch_provenance, dict) and scratch_provenance:
            attempt["scratch_provenance"] = dict(scratch_provenance)
        attempt["output_identity"] = output_identity
        execution_provenance = getattr(run_result, "execution_provenance", None)
        if isinstance(execution_provenance, dict) and execution_provenance:
            state["execution_provenance"] = dict(execution_provenance)
        state["attempts"].append(attempt)
        save_state(reaction_dir, state)
    except BaseException as exc:
        if isinstance(scratch_provenance, dict) and scratch_provenance:
            attach_scratch_provenance_mapping_to_exception(exc, scratch_provenance)
        raise

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
        last_out_path_from_state=_last_out_path_from_state,
        exit_with_result=_exit_with_result,
        emit=ctx.emit,
        notify_finished=ctx.notify_finished,
    )


def _resolve_current_attempt_input(
    ctx: AttemptRunContext,
    loop: AttemptExecution,
) -> ResolvedAttemptInput:
    current_inp = ctx.selected_inp
    if not current_inp.is_file():
        return ResolvedAttemptInput(None, "selected_input_missing", [])

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
    loop: AttemptExecution,
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
    loop: AttemptExecution,
    current_inp: Path,
    exc: BaseException,
) -> int:
    published_out = _last_out_path_from_state(ctx.state)
    durable_out = current_inp.with_suffix(".out")
    if durable_out.is_file() and not durable_out.is_symlink():
        published_out = str(durable_out)
    if isinstance(exc, KeyboardInterrupt):
        logger.warning("Interrupted by user during attempt %d", loop.execution_index)
        return _finish_attempt(
            ctx,
            status=RunStatus.FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="interrupted_by_user",
            last_out_path=published_out,
            exit_code=130,
        )

    logger.exception("ORCA runner crashed during attempt %d: %s", loop.execution_index, exc)
    return _finish_attempt(
        ctx,
        status=RunStatus.FAILED,
        analyzer_status=AnalyzerStatus.INCOMPLETE,
        reason="runner_exception",
        last_out_path=published_out,
        exit_code=1,
        extra={"runner_error": str(exc)},
    )


def _record_exception_scratch_publication(
    ctx: AttemptRunContext,
    loop: AttemptExecution,
    current_inp: Path,
    exc: BaseException,
    *,
    outcome: str,
) -> None:
    provenance = scratch_provenance_from_exception(exc)
    if not provenance:
        return
    publications = ctx.state.setdefault("scratch_publications", [])
    publications.append(
        {
            "attempt_index": loop.execution_index,
            "inp_path": str(current_inp),
            "outcome": outcome,
            "published_at": now_utc_iso(),
            "publication": provenance,
        }
    )
    save_state(ctx.reaction_dir, ctx.state)


def _mark_and_notify_attempt_started(
    ctx: AttemptRunContext,
    loop: AttemptExecution,
    current_inp: Path,
) -> str:
    started_at, current_status = _mark_attempt_started(
        ctx.reaction_dir,
        ctx.state,
    )
    notify_attempt_started(
        AttemptStartedNotification(
            reaction_dir=ctx.reaction_dir,
            selected_inp=ctx.selected_inp,
            current_inp=current_inp,
            state=ctx.state,
            execution_index=loop.execution_index,
            first_execution_index=loop.first_execution_index,
            status=current_status,
            attempt_started_at=started_at,
            resumed=ctx.resumed,
            notify_started=ctx.notify_started,
        )
    )
    return started_at


def _resolve_attempt_step(
    ctx: AttemptRunContext,
    loop: AttemptExecution,
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
    loop: AttemptExecution,
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
    except WorkerShutdownInterrupt as exc:
        _record_exception_scratch_publication(
            ctx,
            loop,
            step.current_inp,
            exc,
            outcome="worker_shutdown",
        )
        logger.warning("Interrupted by worker shutdown during attempt %d", loop.execution_index)
        raise
    except (KeyboardInterrupt, Exception) as exc:  # noqa: BLE001
        _record_exception_scratch_publication(
            ctx,
            loop,
            step.current_inp,
            exc,
            outcome="exception",
        )
        return None, _finish_attempt_exception(ctx, loop, step.current_inp, exc)
    return (
        RecordedAttemptResult(
            current_inp=step.current_inp,
            out_path=out_path,
            analysis=analysis,
        ),
        None,
    )


def _finish_recorded_attempt(ctx: AttemptRunContext, result: RecordedAttemptResult) -> int:
    analysis = result.analysis
    decision = decide_attempt_outcome(
        analyzer_status=analysis.status,
        analyzer_reason=analysis.reason,
    )
    return _finish_attempt(
        ctx,
        status=decision.run_status,
        analyzer_status=analysis.status,
        reason=decision.reason,
        last_out_path=str(result.out_path),
        exit_code=decision.exit_code,
    )


def _run_attempt_cycle(ctx: AttemptRunContext, loop: AttemptExecution) -> int:
    step, step_exit = _resolve_attempt_step(ctx, loop)
    if step_exit is not None:
        return step_exit
    assert step is not None

    result, run_exit = _run_attempt_step(ctx, loop, step)
    if run_exit is not None:
        return run_exit
    assert result is not None

    return _finish_recorded_attempt(ctx, result)


def run_attempts(
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    *,
    resumed: bool,
    runner: RunnerLike,
    emit: Callable[[dict[str, Any]], None],
    notify_started: Callable[[RunStartedNotification], Any] | None = None,
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None,
) -> int:
    ctx = AttemptRunContext(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        state=state,
        resumed=resumed,
        runner=runner,
        emit=emit,
        notify_started=notify_started,
        notify_finished=notify_finished,
    )
    resumed_exit = _resume_attempts_if_terminal(ctx)
    if resumed_exit is not None:
        return resumed_exit

    loop = AttemptExecution(
        execution_index=len(ctx.state["attempts"]) + 1,
        first_execution_index=len(ctx.state["attempts"]) + 1,
    )
    return _run_attempt_cycle(ctx, loop)
