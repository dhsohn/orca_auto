from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import _common as _runtime_common
from .models import WorkflowRuntimeContext, _WorkflowCycle


@dataclass(frozen=True)
class WorkflowCycleDeps:
    now_utc_iso_fn: Callable[[], str]
    timestamped_token_fn: Callable[[str], str]
    workflow_submission_has_capacity_fn: Callable[..., bool]


def workflow_lease_expires_at(lease_seconds: float) -> str:
    if lease_seconds <= 0:
        return ""
    return (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()


def start_workflow_cycle_with_deps(
    *,
    context: WorkflowRuntimeContext,
    deps: WorkflowCycleDeps,
) -> _WorkflowCycle:
    cycle_started_at = deps.now_utc_iso_fn()
    session_id = _runtime_common.normalize_text(
        context.worker_session_id
    ) or deps.timestamped_token_fn("wf_worker")
    requested_submit_ready = bool(context.submit_ready)
    cycle_submit_ready = requested_submit_ready and deps.workflow_submission_has_capacity_fn(
        context.options.crest_config,
        context.options.xtb_config,
        context.options.orca_config,
    )
    admission_blocked = requested_submit_ready and not cycle_submit_ready
    return _WorkflowCycle(
        root=context.root,
        cycle_started_at=cycle_started_at,
        session_id=session_id,
        requested_submit_ready=requested_submit_ready,
        cycle_submit_ready=cycle_submit_ready,
        admission_blocked=admission_blocked,
    )


def start_workflow_cycle(
    *,
    context: WorkflowRuntimeContext,
    now_utc_iso_fn: Callable[[], str],
    timestamped_token_fn: Callable[[str], str],
    workflow_submission_has_capacity_fn: Callable[..., bool],
) -> _WorkflowCycle:
    return start_workflow_cycle_with_deps(
        context=context,
        deps=WorkflowCycleDeps(
            now_utc_iso_fn=now_utc_iso_fn,
            timestamped_token_fn=timestamped_token_fn,
            workflow_submission_has_capacity_fn=workflow_submission_has_capacity_fn,
        ),
    )


def finish_workflow_cycle(
    *,
    now_utc_iso_fn: Callable[[], str],
) -> str:
    return now_utc_iso_fn()


__all__ = [
    "WorkflowCycleDeps",
    "finish_workflow_cycle",
    "start_workflow_cycle",
    "start_workflow_cycle_with_deps",
    "workflow_lease_expires_at",
]
