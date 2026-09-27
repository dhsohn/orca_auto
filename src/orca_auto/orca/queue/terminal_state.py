"""Terminal ``job_state.json`` synthesis for runs that never wrote their own outcome.

These writers run under ``run.lock`` and fail closed whenever the state on disk
belongs to a different generation than the queue row being replayed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..attempt.reporting import build_final_result, last_out_path_from_state
from ..report.publication import write_report_files
from ..run_lock import acquire_run_lock
from ..state import finalize_state, new_state
from ..state_reading import load_state, state_path
from ..statuses import TERMINAL_RUN_STATUS_VALUES, AnalyzerStatus, RunStatus
from ..types import RunState
from .terminal_marker import (
    StateGenerationFingerprint,
    TerminalGenerationVerdict,
    state_generation_fingerprint,
    terminal_generation_verdict,
    terminal_status_from_run_state,
)

logger = logging.getLogger(__name__)


def _load_state_for_terminal_generation(
    job_dir: Path,
    *,
    expected_job_id: str,
    observed_state: StateGenerationFingerprint | None = None,
) -> RunState | None:
    """The state to finalize, ``None`` to synthesize a fresh one, or a fail-closed error."""
    state_file = state_path(job_dir)
    state_existed = state_file.exists()
    state = load_state(job_dir)
    current = state_generation_fingerprint(state, present=state_existed or state_file.exists())
    observed_run_id = (
        observed_state.run_id
        if observed_state is not None
        and observed_state.readable
        and observed_state.job_id == expected_job_id
        else ""
    )
    verdict = terminal_generation_verdict(
        current,
        task_id=expected_job_id,
        observed=observed_state,
        expected_run_id=observed_run_id,
    )
    target = f"job_dir={job_dir} expected_job_id={expected_job_id}"
    state_job_id = current.job_id or "<missing>"
    if verdict is TerminalGenerationVerdict.OWNED:
        return state
    if verdict is TerminalGenerationVerdict.ABSENT:
        return None
    if verdict in (
        TerminalGenerationVerdict.PREVIOUS_TERMINAL,
        TerminalGenerationVerdict.UNOBSERVED_PREVIOUS_TERMINAL,
    ):
        # Synthesize a fresh state for the expected queue task instead of
        # relabeling the previous generation's result.
        logger.info(
            "Ignoring previous-generation terminal ORCA state: "
            "job_dir=%s expected_job_id=%s state_job_id=%s",
            job_dir,
            expected_job_id,
            current.job_id,
        )
        return None
    if verdict is TerminalGenerationVerdict.UNREADABLE:
        raise RuntimeError(f"ORCA run state is unreadable: {state_file}")
    if verdict in (
        TerminalGenerationVerdict.DISAPPEARED,
        TerminalGenerationVerdict.DISAPPEARED_UNVERIFIED,
    ):
        raise RuntimeError(f"ORCA terminal replay state disappeared after its queue mark: {target}")
    if verdict is TerminalGenerationVerdict.RESTARTED:
        assert observed_state is not None
        raise RuntimeError(
            "ORCA terminal replay observed a new run for the expected task "
            f"after marking a different state generation: {target} "
            f"observed_job_id={observed_state.job_id}"
        )
    if verdict is TerminalGenerationVerdict.NEWER_RUN:
        raise RuntimeError(f"ORCA terminal replay observed a newer run for the same task: {target}")
    if verdict in (
        TerminalGenerationVerdict.REPLACED,
        TerminalGenerationVerdict.REPLACED_UNVERIFIED,
    ):
        raise RuntimeError(
            "ORCA terminal replay was superseded by another state generation: "
            f"{target} state_job_id={state_job_id}"
        )
    if verdict is TerminalGenerationVerdict.OTHER_ACTIVE:
        raise RuntimeError(
            "ORCA terminal replay observed another active state generation: "
            f"{target} state_job_id={current.job_id}"
        )
    # UNIDENTIFIED_ACTIVE or UNOBSERVED_OTHER_ACTIVE: possibly the live generation.
    raise RuntimeError(
        f"ORCA run state belongs to a different active generation: {target} "
        f"state_job_id={state_job_id}"
    )


def _record_terminal_run_state(
    job_dir: Path,
    *,
    status: RunStatus,
    reason: str,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    observed_state: StateGenerationFingerprint | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Write a terminal run state for a run that never recorded its own outcome.

    A run stopped by a signal or an exited child never writes its terminal
    result, so the run state lingers as ``running``. That leaves a stale run
    snapshot in the activity list and starves the terminal notification
    (which requires ``final_result``). Persist the outcome here instead.

    Returns ``(run_id, terminal_status)``: the run_id when known (so the queue
    entry can be matched to this snapshot) and the terminal status now recorded
    in the run state -- ``status`` when we wrote it, or a pre-existing terminal
    status we refused to clobber. When no state exists yet, a minimal state is
    created from the queue identity so indexing cannot fall back to ``unknown``.
    """
    expected_job_id = str(fallback_job_id or "").strip()
    with acquire_run_lock(job_dir):
        state = _load_state_for_terminal_generation(
            job_dir,
            expected_job_id=expected_job_id,
            observed_state=observed_state,
        )
        if state is None:
            selected_text = str(selected_inp or "").strip()
            selected_path = Path(selected_text).expanduser() if selected_text else job_dir / "-"
            if not selected_path.is_absolute():
                selected_path = job_dir / selected_path
            state = new_state(job_dir, selected_path)
            if expected_job_id:
                state["job_id"] = expected_job_id
        if execution_provenance and not (
            state.get("execution_provenance") and terminal_status_from_run_state(state) is not None
        ):
            # Published terminal evidence belongs to the execution that wrote it.
            # Replaying with a newer projection must not backfill old reports or
            # replace their captured source identities.
            state["execution_provenance"] = dict(execution_provenance)
        run_id = str(state.get("run_id") or "").strip() or None
        final_result = state.get("final_result")
        if isinstance(final_result, dict):
            existing_status = str(final_result.get("status") or "").strip()
            if existing_status in TERMINAL_RUN_STATUS_VALUES:
                # A real terminal outcome was already recorded (e.g. the run finished
                # just before cancellation landed); do not clobber it, and report the
                # real status so the queue entry is reconciled to what actually
                # happened instead of being mislabeled with the requested status.
                finalize_state(
                    job_dir,
                    state,
                    status=existing_status,
                    final_result=final_result,
                )
                write_report_files(job_dir, state)
                return run_id, existing_status
        terminal_result = build_final_result(
            status=status,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason=reason,
            last_out_path=last_out_path_from_state(state),
        )
        finalize_state(job_dir, state, status=status, final_result=terminal_result)
        write_report_files(job_dir, state)
        return run_id, status.value


def record_cancelled_run_state(
    job_dir: Path,
    *,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    observed_state: StateGenerationFingerprint | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Record the terminal state a signal-interrupted run never wrote."""

    return _record_terminal_run_state(
        job_dir,
        status=RunStatus.CANCELLED,
        reason="cancel_requested",
        fallback_job_id=fallback_job_id,
        selected_inp=selected_inp,
        observed_state=observed_state,
        execution_provenance=execution_provenance,
    )


def record_failed_run_state(
    job_dir: Path,
    *,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    reason: str,
    observed_state: StateGenerationFingerprint | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Ensure an exited child has a terminal state for its queue generation."""

    return _record_terminal_run_state(
        job_dir,
        status=RunStatus.FAILED,
        reason=reason,
        fallback_job_id=fallback_job_id,
        selected_inp=selected_inp,
        observed_state=observed_state,
        execution_provenance=execution_provenance,
    )


__all__ = ["record_cancelled_run_state", "record_failed_run_state"]
