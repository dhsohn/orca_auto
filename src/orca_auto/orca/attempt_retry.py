from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

from .attempt_reporting import build_retry_notification, exit_with_result
from .inp_rewriter import (
    prepare_checkpoint_restart_input,
    prepare_scants_scan_retry_input,
    rewrite_for_retry,
)
from .out_analyzer import OutAnalysis
from .retry_policy import RetryRecipeName, retry_recipe_name_for_input
from .scants import (
    highest_scants_surface_point,
    input_uses_scants,
    prepare_scants_endpoint_scan_input,
    prepare_scants_reverse_scan_retry_input,
)
from .state import save_state
from .statuses import RunStatus
from .types import RetryNotification, RunFinishedNotification, RunState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryAttemptRequest:
    reaction_dir: Path
    selected_inp: Path
    state: RunState
    resumed: bool
    current_inp: Path
    out_path: Path
    execution_index: int
    retries_used: int
    max_retries: int
    analysis: OutAnalysis
    retry_inp_path: Callable[[Path, int], Path]
    emit: Callable[[Dict[str, Any]], None]
    notify_finished: Callable[[RunFinishedNotification], Any] | None
    notify_retry: Callable[[RetryNotification], Any] | None


def retry_recipe_step(retry_number: int) -> RetryRecipeName:
    """Legacy helper retained for retry-input recovery call sites.

    Normal retry preparation now selects recipes from the calculation-type policy.
    Recovery without an input path falls back to a no-route-rewrite copy instead
    of the old global TightSCF/geometry-hardening ladder.
    """
    del retry_number
    return "no_route_rewrite"


def resume_checkpoint_inp_path(current_inp: Path) -> Path:
    return current_inp.with_name(f"{current_inp.stem}.resume.inp")


def prepare_resumed_checkpoint_input(
    *,
    resumed: bool,
    current_inp: Path,
    reaction_dir: Path,
) -> tuple[Path | None, list[str]]:
    if not resumed:
        return None, []
    target_inp = resume_checkpoint_inp_path(current_inp)
    prepared, actions = prepare_checkpoint_restart_input(
        current_inp,
        target_inp,
        reaction_dir,
    )
    if prepared is None:
        return None, []
    return prepared, [f"resume_{action}" for action in actions]


def _attempt_patch_actions(attempt: Any) -> list[str]:
    if not isinstance(attempt, dict):
        return []
    actions = attempt.get("patch_actions")
    if not isinstance(actions, list):
        return []
    return [str(action) for action in actions]


def _state_has_scants_reverse_scan(state: RunState) -> bool:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        action.startswith("scants_reverse_scan")
        for attempt in attempts
        for action in _attempt_patch_actions(attempt)
    )


def state_pending_scants_reverse_after_endpoint_scan(state: RunState) -> bool:
    """True from endpoint-scan preparation until a reverse scan is prepared.

    While pending, a COMPLETED attempt is only the intermediate relaxed endpoint
    scan, not the requested ScanTS result, so the run must not finish
    successfully — including on crash-resume, where recovery attempts may sit
    between the endpoint-scan preparation and the completed attempt.
    """
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return False
    pending = False
    for attempt in attempts:
        actions = _attempt_patch_actions(attempt)
        if any(action.startswith("scants_reverse_scan") for action in actions):
            pending = False
        elif any(action.startswith("scants_endpoint_scan") for action in actions):
            pending = True
    return pending


def _output_has_scants_actual_surface_maximum(ctx: RetryAttemptRequest) -> bool:
    return (
        input_uses_scants(ctx.current_inp)
        and highest_scants_surface_point(ctx.out_path) is not None
    )


def _prepare_scants_reverse_scan_after_maximum(
    ctx: RetryAttemptRequest,
    *,
    next_inp: Path,
) -> tuple[Path | None, list[str]]:
    if not _output_has_scants_actual_surface_maximum(ctx):
        return None, []
    if _state_has_scants_reverse_scan(ctx.state):
        return None, []
    prepared, actions = prepare_scants_reverse_scan_retry_input(
        source_inp=ctx.current_inp,
        selected_inp=ctx.selected_inp,
        target_inp=next_inp,
        max_memory_gb=ctx.state.get("max_memory_gb_per_task"),
    )
    if prepared is not None:
        return prepared, actions
    return prepare_scants_endpoint_scan_input(
        source_inp=ctx.current_inp,
        target_inp=next_inp,
        max_memory_gb=ctx.state.get("max_memory_gb_per_task"),
    )


def _prepare_scants_reverse_scan_after_endpoint_scan(
    ctx: RetryAttemptRequest,
    *,
    next_inp: Path,
) -> tuple[Path | None, list[str]]:
    if not state_pending_scants_reverse_after_endpoint_scan(ctx.state):
        return None, []
    if _state_has_scants_reverse_scan(ctx.state):
        return None, []
    return prepare_scants_reverse_scan_retry_input(
        source_inp=ctx.current_inp,
        selected_inp=ctx.selected_inp,
        target_inp=next_inp,
        max_memory_gb=ctx.state.get("max_memory_gb_per_task"),
    )


def prepare_retry_attempt(ctx: RetryAttemptRequest) -> int | None:
    next_retry_number = ctx.retries_used + 1
    next_inp = ctx.retry_inp_path(ctx.selected_inp, next_retry_number)
    patch_step = retry_recipe_name_for_input(ctx.selected_inp, next_retry_number)
    try:
        uses_scants = input_uses_scants(ctx.current_inp) or input_uses_scants(ctx.selected_inp)
        scants_endpoint_scan_seen = state_pending_scants_reverse_after_endpoint_scan(ctx.state)
        scants_surface_maximum_seen = _output_has_scants_actual_surface_maximum(ctx)
        prepared_scants, patch_actions = _prepare_scants_reverse_scan_after_endpoint_scan(
            ctx,
            next_inp=next_inp,
        )
        if prepared_scants is None:
            prepared_scants, patch_actions = _prepare_scants_reverse_scan_after_maximum(
                ctx,
                next_inp=next_inp,
            )
        if prepared_scants is None and (scants_surface_maximum_seen or scants_endpoint_scan_seen):
            raise RuntimeError("no_scants_retry_input")
        if prepared_scants is None:
            scants_retry_source = ctx.selected_inp if next_retry_number == 1 else ctx.current_inp
            prepared_scants, patch_actions = prepare_scants_scan_retry_input(
                source_inp=scants_retry_source,
                target_inp=next_inp,
                retry_number=next_retry_number,
                max_memory_gb=ctx.state.get("max_memory_gb_per_task"),
            )
        if prepared_scants is None:
            if uses_scants:
                raise RuntimeError("no_scants_retry_input")
            patch_actions = rewrite_for_retry(
                source_inp=ctx.current_inp,
                target_inp=next_inp,
                reaction_dir=ctx.reaction_dir,
                step=patch_step,
                max_memory_gb=ctx.state.get("max_memory_gb_per_task"),
            )
    except Exception as exc:  # noqa: BLE001
        ctx.state["attempts"][-1]["patch_actions"] = [f"rewrite_failed:{exc}"]
        return exit_with_result(
            ctx.reaction_dir,
            ctx.state,
            ctx.selected_inp,
            status=RunStatus.FAILED,
            analyzer_status=ctx.analysis.status,
            reason="rewrite_failed",
            last_out_path=str(ctx.out_path),
            resumed=ctx.resumed,
            exit_code=1,
            emit=ctx.emit,
            notify_finished=ctx.notify_finished,
        )

    ctx.state["attempts"][-1]["patch_actions"] = patch_actions
    save_state(ctx.reaction_dir, ctx.state)
    if ctx.notify_retry is None:
        return None

    retry_notification = build_retry_notification(
        reaction_dir=ctx.reaction_dir,
        selected_inp=ctx.selected_inp,
        current_inp=ctx.current_inp,
        out_path=ctx.out_path,
        next_inp=next_inp,
        execution_index=ctx.execution_index,
        next_retry_number=next_retry_number,
        max_retries=ctx.max_retries,
        analysis_status=ctx.analysis.status,
        analysis_reason=ctx.analysis.reason,
        patch_actions=patch_actions,
        resumed=ctx.resumed,
    )
    try:
        ctx.notify_retry(retry_notification)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Retry notification callback failed for attempt %d",
            ctx.execution_index,
            exc_info=True,
        )
    return None


__all__ = [
    "RetryAttemptRequest",
    "prepare_resumed_checkpoint_input",
    "prepare_retry_attempt",
    "resume_checkpoint_inp_path",
    "retry_recipe_step",
    "state_pending_scants_reverse_after_endpoint_scan",
]
