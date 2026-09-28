"""Decide whether an existing ORCA ``.out`` counts as the completed run.

``existing_completed_out`` is the adoption rule: the newest ``<stem>.out``
beside the selected input that is not older than its inputs and that the
analyzer verifies as ``COMPLETED``. ``existing_completed_exit`` turns that
verdict into the run's exit code without launching ORCA, but only when no
recorded attempt verdict for the same generation outranks it; a settled or
resumable record is honoured over a completed-looking output. The rules are
shared by the worker child's pre-launch probe (``worker_execution``), the
crash-recovery rebind (``recovery_rebind``) and the locked run in
``execution``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .attempt.reporting import exit_with_result, parse_analyzer_status
from .attempt.resume import (
    is_resumable_state,
    load_or_create_state,
    settle_from_recorded_attempt,
    state_matches_selected,
)
from .completion_rules import detect_completion_mode
from .out_analyzer import analyze_output
from .run_context import RunExecutionContext, bind_queue_identity
from .state import save_state
from .state_reading import load_state
from .statuses import AnalyzerStatus, RunStatus
from .types import RunState

logger = logging.getLogger(__name__)


def existing_completed_out(selected_inp: Path) -> dict[str, Any] | None:
    base_stem = selected_inp.stem

    out_candidates = list(selected_inp.parent.glob(f"{base_stem}.out"))
    out_candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.name.lower()), reverse=True)

    seen: set[Path] = set()
    for out_path in out_candidates:
        resolved = out_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)

        mode_inp = out_path.with_suffix(".inp")
        if not mode_inp.exists():
            mode_inp = selected_inp
        if _out_is_older_than_inputs(out_path, selected_inp=selected_inp, mode_inp=mode_inp):
            continue
        mode = detect_completion_mode(mode_inp)
        analysis = analyze_output(out_path, mode)
        if analysis.status != AnalyzerStatus.COMPLETED:
            continue
        return {
            "out_path": str(out_path),
            "analysis": analysis,
        }
    return None


def _out_is_older_than_inputs(out_path: Path, *, selected_inp: Path, mode_inp: Path) -> bool:
    newest_input_mtime_ns = max(selected_inp.stat().st_mtime_ns, mode_inp.stat().st_mtime_ns)
    return out_path.stat().st_mtime_ns < newest_input_mtime_ns


def completed_out_or_none(bound_selected: Path) -> dict[str, Any] | None:
    """``existing_completed_out`` for a bound input that may be torn or racing.

    The output in a crashed generation is exactly the file most likely to be
    truncated or actively written; a probe failure must degrade to the
    recovery path, never abort the claim.
    """
    try:
        return existing_completed_out(bound_selected)
    except Exception:  # noqa: BLE001
        logger.debug(
            "completed-output probe failed for %s; continuing with recovery",
            bound_selected,
            exc_info=True,
        )
        return None


def _state_with_recorded_attempt(reaction_dir: Path, selected_inp: Path) -> RunState | None:
    # Read-only: load_or_create_state clears the resumable final result as it
    # loads, so a second load would no longer recognize the state as resumable.
    state = load_state(reaction_dir)
    if not state or not state_matches_selected(state, selected_inp):
        return None
    attempts = state.get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return None
    return state


def _recorded_attempt_failed(state: RunState) -> bool:
    recorded = parse_analyzer_status(
        str(state["attempts"][-1].get("analyzer_status") or "").strip()
    )
    return recorded != AnalyzerStatus.COMPLETED


def existing_completed_exit(context: RunExecutionContext) -> int | None:
    """Settle the run from an already completed output, or ``None`` to run ORCA."""
    reaction_dir = context.reaction_dir
    selected_inp = context.selected_inp
    done = existing_completed_out(selected_inp)
    if done is None:
        return None
    recorded = _state_with_recorded_attempt(reaction_dir, selected_inp)
    if recorded is not None:
        # The run already recorded its analyzer verdict for this generation, and
        # that verdict was reconciled with the process exit code when it was
        # saved. The completion marker alone must not publish success over it.
        if is_resumable_state(recorded):
            # The ordinary resume path settles from the record.
            return None
        if recorded.get("status") == RunStatus.FAILED.value and _recorded_attempt_failed(recorded):
            # Loading this state would replace it and erase the attempt record.
            if isinstance(recorded.get("final_result"), dict):
                # Already settled: keep its reason, diagnostics and
                # notification marker exactly as published.
                logger.warning(
                    "Keeping the settled failed result in %s over a completed-looking output",
                    reaction_dir,
                )
                return 1
            return settle_from_recorded_attempt(reaction_dir, selected_inp, recorded)

    state, resumed = load_or_create_state(reaction_dir, selected_inp)
    if bind_queue_identity(state, context):
        save_state(reaction_dir, state)
    return exit_with_result(
        reaction_dir,
        state,
        selected_inp,
        status=RunStatus.COMPLETED,
        analyzer_status=AnalyzerStatus.COMPLETED,
        reason="existing_out_completed",
        last_out_path=done["out_path"],
        resumed=True if resumed else None,
        exit_code=0,
        extra={"skipped_execution": True},
    )


__all__ = [
    "completed_out_or_none",
    "existing_completed_exit",
    "existing_completed_out",
]
