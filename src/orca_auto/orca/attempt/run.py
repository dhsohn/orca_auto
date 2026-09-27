"""Run the one ORCA attempt of a bound generation and publish its result.

A run makes exactly one attempt (ADR 0002). ``run_attempt`` settles a resumed
run from its recorded attempt; otherwise it marks the run started, sends the
started notification, runs ORCA once through the run's ``OrcaRunner``, records
the attempt with its analyzer verdict and publishes the terminal result. A
worker shutdown or cancel during the attempt records only the attempt's scratch
publication and propagates; the parent settles the row.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...core.engine_scratch import (
    attach_scratch_provenance_mapping_to_exception,
    scratch_provenance_from_exception,
)
from ..completion_rules import detect_completion_mode
from ..file_identity import confined_output_identity, verify_confined_output_identity
from ..notifications import dispatch_notification, notification_channel, notify_run_started_event
from ..orca_runner import OrcaRunner, WorkerShutdownInterrupt
from ..out_analyzer import OutAnalysis, analyze_output, apply_exit_code
from ..run_context import RunExecutionContext
from ..state import now_utc_iso, save_state
from ..statuses import AnalyzerStatus, RunStatus
from ..types import AttemptRecord, RunState
from .reporting import (
    build_run_started_notification,
    decide_attempt_outcome,
    exit_with_result,
    last_out_path_from_state,
)
from .resume import resume_terminal_decision

logger = logging.getLogger(__name__)


def _run_and_record_attempt(
    reaction_dir: Path,
    state: RunState,
    *,
    current_inp: Path,
    execution_index: int,
    started_at: str,
    runner: OrcaRunner,
) -> tuple[Path, OutAnalysis]:
    logger.info("Attempt %d starting: %s", execution_index, current_inp)
    run_result = runner.run(current_inp)
    scratch_provenance = run_result.scratch_provenance
    try:
        out_path = Path(run_result.out_path)

        if run_result.output_identity:
            verify_confined_output_identity(
                reaction_dir,
                run_result.output_identity,
                path_override=out_path,
            )
        output_identity_before = confined_output_identity(reaction_dir, out_path)

        mode = detect_completion_mode(current_inp)
        analysis = apply_exit_code(analyze_output(out_path, mode), run_result.return_code)
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
            "patch_actions": [],
            "started_at": started_at,
            "ended_at": now_utc_iso(),
            "command": list(run_result.command),
            "input_identity": dict(run_result.input_identity),
            "executable_identity": dict(run_result.executable_identity),
        }
        if scratch_provenance:
            attempt["scratch_provenance"] = dict(scratch_provenance)
        attempt["output_identity"] = output_identity
        if run_result.execution_provenance:
            state["execution_provenance"] = dict(run_result.execution_provenance)
        state["attempts"].append(attempt)
        save_state(reaction_dir, state)
    except BaseException as exc:
        if scratch_provenance:
            attach_scratch_provenance_mapping_to_exception(exc, scratch_provenance)
        raise

    logger.info(
        "Attempt %d finished: return_code=%d, status=%s",
        execution_index,
        run_result.return_code,
        analysis.status,
    )
    return out_path, analysis


def _record_exception_scratch_publication(
    reaction_dir: Path,
    state: RunState,
    *,
    execution_index: int,
    current_inp: Path,
    exc: BaseException,
    outcome: str,
) -> None:
    provenance = scratch_provenance_from_exception(exc)
    if not provenance:
        return
    publications = state.setdefault("scratch_publications", [])
    publications.append(
        {
            "attempt_index": execution_index,
            "inp_path": str(current_inp),
            "outcome": outcome,
            "published_at": now_utc_iso(),
            "publication": provenance,
        }
    )
    save_state(reaction_dir, state)


def _finish_runner_exception(
    context: RunExecutionContext,
    state: RunState,
    *,
    resumed: bool,
    execution_index: int,
    exc: Exception,
) -> int:
    published_out = last_out_path_from_state(state)
    durable_out = context.selected_inp.with_suffix(".out")
    if durable_out.is_file() and not durable_out.is_symlink():
        published_out = str(durable_out)
    logger.exception("ORCA runner crashed during attempt %d: %s", execution_index, exc)
    return exit_with_result(
        context.reaction_dir,
        state,
        context.selected_inp,
        status=RunStatus.FAILED,
        analyzer_status=AnalyzerStatus.INCOMPLETE,
        reason="runner_exception",
        last_out_path=published_out,
        resumed=resumed,
        exit_code=1,
        extra={"runner_error": str(exc)},
    )


def run_attempt(
    context: RunExecutionContext,
    state: RunState,
    runner: OrcaRunner,
    *,
    resumed: bool,
) -> int:
    reaction_dir = context.reaction_dir
    selected_inp = context.selected_inp
    if resumed:
        settled = resume_terminal_decision(reaction_dir, selected_inp, state)
        if settled is not None:
            return settled

    execution_index = len(state["attempts"]) + 1
    if not selected_inp.is_file():
        return exit_with_result(
            reaction_dir,
            state,
            selected_inp,
            status=RunStatus.FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="selected_input_missing",
            last_out_path=None,
            resumed=resumed,
            exit_code=1,
        )

    started_at = now_utc_iso()
    state["status"] = RunStatus.RUNNING.value
    save_state(reaction_dir, state)
    channel = notification_channel(context.cfg)
    if channel.enabled:
        started = build_run_started_notification(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            current_inp=selected_inp,
            state=state,
            execution_index=execution_index,
            status=RunStatus.RUNNING,
            attempt_started_at=started_at,
            resumed=resumed,
        )
        dispatch_notification(lambda: notify_run_started_event(channel, started), kind="started")

    try:
        out_path, analysis = _run_and_record_attempt(
            reaction_dir,
            state,
            current_inp=selected_inp,
            execution_index=execution_index,
            started_at=started_at,
            runner=runner,
        )
    except WorkerShutdownInterrupt as exc:
        _record_exception_scratch_publication(
            reaction_dir,
            state,
            execution_index=execution_index,
            current_inp=selected_inp,
            exc=exc,
            outcome="worker_shutdown",
        )
        logger.warning("Interrupted by worker shutdown during attempt %d", execution_index)
        raise
    except Exception as exc:  # noqa: BLE001
        _record_exception_scratch_publication(
            reaction_dir,
            state,
            execution_index=execution_index,
            current_inp=selected_inp,
            exc=exc,
            outcome="exception",
        )
        return _finish_runner_exception(
            context, state, resumed=resumed, execution_index=execution_index, exc=exc
        )

    decision = decide_attempt_outcome(
        analyzer_status=analysis.status,
        analyzer_reason=analysis.reason,
    )
    return exit_with_result(
        reaction_dir,
        state,
        selected_inp,
        status=decision.run_status,
        analyzer_status=analysis.status,
        reason=decision.reason,
        last_out_path=str(out_path),
        resumed=resumed,
        exit_code=decision.exit_code,
    )
