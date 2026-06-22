from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

AnyCallable = Callable[..., Any]
WorkflowPayload = dict[str, Any]
WorkflowStagePayload = dict[str, Any]
WorkflowWorkspace = str | Path
WorkflowPayloadLoader = Callable[[Path], WorkflowPayload]
WorkflowPayloadWriter = Callable[[Path, WorkflowPayload], Any]
WorkflowWorkspaceResolver = Callable[..., Path]
WorkflowRegistrySyncer = Callable[[str | Path, Path, WorkflowPayload], Any]
WorkflowStatusComputer = Callable[[WorkflowPayload], str]
WorkflowPredicate = Callable[[WorkflowPayload], bool]
StagePredicate = Callable[[WorkflowStagePayload], bool]
MappingCoercer = Callable[[Any], dict[str, Any]]
TextNormalizer = Callable[[Any], str]
StageMetadataResolver = Callable[[WorkflowStagePayload], dict[str, Any]]


@dataclass(frozen=True)
class OrchestrationContractDeps:
    CrestDownstreamPolicy: type[Any]
    EndpointPairingPolicy: type[Any]
    WorkflowStageInput: type[Any]
    XtbDownstreamPolicy: type[Any]


@dataclass(frozen=True)
class OrchestrationPersistenceDeps:
    acquire_workflow_lock: AnyCallable
    load_workflow_payload: WorkflowPayloadLoader
    now_utc_iso: Callable[[], str]
    resolve_workflow_workspace: WorkflowWorkspaceResolver
    sync_workflow_registry: WorkflowRegistrySyncer
    write_workflow_payload: WorkflowPayloadWriter


@dataclass(frozen=True)
class OrchestrationEngineDeps:
    build_materialized_orca_stage: AnyCallable
    choose_orca_geometry_frame: AnyCallable
    crest_cancel_target: AnyCallable
    load_crest_artifact_contract: AnyCallable
    load_orca_artifact_contract: AnyCallable
    load_xtb_artifact_contract: AnyCallable
    orca_cancel_target: AnyCallable
    safe_name: AnyCallable
    select_crest_downstream_inputs: AnyCallable
    select_endpoint_pairs: AnyCallable
    select_xtb_downstream_inputs: AnyCallable
    engine_runtime_paths: AnyCallable
    submit_crest_job_dir: AnyCallable
    submit_reaction_dir: AnyCallable
    submit_xtb_job_dir: AnyCallable
    xtb_cancel_target: AnyCallable


@dataclass(frozen=True)
class OrchestrationStageBuilderDeps:
    _new_xtb_stage: AnyCallable


@dataclass(frozen=True)
class OrchestrationStageMaterializationDeps:
    _append_crest_orca_stages: AnyCallable
    _append_reaction_orca_stages: AnyCallable
    _append_reaction_xtb_stages: AnyCallable


@dataclass(frozen=True)
class OrchestrationStageRuntimeDeps:
    _append_unique_artifact: AnyCallable
    _completed_crest_roles: AnyCallable
    _completed_crest_stage: AnyCallable
    _ensure_crest_job_dir: AnyCallable
    _ensure_xtb_job_dir: AnyCallable
    _sync_crest_stage: AnyCallable
    _sync_orca_stage: AnyCallable
    _sync_xtb_stage: AnyCallable
    _write_xtb_path_job: AnyCallable
    _xtb_attempt_record: AnyCallable
    _xtb_current_attempt_number: AnyCallable
    _xtb_handoff_status: AnyCallable
    _xtb_path_retry_limit: AnyCallable
    _xtb_retry_recipe: AnyCallable


@dataclass(frozen=True)
class OrchestrationStageSupportDeps:
    _clear_reaction_xtb_handoff_error_if_recovering: AnyCallable
    _coerce_mapping: MappingCoercer
    _load_config_organized_root: AnyCallable
    _load_config_root: AnyCallable
    _normalize_text: TextNormalizer
    _reaction_orca_source_candidate_path: AnyCallable
    _reaction_ts_guess_error: AnyCallable
    _safe_int: AnyCallable
    _stage_metadata: StageMetadataResolver
    _submission_target: AnyCallable
    _task_payload_dict: AnyCallable


@dataclass(frozen=True)
class OrchestrationStageWorkflowDeps:
    _maybe_notify_workflow_phase_summary: AnyCallable
    _persist_workflow_progress: AnyCallable
    _recompute_workflow_status: WorkflowStatusComputer
    _stage_failure_is_recoverable: StagePredicate
    _workflow_has_active_children: WorkflowPredicate
    _workflow_sync_only: WorkflowPredicate


@dataclass(frozen=True)
class _OrchestrationStageDepGroup:
    name: str
    deps_type: type[Any]
    dep_names: tuple[str, ...]


def _stage_dep_group(name: str, deps_type: type[Any]) -> _OrchestrationStageDepGroup:
    return _OrchestrationStageDepGroup(
        name=name,
        deps_type=deps_type,
        dep_names=tuple(field.name for field in fields(deps_type)),
    )


_ORCHESTRATION_STAGE_BUILDER_GROUP = _stage_dep_group(
    "builders",
    OrchestrationStageBuilderDeps,
)
_ORCHESTRATION_STAGE_MATERIALIZATION_GROUP = _stage_dep_group(
    "materialization",
    OrchestrationStageMaterializationDeps,
)
_ORCHESTRATION_STAGE_RUNTIME_GROUP = _stage_dep_group(
    "runtime",
    OrchestrationStageRuntimeDeps,
)
_ORCHESTRATION_STAGE_SUPPORT_GROUP = _stage_dep_group(
    "support",
    OrchestrationStageSupportDeps,
)
_ORCHESTRATION_STAGE_WORKFLOW_GROUP = _stage_dep_group(
    "workflow",
    OrchestrationStageWorkflowDeps,
)

_ORCHESTRATION_STAGE_DEP_REGISTRY: tuple[_OrchestrationStageDepGroup, ...] = (
    _ORCHESTRATION_STAGE_BUILDER_GROUP,
    _ORCHESTRATION_STAGE_MATERIALIZATION_GROUP,
    _ORCHESTRATION_STAGE_RUNTIME_GROUP,
    _ORCHESTRATION_STAGE_SUPPORT_GROUP,
    _ORCHESTRATION_STAGE_WORKFLOW_GROUP,
)

_ORCHESTRATION_STAGE_DEP_GROUPS: Mapping[str, tuple[str, ...]] = {
    group.name: group.dep_names for group in _ORCHESTRATION_STAGE_DEP_REGISTRY
}


@dataclass(frozen=True)
class OrchestrationStageDeps:
    builders: OrchestrationStageBuilderDeps
    materialization: OrchestrationStageMaterializationDeps
    runtime: OrchestrationStageRuntimeDeps
    support: OrchestrationStageSupportDeps
    workflow: OrchestrationStageWorkflowDeps


@dataclass(frozen=True)
class OrchestrationAdvanceDeps:
    _cancel_active_workflow_stages: AnyCallable
    _cancel_stage_activity: AnyCallable


@dataclass(frozen=True)
class OrchestrationDeps:
    contracts: OrchestrationContractDeps
    persistence: OrchestrationPersistenceDeps
    engines: OrchestrationEngineDeps
    stages: OrchestrationStageDeps
    advance: OrchestrationAdvanceDeps


__all__ = [
    "AnyCallable",
    "MappingCoercer",
    "OrchestrationAdvanceDeps",
    "OrchestrationContractDeps",
    "OrchestrationDeps",
    "OrchestrationEngineDeps",
    "OrchestrationPersistenceDeps",
    "OrchestrationStageBuilderDeps",
    "OrchestrationStageDeps",
    "OrchestrationStageMaterializationDeps",
    "OrchestrationStageRuntimeDeps",
    "OrchestrationStageSupportDeps",
    "OrchestrationStageWorkflowDeps",
    "StageMetadataResolver",
    "StagePredicate",
    "TextNormalizer",
    "WorkflowPayload",
    "WorkflowPayloadLoader",
    "WorkflowPayloadWriter",
    "WorkflowPredicate",
    "WorkflowRegistrySyncer",
    "WorkflowStagePayload",
    "WorkflowStatusComputer",
    "WorkflowWorkspace",
    "WorkflowWorkspaceResolver",
    "_ORCHESTRATION_STAGE_BUILDER_GROUP",
    "_ORCHESTRATION_STAGE_DEP_GROUPS",
    "_ORCHESTRATION_STAGE_DEP_REGISTRY",
    "_ORCHESTRATION_STAGE_MATERIALIZATION_GROUP",
    "_ORCHESTRATION_STAGE_RUNTIME_GROUP",
    "_ORCHESTRATION_STAGE_SUPPORT_GROUP",
    "_ORCHESTRATION_STAGE_WORKFLOW_GROUP",
    "_OrchestrationStageDepGroup",
    "_stage_dep_group",
]
