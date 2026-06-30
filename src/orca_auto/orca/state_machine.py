from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .state import load_state, new_state, save_state
from .statuses import AnalyzerStatus, RunStatus
from .types import RunState

RESUMABLE_RUN_STATUSES = {RunStatus.RUNNING.value, RunStatus.RETRYING.value}
RESUMABLE_FAILED_REASONS = {"interrupted_by_user", "worker_shutdown", "crashed_recovery"}
_RETRY_STEM_RE = re.compile(r"\.retry\d+$", re.IGNORECASE)


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
    retries_used: int,
    max_retries: int,
) -> AttemptDecision | None:
    parsed = parse_analyzer_status(analyzer_status)
    if parsed == AnalyzerStatus.COMPLETED:
        return AttemptDecision(run_status=RunStatus.COMPLETED, reason=analyzer_reason, exit_code=0)
    if parsed == AnalyzerStatus.ERROR_MULTIPLICITY_IMPOSSIBLE:
        return AttemptDecision(run_status=RunStatus.FAILED, reason=analyzer_reason, exit_code=1)
    if retries_used >= max_retries:
        return AttemptDecision(
            run_status=RunStatus.FAILED, reason="retry_limit_reached", exit_code=1
        )
    return None


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


def _retry_inp_path(selected_inp: Path, retry_number: int) -> Path:
    base_stem = _RETRY_STEM_RE.sub("", selected_inp.stem)
    if not base_stem:
        base_stem = selected_inp.stem
    return selected_inp.with_name(f"{base_stem}.retry{retry_number:02d}.inp")


def _prepared_next_retry_input_exists(selected_inp: Path, attempt_count: int) -> bool:
    if attempt_count < 1:
        return False
    return _retry_inp_path(selected_inp, attempt_count).exists()


def load_or_create_state(
    reaction_dir: Path,
    selected_inp: Path,
    *,
    max_retries: int,
    to_resolved_local: Callable[[str], Path],
) -> tuple[RunState, bool]:
    state = load_state(reaction_dir)
    resumed = False
    if not state or not state_matches_selected(
        state, selected_inp, to_resolved_local=to_resolved_local
    ):
        state = new_state(reaction_dir, selected_inp, max_retries=max_retries)
    elif is_resumable_state(state):
        resumed = True
        if state.get("final_result") is not None:
            state["final_result"] = None
    else:
        state = new_state(reaction_dir, selected_inp, max_retries=max_retries)

    if resumed:
        attempts = state.get("attempts")
        attempt_count = len(attempts) if isinstance(attempts, list) else 0
        if attempt_count <= 1 or _prepared_next_retry_input_exists(selected_inp, attempt_count):
            state["max_retries"] = max(0, int(state.get("max_retries", max_retries)), max_retries)
        else:
            state["max_retries"] = max_retries
    else:
        state["max_retries"] = max_retries
    if not isinstance(state.get("attempts"), list):
        state["attempts"] = []
    save_state(reaction_dir, state)
    return state, resumed
