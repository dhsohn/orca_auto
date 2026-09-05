from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..inp_rewriter import prepare_checkpoint_restart_input, resume_checkpoint_input_path
from ..state_machine import decide_attempt_outcome
from ..statuses import AnalyzerStatus
from ..types import RunFinishedNotification, RunState

logger = logging.getLogger(__name__)


def prepare_resumed_checkpoint_input(
    *,
    resumed: bool,
    current_inp: Path,
    reaction_dir: Path,
) -> tuple[Path | None, list[str]]:
    if not resumed:
        return None, []
    target_inp = resume_checkpoint_input_path(current_inp)
    prepared, actions = prepare_checkpoint_restart_input(
        current_inp,
        target_inp,
        reaction_dir,
    )
    if prepared is None:
        return None, []
    return prepared, [f"resume_{action}" for action in actions]


def _as_non_empty_text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def resume_terminal_decision(
    *,
    reaction_dir: Path,
    selected_inp: Path,
    state: RunState,
    resumed: bool,
    last_out_path_from_state: Callable[[RunState], str | None],
    exit_with_result: Callable[..., int],
    emit: Callable[[dict[str, Any]], None],
    notify_finished: Callable[[RunFinishedNotification], Any] | None = None,
) -> int | None:
    if not resumed:
        return None

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
        resumed=resumed,
        exit_code=decision.exit_code,
        emit=emit,
        notify_finished=notify_finished,
    )
