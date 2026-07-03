from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..inp_rewriter import (
    prepare_checkpoint_restart_input,
    prepare_scants_scan_retry_input,
    rewrite_for_retry,
)
from ..out_analyzer import OutAnalysis
from ..retry_policy import RetryRecipeName, retry_recipe_name_for_input
from ..scants import (
    SCANTS_BARRIER_NOISE_KCAL,
    highest_scants_surface_point,
    input_uses_scants,
    parse_scants_actual_surface,
    prepare_scants_endpoint_scan_input,
    prepare_scants_optts_fallback_input,
    prepare_scants_reverse_scan_retry_input,
    scan_profile_interior_barrier_kcal,
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


def _state_has_scants_optts_fallback(state: RunState) -> bool:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return False
    return any(
        action.startswith("scants_fallback_to_optts")
        for attempt in attempts
        for action in _attempt_patch_actions(attempt)
    )


def _scants_chain_request(ctx: RetryAttemptRequest) -> RetryAttemptRequest:
    """Redirect the scan-recipe chain at the crashed ScanTS attempt.

    When the current attempt is a failed OptTS fallback, its input is no
    longer a ScanTS input and its output carries no scan surface, so the
    endpoint/reverse recipes would fail closed. Pointing the chain back at the
    ScanTS attempt the fallback was prepared from (its scan artifacts are
    still on disk) keeps those recipes available after the fallback.

    Resume recovery recreates a missing retry input as a plain copy of the
    previous attempt, so after a worker restart the current attempt may be a
    copy-of-a-copy of the fallback; the walk follows
    ``resume_recreated_missing_input`` markers back to the attempt the copies
    originated from before looking for the fallback marker.
    """
    if input_uses_scants(ctx.current_inp):
        return ctx
    attempts = ctx.state.get("attempts")
    if not isinstance(attempts, list):
        return ctx
    cursor = len(attempts) - 1
    while cursor >= 1:
        creator = attempts[cursor - 1]
        if not isinstance(creator, dict):
            return ctx
        creating_actions = _attempt_patch_actions(creator)
        if any(action.startswith("scants_fallback_to_optts") for action in creating_actions):
            inp_raw = str(creator.get("inp_path") or "").strip()
            out_raw = str(creator.get("out_path") or "").strip()
            if not inp_raw or not out_raw:
                return ctx
            return replace(ctx, current_inp=Path(inp_raw), out_path=Path(out_raw))
        if any(action.startswith("resume_recreated_missing_input") for action in creating_actions):
            cursor -= 1
            continue
        return ctx
    return ctx


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
    OptTS attempt fails too, ``_scants_chain_request`` resumes the ordinary
    endpoint/reverse chain from the crashed ScanTS attempt.
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
        reaction_dir=ctx.reaction_dir,
        out_path=ctx.out_path,
        max_memory_gb=ctx.max_memory_gb_per_task(),
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


def _prior_attempt_surface_energies(state: RunState) -> list[float]:
    energies: list[float] = []
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return energies
    for attempt in attempts[:-1]:
        if not isinstance(attempt, dict):
            continue
        out_raw = attempt.get("out_path")
        if not isinstance(out_raw, str) or not out_raw.strip():
            continue
        energies.extend(point.energy for point in parse_scants_actual_surface(Path(out_raw)))
    return energies


def _stop_when_scan_profile_has_no_barrier(ctx: RetryAttemptRequest) -> None:
    """Fail the run when the completed forward profile is barrierless.

    Once the endpoint scan has finished, the forward surface segments plus the
    endpoint segment cover the whole scan range. Without an interior maximum
    above the noise threshold there is no TS along this coordinate, so a
    reverse ScanTS would only mirror the same monotonic profile; stop with a
    chemistry-actionable reason instead. Skipped (fail-open) when the endpoint
    attempt printed no surface, since the range is then not fully explored.
    """
    endpoint_energies = [point.energy for point in parse_scants_actual_surface(ctx.out_path)]
    if not endpoint_energies:
        return
    energies = _prior_attempt_surface_energies(ctx.state) + endpoint_energies
    barrier_kcal = scan_profile_interior_barrier_kcal(energies)
    if barrier_kcal is None or barrier_kcal >= SCANTS_BARRIER_NOISE_KCAL:
        return
    logger.info(
        "ScanTS forward profile is barrierless (max interior prominence %.3f kcal/mol "
        "over %d points); skipping reverse scan",
        barrier_kcal,
        len(energies),
    )
    raise ScantsRetryStop("scan_profile_no_barrier")


def _prepare_scants_retry_after_surface_maximum(
    ctx: RetryAttemptRequest,
    *,
    next_inp: Path,
) -> tuple[Path | None, list[str]]:
    """Reverse the scan from a fresh surface maximum, or finish the endpoint first.

    A direct reverse scan is only valid once the forward scan has reached its
    planned endpoint. When the maximum appears early, ``prepare_scants_reverse_...``
    fails closed and we fall back to completing the endpoint with a relaxed scan;
    the reverse scan then runs on the next retry off that real endpoint geometry.
    """
    if not _output_has_scants_actual_surface_maximum(ctx):
        return None, []
    if _state_has_scants_reverse_scan(ctx.state):
        return None, []
    prepared, actions = prepare_scants_reverse_scan_retry_input(
        source_inp=ctx.current_inp,
        selected_inp=ctx.selected_inp,
        target_inp=next_inp,
        max_memory_gb=ctx.max_memory_gb_per_task(),
    )
    if prepared is not None:
        return prepared, actions
    return prepare_scants_endpoint_scan_input(
        source_inp=ctx.current_inp,
        target_inp=next_inp,
        max_memory_gb=ctx.max_memory_gb_per_task(),
    )


def _prepare_scants_reverse_scan_after_endpoint_scan(
    ctx: RetryAttemptRequest,
    *,
    next_inp: Path,
) -> tuple[Path | None, list[str]]:
    """Reverse the scan once a preceding relaxed endpoint scan supplied the endpoint."""
    if not state_pending_scants_reverse_after_endpoint_scan(ctx.state):
        return None, []
    if _state_has_scants_reverse_scan(ctx.state):
        return None, []
    _stop_when_scan_profile_has_no_barrier(ctx)
    return prepare_scants_reverse_scan_retry_input(
        source_inp=ctx.current_inp,
        selected_inp=ctx.selected_inp,
        target_inp=next_inp,
        max_memory_gb=ctx.max_memory_gb_per_task(),
    )


def prepare_retry_attempt(ctx: RetryAttemptRequest) -> int | None:
    next_retry_number = ctx.retries_used + 1
    next_inp = ctx.retry_inp_path(ctx.selected_inp, next_retry_number)
    patch_step = retry_recipe_name_for_input(ctx.selected_inp, next_retry_number)
    try:
        chain_ctx = _scants_chain_request(ctx)
        uses_scants = input_uses_scants(chain_ctx.current_inp) or input_uses_scants(
            ctx.selected_inp
        )
        scants_endpoint_scan_seen = state_pending_scants_reverse_after_endpoint_scan(ctx.state)
        scants_surface_maximum_seen = _output_has_scants_actual_surface_maximum(chain_ctx)
        prepared_scants, patch_actions = _prepare_scants_optts_fallback(ctx, next_inp=next_inp)
        if prepared_scants is None:
            prepared_scants, patch_actions = _prepare_scants_reverse_scan_after_endpoint_scan(
                chain_ctx,
                next_inp=next_inp,
            )
        if prepared_scants is None:
            prepared_scants, patch_actions = _prepare_scants_retry_after_surface_maximum(
                chain_ctx,
                next_inp=next_inp,
            )
        if prepared_scants is None and (scants_surface_maximum_seen or scants_endpoint_scan_seen):
            raise ScantsRetryStop("scants_recipes_exhausted")
        if prepared_scants is None:
            scants_retry_source = (
                ctx.selected_inp if next_retry_number == 1 else chain_ctx.current_inp
            )
            prepared_scants, patch_actions = prepare_scants_scan_retry_input(
                source_inp=scants_retry_source,
                target_inp=next_inp,
                retry_number=next_retry_number,
                max_memory_gb=ctx.max_memory_gb_per_task(),
            )
        if prepared_scants is None:
            if uses_scants:
                raise ScantsRetryStop("scants_recipes_exhausted")
            patch_actions = rewrite_for_retry(
                source_inp=ctx.current_inp,
                target_inp=next_inp,
                reaction_dir=ctx.reaction_dir,
                step=patch_step,
                max_memory_gb=ctx.max_memory_gb_per_task(),
            )
    except ScantsRetryStop as stop:
        reason = str(stop)
        ctx.state["attempts"][-1]["patch_actions"] = [f"scants_retry_stopped:{reason}"]
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
    "ScantsRetryStop",
    "prepare_resumed_checkpoint_input",
    "prepare_retry_attempt",
    "resume_checkpoint_inp_path",
    "retry_recipe_step",
    "state_pending_scants_reverse_after_endpoint_scan",
]
