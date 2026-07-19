from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.admission import active_slot_count
from orca_auto.core.paths.workflow import validate_workflow_workspace_identity
from orca_auto.core.utils import now_utc_iso, timestamped_token

from ..engine_options import WorkflowEngineOptions
from ..orchestration import advance_workflow
from ..registry import (
    append_workflow_journal_event,
    list_workflow_registry,
    reindex_workflow_registry,
)
from ..stage_transition_events import (
    append_phase_transition_events,
    append_stage_transition_events,
    append_workflow_advance_failed_event,
    append_workflow_advanced_events,
    stage_transition_event_payloads,
)
from ..state import (
    load_workflow_payload,
    resolve_workflow_workspace,
    workflow_has_active_downstream,
    workflow_summary,
)
from ..workflow._phases import phase_transition_event_payloads
from . import _common as _runtime_common
from . import admission as runtime_admission
from . import advance as runtime_advance
from . import models as runtime_models
from .cycle import (
    WorkflowCycleDeps as WorkflowCycleDeps,
)
from .cycle import (
    finish_workflow_cycle,
    start_workflow_cycle,
    workflow_lease_expires_at,
)
from .cycle import (
    start_workflow_cycle_with_deps as start_workflow_cycle_with_deps,
)
from .results import (
    TERMINAL_WORKFLOW_STATUSES,
    si_publish_retry_due,
    workflow_advance_failed_result,
    workflow_advanced_result,
    workflow_needs_terminal_sync,
    workflow_skipped_terminal_result,
)

WORKFLOW_WORKER_LOCK_NAME = "workflow_worker.lock"

StageTransitionContext = runtime_models.StageTransitionContext
WorkflowAdvanceResult = runtime_models.WorkflowAdvanceResult
WorkflowJournalEventPayload = runtime_models.WorkflowJournalEventPayload
WorkflowRegistryCyclePayload = runtime_models.WorkflowRegistryCyclePayload
WorkflowRegistryAdvanceRequest = runtime_models.WorkflowRegistryAdvanceRequest
WorkflowRuntimeContext = runtime_models.WorkflowRuntimeContext
_WorkflowCycle = runtime_models._WorkflowCycle
_WorkflowCycleProgress = runtime_models._WorkflowCycleProgress
WorkflowAdvanceDeps = runtime_advance.WorkflowAdvanceDeps
WorkflowAdvanceOutcome = runtime_advance.WorkflowAdvanceOutcome

submission_admission_limit_from_config = runtime_admission.submission_admission_limit_from_config
submission_admission_has_capacity = runtime_admission.submission_admission_has_capacity
workflow_submission_has_capacity = runtime_admission.workflow_submission_has_capacity

_advanced_workflow_outcome = runtime_advance.advanced_workflow_outcome
_failed_workflow_advance_outcome = runtime_advance.failed_workflow_advance_outcome
_skipped_terminal_workflow_outcome = runtime_advance.skipped_terminal_workflow_outcome
_advance_workflow_record_outcome = runtime_advance.advance_workflow_record_outcome


def _safe_workflow_summary(
    workspace_dir: str | Path,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return workflow_summary(workspace_dir, payload=payload)
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def _append_stage_transition_events(
    workflow_root: str | Path,
    *,
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
    append_workflow_journal_event_fn: Callable[..., Any],
) -> None:
    _append_summary_transition_events(
        workflow_root,
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
        append_fn=append_stage_transition_events,
        payloads_kwarg="stage_transition_event_payloads_fn",
        payloads_fn=stage_transition_event_payloads,
        append_workflow_journal_event_fn=append_workflow_journal_event_fn,
    )


def _append_summary_transition_events(
    workflow_root: str | Path,
    *,
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
    append_fn: Any,
    payloads_kwarg: str,
    payloads_fn: Any,
    append_workflow_journal_event_fn: Callable[..., Any],
) -> None:
    append_fn(
        workflow_root,
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
        **{
            payloads_kwarg: payloads_fn,
            "append_workflow_journal_event_fn": append_workflow_journal_event_fn,
        },
    )


def _append_phase_transition_events(
    workflow_root: str | Path,
    *,
    previous_summary: dict[str, Any],
    current_summary: dict[str, Any],
    workflow_id: str,
    template_name: str,
    worker_session_id: str,
    append_workflow_journal_event_fn: Callable[..., Any],
) -> None:
    _append_summary_transition_events(
        workflow_root,
        previous_summary=previous_summary,
        current_summary=current_summary,
        workflow_id=workflow_id,
        template_name=template_name,
        worker_session_id=worker_session_id,
        append_fn=append_phase_transition_events,
        payloads_kwarg="phase_transition_event_payloads_fn",
        payloads_fn=phase_transition_event_payloads,
        append_workflow_journal_event_fn=append_workflow_journal_event_fn,
    )


def workflow_worker_lock_path(workflow_root: str | Path) -> Path:
    return Path(workflow_root).expanduser().resolve() / WORKFLOW_WORKER_LOCK_NAME


def _workflow_is_terminal_status(status: Any) -> bool:
    return _runtime_common.normalize_text(status).lower() in TERMINAL_WORKFLOW_STATUSES


def _workflow_needs_terminal_sync(workspace_dir: str | Path) -> bool:
    return workflow_needs_terminal_sync(
        workspace_dir,
        load_workflow_payload_fn=load_workflow_payload,
        workflow_has_active_downstream_fn=workflow_has_active_downstream,
        normalize_text_fn=_runtime_common.normalize_text,
    )


def _workflow_needs_terminal_child_sync(
    record: Any,
    *,
    previous_status: str,
    workspace_dir: str | Path | None = None,
) -> bool:
    if not _workflow_is_terminal_status(previous_status):
        return False
    resolved_workspace = workspace_dir or record.workspace_dir
    payload_needs_sync = _workflow_needs_terminal_sync(resolved_workspace)
    if payload_needs_sync:
        return True
    try:
        payload = load_workflow_payload(resolved_workspace)
    except (FileNotFoundError, ValueError):
        payload = {}
        payload_loaded = False
    else:
        payload_loaded = True
    payload_status = _runtime_common.normalize_text(payload.get("status")).lower()
    payload_workflow_id = _runtime_common.normalize_text(payload.get("workflow_id"))
    record_workflow_id = _runtime_common.normalize_text(getattr(record, "workflow_id", ""))
    payload_metadata = payload.get("metadata")
    record_metadata = getattr(record, "metadata", {})
    workflow_error = (
        payload_metadata.get("workflow_error") if isinstance(payload_metadata, dict) else None
    )
    identity_error_marked = (
        isinstance(workflow_error, dict)
        and workflow_error.get("scope") == "workflow_identity_validation"
    )
    authoritative_workflow_id = ""
    identity_valid = False
    if payload_loaded:
        try:
            authoritative_workflow_id = validate_workflow_workspace_identity(
                resolved_workspace,
                payload_workflow_id,
            )
        except (OSError, TypeError, ValueError):
            pass
        else:
            identity_valid = True
    identity_quarantined = (
        payload_status == "failed" and identity_error_marked and not identity_valid
    )
    if payload_loaded:
        payload_pending = isinstance(payload_metadata, dict) and bool(
            payload_metadata.get("si_publish_pending")
        )
        payload_blocked = isinstance(payload_metadata, dict) and bool(
            payload_metadata.get("si_publish_blocked")
        )
        payload_child_pending = isinstance(payload_metadata, dict) and bool(
            payload_metadata.get("final_child_sync_pending")
        )
        record_pending = isinstance(record_metadata, dict) and bool(
            record_metadata.get("si_publish_pending")
        )
        record_blocked = isinstance(record_metadata, dict) and bool(
            record_metadata.get("si_publish_blocked")
        )
        record_child_pending = isinstance(record_metadata, dict) and bool(
            record_metadata.get("final_child_sync_pending")
        )
        if (
            payload_pending != record_pending
            or payload_blocked != record_blocked
            or payload_child_pending != record_child_pending
        ):
            # A durable publication/child-sync checkpoint may have committed
            # before registry sync. Reconcile the marker drift exactly once;
            # this does not retry a writer that is blocked or not yet due.
            return True
        record_quarantine_marker = isinstance(record_metadata, dict) and bool(
            record_metadata.get("identity_quarantined")
            or _runtime_common.normalize_text(
                record_metadata.get("quarantined_persisted_workflow_id")
            )
        )
        record_reconciliation_marker = isinstance(record_metadata, dict) and bool(
            record_metadata.get("identity_reconciliation_required")
            or _runtime_common.normalize_text(
                record_metadata.get("identity_reconciliation_persisted_workflow_id")
            )
        )
        if not identity_quarantined and (record_quarantine_marker or record_reconciliation_marker):
            # Identity recovery may have committed in workflow.json before the
            # normalized registry marker was cleared. Refresh that cached row.
            return True
        if identity_error_marked and identity_valid:
            # The workspace/ID invariant has been restored but the durable
            # quarantine error survived a prior crash. Clear it in one pass.
            return True
    if identity_quarantined:
        # The first mismatch pass persists the quarantine and active-child sync
        # is handled above. A crash may still have left the cached row at its old
        # status or without the normalized quarantine identity; reconcile that
        # once, then suppress the intentionally irreconcilable durable ID.
        cached_quarantine_id = (
            _runtime_common.normalize_text(record_metadata.get("quarantined_persisted_workflow_id"))
            if isinstance(record_metadata, dict)
            else ""
        )
        try:
            resolved_path = Path(resolved_workspace).expanduser().resolve()
            cached_path = Path(getattr(record, "workspace_dir", "")).expanduser().resolve()
        except OSError:
            trusted_cached_identity = False
        else:
            trusted_cached_identity = (
                resolved_path == cached_path and resolved_path.name == record_workflow_id
            )
        return not (
            previous_status == "failed"
            and record_quarantine_marker
            and cached_quarantine_id == payload_workflow_id
            and trusted_cached_identity
        )
    if not payload_loaded:
        # No authoritative state exists to reconcile. Keep the cached terminal
        # row visible and idle; only an explicitly due cached SI retry warrants
        # another attempt instead of a permanent missing-path hot loop.
        return isinstance(record_metadata, dict) and si_publish_retry_due(record_metadata)
    if not identity_valid:
        # Reindex/cancellation can expose a terminal payload before ordinary
        # advance has quarantined its directory/ID mismatch. Force that one
        # validation pass even when cached and persisted IDs agree with each other.
        return True
    if authoritative_workflow_id != record_workflow_id:
        # Upgrade/self-heal legacy cached rows that predate workspace-name
        # normalization for payloads with an omitted workflow_id.
        return True
    if payload_status and payload_status != previous_status:
        # A workflow mutation may have committed authoritative state and crashed
        # before registry sync. Force one cycle to reconcile the stale terminal row.
        return True
    if payload_workflow_id and record_workflow_id and payload_workflow_id != record_workflow_id:
        return True
    if isinstance(payload_metadata, dict) and bool(payload_metadata.get("si_publish_pending")):
        return si_publish_retry_due(payload_metadata)
    return isinstance(record_metadata, dict) and si_publish_retry_due(record_metadata)


def _start_workflow_cycle(
    *,
    context: WorkflowRuntimeContext,
) -> _WorkflowCycle:
    def cycle_submission_has_capacity(*config_paths: str | Path | None) -> bool:
        def submission_limit(config_path: str | Path) -> int | None:
            return submission_admission_limit_from_config(
                config_path,
                positive_int_fn=_runtime_common.positive_int,
            )

        def submission_has_capacity(config_path: str | Path) -> bool | None:
            return submission_admission_has_capacity(
                config_path,
                submission_admission_limit_from_config_fn=submission_limit,
                active_slot_count_fn=active_slot_count,
            )

        return workflow_submission_has_capacity(
            *config_paths,
            submission_admission_has_capacity_fn=submission_has_capacity,
            normalize_text_fn=_runtime_common.normalize_text,
        )

    return start_workflow_cycle(
        context=context,
        now_utc_iso_fn=now_utc_iso,
        timestamped_token_fn=timestamped_token,
        workflow_submission_has_capacity_fn=cycle_submission_has_capacity,
    )


def _workflow_advance_deps() -> WorkflowAdvanceDeps:
    append_workflow_journal_event_fn = append_workflow_journal_event

    def append_phase_transition_events_fn(
        workflow_root: str | Path,
        **kwargs: Any,
    ) -> None:
        _append_phase_transition_events(
            workflow_root,
            append_workflow_journal_event_fn=append_workflow_journal_event_fn,
            **kwargs,
        )

    def append_stage_transition_events_fn(
        workflow_root: str | Path,
        **kwargs: Any,
    ) -> None:
        _append_stage_transition_events(
            workflow_root,
            append_workflow_journal_event_fn=append_workflow_journal_event_fn,
            **kwargs,
        )

    return WorkflowAdvanceDeps(
        advance_workflow_fn=advance_workflow,
        resolve_workflow_workspace_fn=resolve_workflow_workspace,
        load_workflow_payload_fn=load_workflow_payload,
        safe_workflow_summary_fn=_safe_workflow_summary,
        workflow_is_terminal_status_fn=_workflow_is_terminal_status,
        workflow_needs_terminal_child_sync_fn=_workflow_needs_terminal_child_sync,
        append_workflow_advance_failed_event_fn=append_workflow_advance_failed_event,
        append_workflow_advanced_events_fn=append_workflow_advanced_events,
        append_phase_transition_events_fn=append_phase_transition_events_fn,
        append_stage_transition_events_fn=append_stage_transition_events_fn,
        append_workflow_journal_event_fn=append_workflow_journal_event_fn,
        workflow_skipped_terminal_result_fn=workflow_skipped_terminal_result,
        workflow_advance_failed_result_fn=workflow_advance_failed_result,
        workflow_advanced_result_fn=workflow_advanced_result,
        normalize_text_fn=_runtime_common.normalize_text,
    )


def _advance_workflow_records(
    *,
    cycle: _WorkflowCycle,
    records: list[Any],
    options: WorkflowEngineOptions,
) -> _WorkflowCycleProgress:
    return runtime_advance.advance_workflow_records(
        cycle=cycle,
        records=records,
        options=options,
        deps=_workflow_advance_deps(),
    )


def _finish_workflow_cycle() -> str:
    return finish_workflow_cycle(now_utc_iso_fn=now_utc_iso)


def _workflow_registry_records(context: WorkflowRuntimeContext) -> list[Any]:
    if context.refresh_registry:
        return reindex_workflow_registry(context.root)
    return list_workflow_registry(context.root)


def _workflow_registry_cycle_payload(
    *,
    context: WorkflowRuntimeContext,
    request: WorkflowRegistryAdvanceRequest,
    cycle: _WorkflowCycle,
    records: list[Any],
    progress: _WorkflowCycleProgress,
    cycle_finished_at: str,
) -> WorkflowRegistryCyclePayload:
    return {
        "workflow_root": str(context.root),
        "worker_session_id": cycle.session_id,
        "cycle_started_at": cycle.cycle_started_at,
        "cycle_finished_at": cycle_finished_at,
        "refresh_registry": bool(request.refresh_registry),
        "submit_ready": cycle.cycle_submit_ready,
        "requested_submit_ready": cycle.requested_submit_ready,
        "admission_blocked": cycle.admission_blocked,
        "discovered_count": len(records),
        "advanced_count": progress.advanced_count,
        "skipped_count": progress.skipped_count,
        "failed_count": progress.failed_count,
        "workflow_results": progress.workflow_results,
    }


def advance_workflow_registry_once(
    *,
    workflow_root: str | Path,
    shared_config: str | None = None,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
    submit_ready: bool = True,
    refresh_registry: bool = False,
    worker_session_id: str = "",
    interval_seconds: float | None = None,
    lease_seconds: float = 60.0,
) -> WorkflowRegistryCyclePayload:
    request = WorkflowRegistryAdvanceRequest.from_values(
        workflow_root=workflow_root,
        shared_config=shared_config,
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
        orca_repo_root=orca_repo_root,
        submit_ready=submit_ready,
        refresh_registry=refresh_registry,
        worker_session_id=worker_session_id,
        interval_seconds=interval_seconds,
        lease_seconds=lease_seconds,
    )
    return _advance_workflow_registry_request(request)


def _advance_workflow_registry_request(
    request: WorkflowRegistryAdvanceRequest,
) -> WorkflowRegistryCyclePayload:
    runtime_context = request.runtime_context()
    cycle = _start_workflow_cycle(context=runtime_context)
    records = _workflow_registry_records(runtime_context)
    progress = _advance_workflow_records(
        cycle=cycle,
        records=records,
        options=request.options,
    )
    cycle_finished_at = _finish_workflow_cycle()
    return _workflow_registry_cycle_payload(
        context=runtime_context,
        request=request,
        cycle=cycle,
        records=records,
        progress=progress,
        cycle_finished_at=cycle_finished_at,
    )


__all__ = [
    "TERMINAL_WORKFLOW_STATUSES",
    "WorkflowRegistryAdvanceRequest",
    "WORKFLOW_WORKER_LOCK_NAME",
    "WorkflowRuntimeContext",
    "advance_workflow_registry_once",
    "workflow_lease_expires_at",
    "workflow_worker_lock_path",
]
