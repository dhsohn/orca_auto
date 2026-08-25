from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..inp_rewriter import (
    prepare_checkpoint_restart_input,
    prepare_scants_scan_retry_input,
)
from ..out_analyzer import OutAnalysis
from ..retry_policy import resume_checkpoint_input_path
from ..scants import (
    highest_scants_surface_point,
    input_uses_scants,
    prepare_scants_optts_fallback_input,
)
from ..state import save_state
from ..statuses import RunStatus
from ..types import RetryNotification, RunFinishedNotification, RunState
from .reporting import build_retry_notification, exit_with_result

logger = logging.getLogger(__name__)


class ScantsRetryStop(RuntimeError):
    """Deliberate end of the ScanTS retry chain, carrying a report-facing reason.

    Separates "the recipe chain has nothing left to try" and "the scan profile
    proves another scan is pointless" from genuine rewrite crashes, so the
    final report carries an actionable reason instead of the generic
    ``rewrite_failed``.
    """


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
    emit: Callable[[dict[str, Any]], None]
    notify_finished: Callable[[RunFinishedNotification], Any] | None
    notify_retry: Callable[[RetryNotification], Any] | None

    def max_memory_gb_per_task(self) -> int | None:
        return self.state.get("max_memory_gb_per_task")


def resume_checkpoint_inp_path(current_inp: Path) -> Path:
    return resume_checkpoint_input_path(current_inp)


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


def _extend_current_patch_actions(state: RunState, patch_actions: list[str]) -> list[str]:
    attempts = state.get("attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return list(patch_actions)
    current_attempt = attempts[-1]
    existing = current_attempt.get("patch_actions")
    if not isinstance(existing, list):
        current_attempt["patch_actions"] = []
        existing = current_attempt["patch_actions"]
    existing.extend(patch_actions)
    return [str(action) for action in existing]


def _state_has_scants_optts_fallback(state: RunState) -> bool:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        action.startswith("scants_fallback_to_optts")
        for attempt in attempts
        for action in _attempt_patch_actions(attempt)
    )


def _prepare_scants_optts_fallback(
    ctx: RetryAttemptRequest,
    *,
    next_inp: Path,
) -> tuple[Path | None, list[str]]:
    """Bypass ORCA's TS-guess refinement after it corrupted the geometry.

    ORCA 6.x's ScanTS refinement can construct a geometry with the scanned
    pair at zero distance and abort at the next SCF startup. A surface table
    in the output means the scan itself finished and bracketed a maximum whose
    numbered ``*.NNN.xyz`` is intact on disk, so retry as plain OptTS from
    that maximum, skipping the refinement entirely. One shot per run; when the
    OptTS attempt fails too, the run ends with ``scants_recipes_exhausted`` —
    deeper exploration belongs to the scan_ts_search workflow.
    """
    if not ctx.analysis.markers["geometry_zero_distance"]:
        return None, []
    if not input_uses_scants(ctx.current_inp):
        return None, []
    if _state_has_scants_optts_fallback(ctx.state):
        return None, []
    return prepare_scants_optts_fallback_input(
        source_inp=ctx.current_inp,
        target_inp=next_inp,
        out_path=ctx.out_path,
        max_memory_gb=ctx.max_memory_gb_per_task(),
    )


def _output_has_scants_actual_surface_maximum(ctx: RetryAttemptRequest) -> bool:
    return (
        input_uses_scants(ctx.current_inp)
        and highest_scants_surface_point(ctx.out_path) is not None
    )


def prepare_retry_attempt(ctx: RetryAttemptRequest) -> int | None:
    next_retry_number = ctx.retries_used + 1
    next_inp = ctx.retry_inp_path(ctx.selected_inp, next_retry_number)
    try:
        uses_scants = input_uses_scants(ctx.current_inp) or input_uses_scants(ctx.selected_inp)
        prepared_scants, patch_actions = _prepare_scants_optts_fallback(ctx, next_inp=next_inp)
        if prepared_scants is None and _output_has_scants_actual_surface_maximum(ctx):
            # A surface table in the output means the scan itself finished;
            # whatever followed (TS criteria failure, a refinement crash after
            # the one-shot fallback) is not a mid-scan calculation failure, so
            # the failed-scan continuation below must not fire. Endpoint
            # extension and reverse-scan strategies live in the scan_ts_search
            # workflow now.
            raise ScantsRetryStop("scants_recipes_exhausted")
        if prepared_scants is None:
            # Continue the scan from the numbered ``*.NNN.xyz`` written by the input
            # that actually ran (``current_inp``), not ``selected_inp``. For a fresh
            # first retry these are the same file; for a *resumed* first retry
            # ``current_inp`` is ``<name>.resume.inp`` whose stem owns the on-disk
            # scan points, while ``selected_inp`` (``<name>.inp``) owns only the
            # stale pre-crash points -- sourcing from it would continue from a stale
            # point or dead-end the run despite valid resume artifacts.
            prepared_scants, patch_actions = prepare_scants_scan_retry_input(
                source_inp=ctx.current_inp,
                target_inp=next_inp,
                retry_number=next_retry_number,
                max_memory_gb=ctx.max_memory_gb_per_task(),
            )
        if prepared_scants is None:
            if uses_scants:
                raise ScantsRetryStop("scants_recipes_exhausted")
            # Non-ScanTS inputs carry no automatic rewrite recipe. This branch
            # is reachable only for resumed runs whose persisted state grants a
            # retry budget the current policy would not; fail the run instead
            # of rerunning the identical input.
            raise RuntimeError("no_retry_rewrite_available")
    except ScantsRetryStop as stop:
        reason = str(stop)
        _extend_current_patch_actions(ctx.state, [f"scants_retry_stopped:{reason}"])
        return exit_with_result(
            ctx.reaction_dir,
            ctx.state,
            ctx.selected_inp,
            status=RunStatus.FAILED,
            analyzer_status=ctx.analysis.status,
            reason=reason,
            last_out_path=str(ctx.out_path),
            resumed=ctx.resumed,
            exit_code=1,
            emit=ctx.emit,
            notify_finished=ctx.notify_finished,
        )
    except Exception as exc:  # noqa: BLE001
        _extend_current_patch_actions(ctx.state, [f"rewrite_failed:{exc}"])
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

    _extend_current_patch_actions(ctx.state, patch_actions)
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
    "ScantsRetryStop",
    "prepare_resumed_checkpoint_input",
    "prepare_retry_attempt",
    "resume_checkpoint_inp_path",
]
