from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from orca_auto.flow.contracts.workflow import WorkflowStagePayload

if TYPE_CHECKING:
    from orca_auto.flow.runtime.models import WorkflowJournalWriter

AnyCallable = Callable[..., Any]


class MaterializedWorkflowStage(Protocol):
    def to_dict(self) -> WorkflowStagePayload: ...


@dataclass(frozen=True)
class WorkflowPersistenceServices:
    acquire_workflow_lock: AnyCallable
    load_workflow_payload: Callable[..., dict[str, Any]]
    resolve_workflow_workspace: AnyCallable
    sync_workflow_registry: AnyCallable
    write_workflow_payload: AnyCallable


@dataclass(frozen=True)
class WorkflowEngineGateway:
    build_materialized_orca_stage: Callable[..., MaterializedWorkflowStage]
    crest_cancel_target: Callable[..., dict[str, Any]]
    engine_runtime_paths: Callable[..., dict[str, Path]]
    load_crest_artifact_contract: AnyCallable
    load_orca_artifact_contract: AnyCallable
    orca_cancel_target: Callable[..., dict[str, Any]]
    safe_name: AnyCallable
    select_crest_downstream_inputs: AnyCallable
    submit_crest_job_dir: AnyCallable
    submit_reaction_dir: AnyCallable


@dataclass(frozen=True)
class WorkflowClock:
    now_utc_iso: Callable[[], str]


@dataclass(frozen=True)
class WorkflowEvents:
    append_workflow_journal_event: WorkflowJournalWriter
    notify_phase_summary: AnyCallable
    require_workflow_journal_capacity: AnyCallable


@dataclass(frozen=True)
class OrchestrationServices:
    persistence: WorkflowPersistenceServices
    engines: WorkflowEngineGateway
    clock: WorkflowClock
    events: WorkflowEvents


@cache
def default_orchestration_services() -> OrchestrationServices:
    from orca_auto.core.engine_runtime import engine_runtime_paths
    from orca_auto.core.utils import now_utc_iso
    from orca_auto.flow._orca_stage_materialization import (
        build_materialized_orca_stage,
        safe_name,
    )
    from orca_auto.flow.adapters.crest import (
        load_crest_artifact_contract,
        select_crest_downstream_inputs,
    )
    from orca_auto.flow.adapters.orca import load_orca_artifact_contract
    from orca_auto.flow.registry import (
        append_workflow_journal_event,
        require_workflow_journal_capacity,
        sync_workflow_registry,
    )
    from orca_auto.flow.state import (
        acquire_workflow_lock,
        load_workflow_payload,
        resolve_workflow_workspace,
        write_workflow_payload,
    )
    from orca_auto.flow.submitters.crest import (
        cancel_target as crest_cancel_target,
    )
    from orca_auto.flow.submitters.crest import (
        submit_job_dir as submit_crest_job_dir,
    )
    from orca_auto.flow.submitters.orca import submit_reaction_dir
    from orca_auto.flow.workflow.notifications import maybe_notify_workflow_phase_summary
    from orca_auto.orca.direct_cancel import cancel_target as orca_cancel_target

    return OrchestrationServices(
        persistence=WorkflowPersistenceServices(
            acquire_workflow_lock=acquire_workflow_lock,
            load_workflow_payload=load_workflow_payload,
            resolve_workflow_workspace=resolve_workflow_workspace,
            sync_workflow_registry=sync_workflow_registry,
            write_workflow_payload=write_workflow_payload,
        ),
        engines=WorkflowEngineGateway(
            build_materialized_orca_stage=build_materialized_orca_stage,
            crest_cancel_target=crest_cancel_target,
            engine_runtime_paths=engine_runtime_paths,
            load_crest_artifact_contract=load_crest_artifact_contract,
            load_orca_artifact_contract=load_orca_artifact_contract,
            orca_cancel_target=orca_cancel_target,
            safe_name=safe_name,
            select_crest_downstream_inputs=select_crest_downstream_inputs,
            submit_crest_job_dir=submit_crest_job_dir,
            submit_reaction_dir=submit_reaction_dir,
        ),
        clock=WorkflowClock(now_utc_iso=now_utc_iso),
        events=WorkflowEvents(
            append_workflow_journal_event=append_workflow_journal_event,
            notify_phase_summary=maybe_notify_workflow_phase_summary,
            require_workflow_journal_capacity=require_workflow_journal_capacity,
        ),
    )


def resolve_orchestration_services(
    services: OrchestrationServices | None,
) -> OrchestrationServices:
    return services if services is not None else default_orchestration_services()


__all__ = [
    "MaterializedWorkflowStage",
    "OrchestrationServices",
    "WorkflowClock",
    "WorkflowEngineGateway",
    "WorkflowEvents",
    "WorkflowPersistenceServices",
    "default_orchestration_services",
    "resolve_orchestration_services",
]
