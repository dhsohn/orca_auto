from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import _common as _runtime_common
from .models import WorkflowRuntimeContext, _WorkflowCycle, _WorkflowCycleProgress


@dataclass(frozen=True)
class WorkflowCycleDeps:
    now_utc_iso_fn: Callable[[], str]
    timestamped_token_fn: Callable[[str], str]
    workflow_submission_has_capacity_fn: Callable[..., bool]
    write_workflow_worker_state_fn: Callable[..., Any]
    append_workflow_journal_event_fn: Callable[..., Any]
    workflow_lease_expires_at_fn: Callable[[float], str]


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
    lease_expires_at = deps.workflow_lease_expires_at_fn(context.lease_seconds)

    deps.write_workflow_worker_state_fn(
        context.root,
        worker_session_id=session_id,
        status="running",
        workflow_root_path=context.root,
        last_cycle_started_at=cycle_started_at,
        last_heartbeat_at=cycle_started_at,
        lease_expires_at=lease_expires_at,
        interval_seconds=context.interval_seconds,
        submit_ready=cycle_submit_ready,
        metadata={"admission_blocked": True} if admission_blocked else None,
    )
    deps.append_workflow_journal_event_fn(
        context.root,
        event_type="worker_cycle_started",
        worker_session_id=session_id,
        metadata={
            "cycle_started_at": cycle_started_at,
            "refresh_registry": bool(context.refresh_registry),
            "submit_ready": cycle_submit_ready,
            "requested_submit_ready": requested_submit_ready,
            "admission_blocked": admission_blocked,
        },
    )
    return _WorkflowCycle(
        root=context.root,
        cycle_started_at=cycle_started_at,
        session_id=session_id,
        requested_submit_ready=requested_submit_ready,
        cycle_submit_ready=cycle_submit_ready,
        admission_blocked=admission_blocked,
        lease_expires_at=lease_expires_at,
    )


def start_workflow_cycle(
    *,
    context: WorkflowRuntimeContext,
    now_utc_iso_fn: Callable[[], str],
    timestamped_token_fn: Callable[[str], str],
    workflow_submission_has_capacity_fn: Callable[..., bool],
    write_workflow_worker_state_fn: Callable[..., Any],
    append_workflow_journal_event_fn: Callable[..., Any],
    workflow_lease_expires_at_fn: Callable[[float], str] = workflow_lease_expires_at,
) -> _WorkflowCycle:
    return start_workflow_cycle_with_deps(
        context=context,
        deps=WorkflowCycleDeps(
            now_utc_iso_fn=now_utc_iso_fn,
            timestamped_token_fn=timestamped_token_fn,
            workflow_submission_has_capacity_fn=workflow_submission_has_capacity_fn,
            write_workflow_worker_state_fn=write_workflow_worker_state_fn,
            append_workflow_journal_event_fn=append_workflow_journal_event_fn,
            workflow_lease_expires_at_fn=workflow_lease_expires_at_fn,
        ),
    )


def finish_workflow_cycle(
    *,
    cycle: _WorkflowCycle,
    discovered_count: int,
    progress: _WorkflowCycleProgress,
    interval_seconds: float | None,
    now_utc_iso_fn: Callable[[], str],
    write_workflow_worker_state_fn: Callable[..., Any],
    append_workflow_journal_event_fn: Callable[..., Any],
) -> str:
    cycle_finished_at = now_utc_iso_fn()
    finished_metadata = {
        "discovered_count": discovered_count,
        "advanced_count": progress.advanced_count,
        "skipped_count": progress.skipped_count,
        "failed_count": progress.failed_count,
    }
    if cycle.admission_blocked:
        finished_metadata["admission_blocked"] = True
    write_workflow_worker_state_fn(
        cycle.root,
        worker_session_id=cycle.session_id,
        status="idle",
        workflow_root_path=cycle.root,
        last_cycle_started_at=cycle.cycle_started_at,
        last_cycle_finished_at=cycle_finished_at,
        last_heartbeat_at=cycle_finished_at,
        lease_expires_at=cycle.lease_expires_at,
        interval_seconds=interval_seconds,
        submit_ready=cycle.cycle_submit_ready,
        metadata=finished_metadata,
    )
    append_workflow_journal_event_fn(
        cycle.root,
        event_type="worker_cycle_finished",
        worker_session_id=cycle.session_id,
        metadata={
            "cycle_started_at": cycle.cycle_started_at,
            "cycle_finished_at": cycle_finished_at,
            "discovered_count": discovered_count,
            "advanced_count": progress.advanced_count,
            "skipped_count": progress.skipped_count,
            "failed_count": progress.failed_count,
            "admission_blocked": cycle.admission_blocked,
        },
    )
    return cycle_finished_at


__all__ = [
    "WorkflowCycleDeps",
    "finish_workflow_cycle",
    "start_workflow_cycle",
    "start_workflow_cycle_with_deps",
    "workflow_lease_expires_at",
]
