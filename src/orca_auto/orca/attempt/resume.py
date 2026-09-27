"""Which run state a claim continues, and settling it from a recorded attempt.

A claim resumes only the root state of its own bound input: a state left active
by a crash, or one that ``recover_crashed_state`` closed as
``crashed_recovery``. Such a state belongs to a generation that already shows
started execution, which ``recovery_rebind`` replaces with a fresh generation
unless its completed output settles the claim (ADR 0009).
``settle_from_recorded_attempt`` settles a run from its recorded attempt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..state import new_state, save_state
from ..state_reading import load_state
from ..statuses import ACTIVE_RUN_STATUS_VALUES, AnalyzerStatus, RunStatus
from ..types import RunState
from .reporting import decide_attempt_outcome, exit_with_result, last_out_path_from_state

logger = logging.getLogger(__name__)

CRASHED_RECOVERY_REASON = "crashed_recovery"


def state_matches_selected(state: RunState, selected_inp: Path) -> bool:
    selected = state.get("selected_inp")
    if not isinstance(selected, str) or not selected.strip():
        return False
    try:
        return Path(selected).expanduser().resolve() == selected_inp.resolve()
    except Exception:  # noqa: BLE001
        return False


def _final_reason(state: RunState) -> str:
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return ""
    reason = final_result.get("reason")
    if not isinstance(reason, str):
        return ""
    return reason.strip()


def recover_crashed_state(reaction_dir: Path) -> bool:
    """Close a root state a crashed run left active as failed ``crashed_recovery``.

    Called under ``run.lock``, after admission has reconciled engine ownership.
    """
    state = load_state(reaction_dir)
    if not state:
        return False

    status = str(state.get("status", "")).strip()
    if status not in ACTIVE_RUN_STATUS_VALUES:
        return False

    logger.warning(
        "Detected crashed run in %s (status=%s). Recovering state.",
        reaction_dir,
        status,
    )
    state["status"] = RunStatus.FAILED.value
    state["final_result"] = {
        "status": RunStatus.FAILED.value,
        "reason": CRASHED_RECOVERY_REASON,
        "analyzer_status": AnalyzerStatus.INCOMPLETE.value,
    }
    save_state(reaction_dir, state)
    return True


def is_resumable_state(state: RunState) -> bool:
    status = str(state.get("status", "")).strip()
    if status in ACTIVE_RUN_STATUS_VALUES:
        return True
    if status == RunStatus.FAILED.value:
        return _final_reason(state) == CRASHED_RECOVERY_REASON
    return False


def load_or_create_state(reaction_dir: Path, selected_inp: Path) -> tuple[RunState, bool]:
    state = load_state(reaction_dir)
    resumed = False
    if not state or not state_matches_selected(state, selected_inp):
        state = new_state(reaction_dir, selected_inp)
    elif is_resumable_state(state):
        resumed = True
        if state.get("final_result") is not None:
            state["final_result"] = None
    else:
        state = new_state(reaction_dir, selected_inp)

    if not isinstance(state.get("attempts"), list):
        state["attempts"] = []
    save_state(reaction_dir, state)
    return state, resumed


def _as_non_empty_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def settle_from_recorded_attempt(
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
) -> int | None:
    """Settle the run from its recorded attempt, or ``None`` when it has none."""
    attempts = state.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    last_attempt = attempts[-1]
    if not isinstance(last_attempt, dict):
        return None

    analyzer_status = (
        _as_non_empty_text(last_attempt.get("analyzer_status")) or AnalyzerStatus.INCOMPLETE.value
    )
    analyzer_reason = (
        _as_non_empty_text(last_attempt.get("analyzer_reason")) or "resume_last_attempt"
    )
    decision = decide_attempt_outcome(
        analyzer_status=analyzer_status,
        analyzer_reason=analyzer_reason,
    )

    logger.info(
        "Resume detected terminal previous attempt: analyzer_status=%s, reason=%s",
        analyzer_status,
        decision.reason,
    )
    last_out_path = _as_non_empty_text(last_attempt.get("out_path")) or last_out_path_from_state(
        state
    )
    return exit_with_result(
        reaction_dir,
        state,
        selected_inp,
        status=decision.run_status,
        analyzer_status=analyzer_status,
        reason=decision.reason,
        last_out_path=last_out_path,
        resumed=True,
        exit_code=decision.exit_code,
    )
