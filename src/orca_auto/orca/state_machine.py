from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .state import new_state, save_state
from .state_reading import load_state
from .statuses import AnalyzerStatus, RunStatus
from .types import RunState

RESUMABLE_RUN_STATUSES = frozenset({RunStatus.RUNNING.value, RunStatus.RETRYING.value})
RESUMABLE_FAILED_REASONS = frozenset({"interrupted_by_user", "worker_shutdown", "crashed_recovery"})


@dataclass(frozen=True)
class AttemptDecision:
    run_status: RunStatus
    reason: str
    exit_code: int


def parse_analyzer_status(status_text: AnalyzerStatus | str) -> AnalyzerStatus | None:
    if isinstance(status_text, AnalyzerStatus):
        return status_text
    try:
        return AnalyzerStatus(str(status_text))
    except ValueError:
        return None


def decide_attempt_outcome(
    *,
    analyzer_status: AnalyzerStatus | str,
    analyzer_reason: str,
) -> AttemptDecision:
    parsed = parse_analyzer_status(analyzer_status)
    if parsed == AnalyzerStatus.COMPLETED:
        return AttemptDecision(run_status=RunStatus.COMPLETED, reason=analyzer_reason, exit_code=0)
    return AttemptDecision(run_status=RunStatus.FAILED, reason=analyzer_reason, exit_code=1)


def state_matches_selected(
    state: RunState,
    selected_inp: Path,
    *,
    to_resolved_local: Callable[[str], Path],
) -> bool:
    selected = state.get("selected_inp")
    if not isinstance(selected, str) or not selected.strip():
        return False
    try:
        return to_resolved_local(selected) == selected_inp.resolve()
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


def is_resumable_state(state: RunState) -> bool:
    status = str(state.get("status", "")).strip()
    if status in RESUMABLE_RUN_STATUSES:
        return True
    if status == RunStatus.FAILED.value:
        return _final_reason(state) in RESUMABLE_FAILED_REASONS
    return False


def load_or_create_state(
    reaction_dir: Path,
    selected_inp: Path,
    *,
    to_resolved_local: Callable[[str], Path],
) -> tuple[RunState, bool]:
    state = load_state(reaction_dir)
    resumed = False
    if not state or not state_matches_selected(
        state, selected_inp, to_resolved_local=to_resolved_local
    ):
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
