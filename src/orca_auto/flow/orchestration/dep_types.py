from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Protocol

AnyCallable = Callable[..., Any]
WorkflowPayload = dict[str, Any]
WorkflowStagePayload = dict[str, Any]
WorkflowWorkspace = str | Path
WorkflowPayloadLoader = Callable[[Path], WorkflowPayload]
WorkflowPayloadWriter = Callable[[Path, WorkflowPayload], Any]
WorkflowRegistrySyncer = Callable[[str | Path, Path, WorkflowPayload], Any]
WorkflowStatusComputer = Callable[[WorkflowPayload], str]
WorkflowPredicate = Callable[[WorkflowPayload], bool]
StagePredicate = Callable[[WorkflowStagePayload], bool]
MappingCoercer = Callable[[Any], dict[str, Any]]
TextNormalizer = Callable[[Any], str]
StageMetadataResolver = Callable[[WorkflowStagePayload], dict[str, Any]]
StageMaterializer = Callable[..., bool]


class WorkflowLockFactory(Protocol):
    def __call__(
        self,
        workspace_dir: WorkflowWorkspace,
        *,
        timeout_seconds: float = 10.0,
    ) -> Any: ...


class WorkflowWorkspaceResolver(Protocol):
    def __call__(
        self,
        *,
        target: str,
        workflow_root: WorkflowWorkspace | None = None,
    ) -> Path: ...


class MaterializedOrcaStageBuilder(Protocol):
    def __call__(
        self,
        *,
        workflow_id: str,
        template_name: str,
        stage_id: str,
        stage_key: str,
        stage_root_name: str,
        workspace_dir: Path,
        input_artifact_kind: str,
        candidate: Any,
        task_kind: str,
        route_line: str,
        charge: int,
        multiplicity: int,
        max_cores: int,
        max_memory_gb: int,
        priority: int,
        xyz_filename: str,
        inp_filename: str,
        input_label: str | None = None,
    ) -> Any: ...


class OrcaGeometryFrameChooser(Protocol):
    def __call__(
        self,
        path: WorkflowWorkspace,
        *,
        candidate_kind: str = "",
        source_frame_index: int = 0,
    ) -> tuple[Any | None, dict[str, object]]: ...


class CrestArtifactContractLoader(Protocol):
    def __call__(self, *, crest_index_root: WorkflowWorkspace, target: str) -> Any: ...


class OrcaArtifactContractLoader(Protocol):
    def __call__(
        self,
        *,
        target: str,
        orca_allowed_root: WorkflowWorkspace | None = None,
        orca_organized_root: WorkflowWorkspace | None = None,
        queue_id: str = "",
        run_id: str = "",
        reaction_dir: str = "",
    ) -> Any: ...


class XtbArtifactContractLoader(Protocol):
    def __call__(self, *, xtb_index_root: WorkflowWorkspace, target: str) -> Any: ...


class EngineCancelTarget(Protocol):
    def __call__(
        self,
        *,
        target: str,
        config_path: str,
        repo_root: str | None = None,
    ) -> dict[str, Any]: ...


class SafeNameFn(Protocol):
    def __call__(self, value: str, *, fallback: str) -> str: ...


class CrestDownstreamInputSelector(Protocol):
    def __call__(self, contract: Any, *, policy: Any | None = None) -> tuple[Any, ...]: ...


class XtbDownstreamInputSelector(Protocol):
    def __call__(
        self,
        contract: Any,
        *,
        policy: Any | None = None,
        require_geometry: bool = False,
    ) -> tuple[Any, ...]: ...


class EndpointPairSelector(Protocol):
    def __call__(
        self,
        reactant_inputs: tuple[Any, ...] | list[Any],
        product_inputs: tuple[Any, ...] | list[Any],
        *,
        policy: Any | None = None,
    ) -> tuple[Any, ...]: ...


class EngineRuntimePathsLoader(Protocol):
    def __call__(self, config_path: str, *, engine: str | None = None) -> dict[str, Path]: ...


class InternalEngineJobSubmitter(Protocol):
    def __call__(
        self,
        *,
        job_dir: str,
        priority: int,
        config_path: str,
    ) -> dict[str, Any]: ...


class OrcaReactionSubmitter(Protocol):
    def __call__(
        self,
        *,
        reaction_dir: str,
        priority: int,
        config_path: str,
        max_cores: int | None = None,
        max_memory_gb: int | None = None,
        force: bool = False,
        repo_root: str | None = None,
    ) -> dict[str, Any]: ...


class NewXtbStageBuilder(Protocol):
    def __call__(
        self,
        *,
        workflow_id: str,
        stage_id: str,
        reaction_key: str,
        reactant_input: dict[str, Any],
        product_input: dict[str, Any],
        priority: int,
        max_cores: int,
        max_memory_gb: int,
        max_handoff_retries: int = 2,
        manifest_overrides: dict[str, Any] | None = None,
    ) -> Any: ...


class AppendUniqueArtifact(Protocol):
    def __call__(
        self,
        rows: list[dict[str, Any]],
        *,
        kind: str,
        path: str,
        selected: bool = False,
        metadata: dict[str, Any] | None = None,
        deps: Any | None = None,
    ) -> None: ...


class CompletedCrestRoles(Protocol):
    def __call__(self, payload: WorkflowPayload) -> dict[str, dict[str, Any]]: ...


class CompletedCrestStage(Protocol):
    def __call__(self, stage: WorkflowStagePayload, *, crest_config: str | None) -> Any | None: ...


class EnsureEngineJobDir(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        crest_allowed_root: Path | None = None,
        xtb_allowed_root: Path | None = None,
        workflow_id: str,
    ) -> str: ...


class SyncCrestStage(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        crest_config: str | None,
        submit_ready: bool,
        workflow_id: str,
        workspace_dir: Path,
    ) -> None: ...


class SyncOrcaStage(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        orca_config: str | None,
        orca_repo_root: str | None,
        submit_ready: bool,
    ) -> None: ...


class SyncXtbStage(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        xtb_config: str | None,
        submit_ready: bool,
        workflow_id: str,
        workspace_dir: Path,
    ) -> None: ...


class WriteXtbPathJob(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        xtb_allowed_root: Path,
        workflow_id: str,
        attempt_number: int,
    ) -> str: ...


class XtbAttemptRecord(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        attempt_number: int,
    ) -> dict[str, Any]: ...


class XtbStageIntResolver(Protocol):
    def __call__(self, stage: WorkflowStagePayload) -> int: ...


class XtbHandoffStatus(Protocol):
    def __call__(self, contract: Any) -> dict[str, str]: ...


class XtbRetryRecipe(Protocol):
    def __call__(self, attempt_number: int) -> dict[str, Any]: ...


class ConfigRootLoader(Protocol):
    def __call__(
        self,
        config_path: str | None,
        *,
        engine: str = "orca",
    ) -> Path | None: ...


class ReactionContractErrorMapper(Protocol):
    def __call__(self, contract: Any) -> dict[str, str]: ...


class ReactionOrcaCandidatePathResolver(Protocol):
    def __call__(self, stage: WorkflowStagePayload) -> str: ...


class SafeIntFn(Protocol):
    def __call__(self, value: Any, *, default: int = 0) -> int: ...


class SubmissionTargetResolver(Protocol):
    def __call__(self, stage: WorkflowStagePayload) -> str: ...


class TaskPayloadResolver(Protocol):
    def __call__(self, task: WorkflowStagePayload) -> dict[str, Any]: ...


class WorkflowPhaseNotifier(Protocol):
    def __call__(
        self,
        payload: WorkflowPayload,
        *,
        config_path: str | None,
        phase_engine: str,
        extra_lines: list[str] | None = None,
    ) -> bool: ...


class WorkflowProgressPersister(Protocol):
    def __call__(
        self,
        workflow_root: Path,
        workspace_dir: Path,
        payload: WorkflowPayload,
        *,
        sync_only: bool,
    ) -> None: ...


class WorkflowCancellationHandler(Protocol):
    def __call__(
        self,
        payload: WorkflowPayload,
        *,
        config: Any,
    ) -> dict[str, list[dict[str, Any]]]: ...


class StageCancellationHandler(Protocol):
    def __call__(
        self,
        stage: WorkflowStagePayload,
        *,
        config: Any,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OrchestrationContractDeps:
    CrestDownstreamPolicy: type[Any]
    EndpointPairingPolicy: type[Any]
    WorkflowStageInput: type[Any]
    XtbDownstreamPolicy: type[Any]


@dataclass(frozen=True)
class OrchestrationPersistenceDeps:
    acquire_workflow_lock: WorkflowLockFactory
    load_workflow_payload: WorkflowPayloadLoader
    now_utc_iso: Callable[[], str]
    resolve_workflow_workspace: WorkflowWorkspaceResolver
    sync_workflow_registry: WorkflowRegistrySyncer
    write_workflow_payload: WorkflowPayloadWriter


@dataclass(frozen=True)
class OrchestrationEngineDeps:
    build_materialized_orca_stage: MaterializedOrcaStageBuilder
    choose_orca_geometry_frame: OrcaGeometryFrameChooser
    crest_cancel_target: EngineCancelTarget
    load_crest_artifact_contract: CrestArtifactContractLoader
    load_orca_artifact_contract: OrcaArtifactContractLoader
    load_xtb_artifact_contract: XtbArtifactContractLoader
    orca_cancel_target: EngineCancelTarget
    safe_name: SafeNameFn
    select_crest_downstream_inputs: CrestDownstreamInputSelector
    select_endpoint_pairs: EndpointPairSelector
    select_xtb_downstream_inputs: XtbDownstreamInputSelector
    engine_runtime_paths: EngineRuntimePathsLoader
    submit_crest_job_dir: InternalEngineJobSubmitter
    submit_reaction_dir: OrcaReactionSubmitter
    submit_xtb_job_dir: InternalEngineJobSubmitter
    xtb_cancel_target: EngineCancelTarget


@dataclass(frozen=True)
class OrchestrationStageBuilderDeps:
    _new_xtb_stage: NewXtbStageBuilder


@dataclass(frozen=True)
class OrchestrationStageMaterializationDeps:
    _append_crest_orca_stages: StageMaterializer
    _append_reaction_orca_stages: StageMaterializer
    _append_reaction_xtb_stages: StageMaterializer


@dataclass(frozen=True)
class OrchestrationStageRuntimeDeps:
    _append_unique_artifact: AppendUniqueArtifact
    _completed_crest_roles: CompletedCrestRoles
    _completed_crest_stage: CompletedCrestStage
    _ensure_crest_job_dir: EnsureEngineJobDir
    _ensure_xtb_job_dir: EnsureEngineJobDir
    _sync_crest_stage: SyncCrestStage
    _sync_orca_stage: SyncOrcaStage
    _sync_xtb_stage: SyncXtbStage
    _write_xtb_path_job: WriteXtbPathJob
    _xtb_attempt_record: XtbAttemptRecord
    _xtb_current_attempt_number: XtbStageIntResolver
    _xtb_handoff_status: XtbHandoffStatus
    _xtb_path_retry_limit: XtbStageIntResolver
    _xtb_retry_recipe: XtbRetryRecipe


@dataclass(frozen=True)
class OrchestrationStageSupportDeps:
    _clear_reaction_xtb_handoff_error_if_recovering: Callable[[WorkflowPayload], None]
    _coerce_mapping: MappingCoercer
    _load_config_organized_root: ConfigRootLoader
    _load_config_root: ConfigRootLoader
    _normalize_text: TextNormalizer
    _reaction_orca_source_candidate_path: ReactionOrcaCandidatePathResolver
    _reaction_ts_guess_error: ReactionContractErrorMapper
    _safe_int: SafeIntFn
    _stage_metadata: StageMetadataResolver
    _submission_target: SubmissionTargetResolver
    _task_payload_dict: TaskPayloadResolver


@dataclass(frozen=True)
class OrchestrationStageWorkflowDeps:
    _maybe_notify_workflow_phase_summary: WorkflowPhaseNotifier
    _persist_workflow_progress: WorkflowProgressPersister
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
    _cancel_active_workflow_stages: WorkflowCancellationHandler
    _cancel_stage_activity: StageCancellationHandler


@dataclass(frozen=True)
class OrchestrationDeps:
    contracts: OrchestrationContractDeps
    persistence: OrchestrationPersistenceDeps
    engines: OrchestrationEngineDeps
    stages: OrchestrationStageDeps
    advance: OrchestrationAdvanceDeps


__all__ = [
    "AnyCallable",
    "AppendUniqueArtifact",
    "CompletedCrestRoles",
    "CompletedCrestStage",
    "ConfigRootLoader",
    "CrestArtifactContractLoader",
    "CrestDownstreamInputSelector",
    "EndpointPairSelector",
    "EngineCancelTarget",
    "EngineRuntimePathsLoader",
    "EnsureEngineJobDir",
    "InternalEngineJobSubmitter",
    "MappingCoercer",
    "MaterializedOrcaStageBuilder",
    "NewXtbStageBuilder",
    "OrcaArtifactContractLoader",
    "OrcaGeometryFrameChooser",
    "OrcaReactionSubmitter",
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
    "ReactionContractErrorMapper",
    "ReactionOrcaCandidatePathResolver",
    "SafeIntFn",
    "SafeNameFn",
    "StageCancellationHandler",
    "StageMaterializer",
    "StageMetadataResolver",
    "StagePredicate",
    "SubmissionTargetResolver",
    "SyncCrestStage",
    "SyncOrcaStage",
    "SyncXtbStage",
    "TaskPayloadResolver",
    "TextNormalizer",
    "WorkflowCancellationHandler",
    "WorkflowLockFactory",
    "WorkflowPayload",
    "WorkflowPayloadLoader",
    "WorkflowPayloadWriter",
    "WorkflowPhaseNotifier",
    "WorkflowPredicate",
    "WorkflowProgressPersister",
    "WorkflowRegistrySyncer",
    "WorkflowStagePayload",
    "WorkflowStatusComputer",
    "WorkflowWorkspace",
    "WorkflowWorkspaceResolver",
    "WriteXtbPathJob",
    "XtbArtifactContractLoader",
    "XtbAttemptRecord",
    "XtbDownstreamInputSelector",
    "XtbHandoffStatus",
    "XtbRetryRecipe",
    "XtbStageIntResolver",
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
