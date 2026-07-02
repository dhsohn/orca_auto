from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import runtime_models
from .engine_options import WorkflowEngineOptions

WorkflowAdvanceResult = runtime_models.WorkflowAdvanceResult
_WorkflowCycle = runtime_models._WorkflowCycle
_WorkflowCycleProgress = runtime_models._WorkflowCycleProgress


@dataclass(frozen=True)
class WorkflowAdvanceDeps:
    advance_workflow_fn: Callable[..., dict[str, Any]]
    safe_workflow_summary_fn: Callable[..., dict[str, Any]]
    workflow_is_terminal_status_fn: Callable[[Any], bool]
    workflow_needs_terminal_child_sync_fn: Callable[..., bool]
    append_workflow_advance_failed_event_fn: Callable[..., Any]
    append_workflow_advanced_events_fn: Callable[..., Any]
    append_phase_transition_events_fn: Callable[..., Any]
    append_stage_transition_events_fn: Callable[..., Any]
    append_workflow_journal_event_fn: Callable[..., Any]
    workflow_skipped_terminal_result_fn: Callable[..., WorkflowAdvanceResult]
    workflow_advance_failed_result_fn: Callable[..., WorkflowAdvanceResult]
    workflow_advanced_result_fn: Callable[..., WorkflowAdvanceResult]
    normalize_text_fn: Callable[[Any], str]


@dataclass(frozen=True)
class WorkflowAdvanceOutcome:
    outcome: str
    result: WorkflowAdvanceResult


def skipped_terminal_workflow_outcome(
    record: Any,
    *,
    previous_status: str,
    deps: WorkflowAdvanceDeps,
) -> WorkflowAdvanceOutcome:
    return WorkflowAdvanceOutcome(
        "skipped",
        deps.workflow_skipped_terminal_result_fn(
            record,
            previous_status=previous_status,
        ),
    )


def failed_workflow_advance_outcome(
    *,
    cycle: _WorkflowCycle,
    record: Any,
    previous_status: str,
    reason: str,
    deps: WorkflowAdvanceDeps,
) -> WorkflowAdvanceOutcome:
    deps.append_workflow_advance_failed_event_fn(
        cycle.root,
        previous_status=previous_status,
        reason=reason,
        worker_session_id=cycle.session_id,
        record=record,
        append_workflow_journal_event_fn=deps.append_workflow_journal_event_fn,
    )
    return WorkflowAdvanceOutcome(
        "failed",
        deps.workflow_advance_failed_result_fn(
            record,
            previous_status=previous_status,
            reason=reason,
        ),
    )


def advanced_workflow_outcome(
    *,
    cycle: _WorkflowCycle,
    record: Any,
    payload: dict[str, Any],
    previous_status: str,
    previous_summary: dict[str, Any],
    terminal_sync: bool,
    deps: WorkflowAdvanceDeps,
) -> WorkflowAdvanceOutcome:
    status = deps.normalize_text_fn(payload.get("status")).lower()
    current_summary = deps.safe_workflow_summary_fn(record.workspace_dir, payload=payload)
    reason = "terminal_child_sync" if terminal_sync else ""
    deps.append_workflow_advanced_events_fn(
        cycle.root,
        record,
        payload,
        previous_status=previous_status,
        previous_summary=previous_summary,
        current_summary=current_summary,
        worker_session_id=cycle.session_id,
        reason=reason,
        append_workflow_journal_event_fn=deps.append_workflow_journal_event_fn,
        append_phase_transition_events_fn=deps.append_phase_transition_events_fn,
        append_stage_transition_events_fn=deps.append_stage_transition_events_fn,
        normalize_text_fn=deps.normalize_text_fn,
    )
    return WorkflowAdvanceOutcome(
        "advanced",
        deps.workflow_advanced_result_fn(
            record,
            payload,
            previous_status=previous_status,
            status=status,
            reason=reason,
            normalize_text_fn=deps.normalize_text_fn,
        ),
    )


def advance_workflow_record_outcome(
    *,
    cycle: _WorkflowCycle,
    record: Any,
    options: WorkflowEngineOptions,
    deps: WorkflowAdvanceDeps,
) -> WorkflowAdvanceOutcome:
    previous_status = deps.normalize_text_fn(record.status).lower()
    terminal_sync = deps.workflow_needs_terminal_child_sync_fn(
        record,
        previous_status=previous_status,
    )
    if deps.workflow_is_terminal_status_fn(previous_status) and not terminal_sync:
        return skipped_terminal_workflow_outcome(
            record,
            previous_status=previous_status,
            deps=deps,
        )

    previous_summary = deps.safe_workflow_summary_fn(record.workspace_dir)
    try:
        payload = deps.advance_workflow_fn(
            target=record.workflow_id,
            workflow_root=cycle.root,
            engine_options=options,
            submit_ready=False if terminal_sync else cycle.cycle_submit_ready,
        )
    except Exception as exc:  # noqa: BLE001
        reason = f"terminal_child_sync_failed: {exc}" if terminal_sync else str(exc)
        return failed_workflow_advance_outcome(
            cycle=cycle,
            record=record,
            previous_status=previous_status,
            reason=reason,
            deps=deps,
        )

    return advanced_workflow_outcome(
        cycle=cycle,
        record=record,
        payload=payload,
        previous_status=previous_status,
        previous_summary=previous_summary,
        terminal_sync=terminal_sync,
        deps=deps,
    )


def advance_workflow_records(
    *,
    cycle: _WorkflowCycle,
    records: list[Any],
    options: WorkflowEngineOptions,
    deps: WorkflowAdvanceDeps,
) -> _WorkflowCycleProgress:
    workflow_results: list[WorkflowAdvanceResult] = []
    advanced_count = 0
    skipped_count = 0
    failed_count = 0
    for record in records:
        outcome = advance_workflow_record_outcome(
            cycle=cycle,
            record=record,
            options=options,
            deps=deps,
        )
        workflow_results.append(outcome.result)
        if outcome.outcome == "advanced":
            advanced_count += 1
        elif outcome.outcome == "skipped":
            skipped_count += 1
        elif outcome.outcome == "failed":
            failed_count += 1
    return _WorkflowCycleProgress(
        workflow_results=workflow_results,
        advanced_count=advanced_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
    )


__all__ = [
    "WorkflowAdvanceDeps",
    "WorkflowAdvanceOutcome",
    "advance_workflow_record_outcome",
    "advance_workflow_records",
    "advanced_workflow_outcome",
    "failed_workflow_advance_outcome",
    "skipped_terminal_workflow_outcome",
]
