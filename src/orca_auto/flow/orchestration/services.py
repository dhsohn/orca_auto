from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

AnyCallable = Callable[..., Any]


@dataclass(frozen=True)
class WorkflowPersistenceServices:
    acquire_workflow_lock: AnyCallable
    load_workflow_payload: AnyCallable
    resolve_workflow_workspace: AnyCallable
    sync_workflow_registry: AnyCallable
    write_workflow_payload: AnyCallable


@dataclass(frozen=True)
class WorkflowEngineGateway:
    build_materialized_orca_stage: AnyCallable
    choose_orca_geometry_frame: AnyCallable
    crest_cancel_target: AnyCallable
    engine_runtime_paths: AnyCallable
    load_crest_artifact_contract: AnyCallable
    load_orca_artifact_contract: AnyCallable
    load_xtb_artifact_contract: AnyCallable
    orca_cancel_target: AnyCallable
    safe_name: AnyCallable
    select_crest_downstream_inputs: AnyCallable
    select_endpoint_pairs: AnyCallable
    select_xtb_downstream_inputs: AnyCallable
    submit_crest_job_dir: AnyCallable
    submit_reaction_dir: AnyCallable
    submit_xtb_job_dir: AnyCallable
    xtb_cancel_target: AnyCallable


@dataclass(frozen=True)
class WorkflowClock:
    now_utc_iso: Callable[[], str]


@dataclass(frozen=True)
class WorkflowEvents:
    append_workflow_journal_event: AnyCallable
    notify_phase_summary: AnyCallable


@dataclass(frozen=True)
class OrchestrationServices:
    persistence: WorkflowPersistenceServices
    engines: WorkflowEngineGateway
    clock: WorkflowClock
    events: WorkflowEvents


@cache
def default_orchestration_services() -> OrchestrationServices:
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
    from orca_auto.flow.adapters.xtb import (
        load_xtb_artifact_contract,
        select_xtb_downstream_inputs,
    )
    from orca_auto.flow.endpoint_pairing import select_endpoint_pairs
    from orca_auto.flow.engine_runtime import engine_runtime_paths
    from orca_auto.flow.registry import sync_workflow_registry
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
    from orca_auto.flow.submitters.orca import cancel_target as orca_cancel_target
    from orca_auto.flow.submitters.orca import submit_reaction_dir
    from orca_auto.flow.submitters.xtb import cancel_target as xtb_cancel_target
    from orca_auto.flow.submitters.xtb import submit_job_dir as submit_xtb_job_dir
    from orca_auto.flow.workflow.journal import append_workflow_journal_event
    from orca_auto.flow.workflow.notifications import maybe_notify_workflow_phase_summary
    from orca_auto.flow.xyz_utils import choose_orca_geometry_frame

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
            choose_orca_geometry_frame=choose_orca_geometry_frame,
            crest_cancel_target=crest_cancel_target,
            engine_runtime_paths=engine_runtime_paths,
            load_crest_artifact_contract=load_crest_artifact_contract,
            load_orca_artifact_contract=load_orca_artifact_contract,
            load_xtb_artifact_contract=load_xtb_artifact_contract,
            orca_cancel_target=orca_cancel_target,
            safe_name=safe_name,
            select_crest_downstream_inputs=select_crest_downstream_inputs,
            select_endpoint_pairs=select_endpoint_pairs,
            select_xtb_downstream_inputs=select_xtb_downstream_inputs,
            submit_crest_job_dir=submit_crest_job_dir,
            submit_reaction_dir=submit_reaction_dir,
            submit_xtb_job_dir=submit_xtb_job_dir,
            xtb_cancel_target=xtb_cancel_target,
        ),
        clock=WorkflowClock(now_utc_iso=now_utc_iso),
        events=WorkflowEvents(
            append_workflow_journal_event=append_workflow_journal_event,
            notify_phase_summary=maybe_notify_workflow_phase_summary,
        ),
    )


def resolve_orchestration_services(
    services: OrchestrationServices | None,
) -> OrchestrationServices:
    return services if services is not None else default_orchestration_services()


__all__ = [
    "OrchestrationServices",
    "WorkflowClock",
    "WorkflowEngineGateway",
    "WorkflowEvents",
    "WorkflowPersistenceServices",
    "default_orchestration_services",
    "resolve_orchestration_services",
]
