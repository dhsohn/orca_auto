from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from functools import partial, wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .dep_types import (
    _ORCHESTRATION_STAGE_BUILDER_GROUP,
    _ORCHESTRATION_STAGE_MATERIALIZATION_GROUP,
    _ORCHESTRATION_STAGE_RUNTIME_GROUP,
    _ORCHESTRATION_STAGE_SUPPORT_GROUP,
    _ORCHESTRATION_STAGE_WORKFLOW_GROUP,
    OrchestrationAdvanceDeps,
    OrchestrationContractDeps,
    OrchestrationEngineDeps,
    OrchestrationPersistenceDeps,
    OrchestrationStageDeps,
)

if TYPE_CHECKING:
    from orca_auto.flow.orchestration.dep_types import (
        OrchestrationDeps,
        _OrchestrationStageDepGroup,
    )

AnyCallable = Callable[..., Any]
StageDepDefaultFactory = Callable[
    [Mapping[str, Any] | None, "_LazyOrchestrationDeps"], Mapping[str, Any]
]


# Generic override/default dependency wiring machinery.


class _LazyOrchestrationDeps:
    def __init__(
        self,
        overrides: Mapping[str, Any] | None,
        *,
        factory: Callable[[Mapping[str, Any] | None], OrchestrationDeps] | None = None,
    ) -> None:
        self._overrides = overrides
        self._factory = factory
        self._deps: OrchestrationDeps | None = None

    def resolve_to(self, deps: OrchestrationDeps) -> None:
        self._deps = deps

    def get(self) -> OrchestrationDeps:
        if self._deps is None:
            if self._factory is None:
                from importlib import import_module

                self._factory = import_module(
                    "orca_auto.flow.orchestration.deps"
                ).orchestration_deps
            self._deps = self._factory(self._overrides)
        return self._deps


def _override(overrides: Mapping[str, Any] | None, name: str, default: Any) -> Any:
    if overrides is not None and name in overrides:
        return overrides[name]
    return default


@dataclass(frozen=True)
class _StageDepDefaultGroup:
    dep_group: _OrchestrationStageDepGroup
    defaults: Mapping[str, Any]

    def build(self, overrides: Mapping[str, Any] | None) -> Any:
        return _build_dep_dataclass(
            self.dep_group.deps_type,
            overrides,
            self.defaults,
            label=f"stage dependency group {self.dep_group.name!r}",
        )


@dataclass(frozen=True)
class _StageDepDefaultSpec:
    dep_group: _OrchestrationStageDepGroup
    default_factory: StageDepDefaultFactory

    def build(
        self, overrides: Mapping[str, Any] | None, deps_provider: _LazyOrchestrationDeps
    ) -> _StageDepDefaultGroup:
        return _StageDepDefaultGroup(
            self.dep_group,
            self.default_factory(overrides, deps_provider),
        )


@dataclass(frozen=True)
class _StageDepDefaultRegistry:
    specs: tuple[_StageDepDefaultSpec, ...]

    def build_groups(
        self,
        overrides: Mapping[str, Any] | None,
        deps_provider: _LazyOrchestrationDeps,
    ) -> tuple[_StageDepDefaultGroup, ...]:
        return tuple(spec.build(overrides, deps_provider) for spec in self.specs)

    def build_deps(
        self,
        deps_type: type[Any],
        overrides: Mapping[str, Any] | None,
        deps_provider: _LazyOrchestrationDeps,
    ) -> Any:
        return deps_type(
            **{
                group.dep_group.name: group.build(overrides)
                for group in self.build_groups(overrides, deps_provider)
            }
        )


def _apply_overrides(
    overrides: Mapping[str, Any] | None,
    items: Mapping[str, Any],
) -> dict[str, Any]:
    return {name: _override(overrides, name, default) for name, default in items.items()}


def _validate_dep_defaults(
    deps_type: type[Any],
    defaults: Mapping[str, Any],
    *,
    label: str | None = None,
) -> None:
    expected = tuple(field.name for field in fields(deps_type))
    expected_names = set(expected)
    default_names = tuple(defaults)
    actual_names = set(default_names)
    if actual_names == expected_names:
        return

    missing = tuple(name for name in expected if name not in actual_names)
    unexpected = tuple(name for name in default_names if name not in expected_names)
    deps_label = label or deps_type.__name__
    raise ValueError(
        f"{deps_label} default mismatch: missing={missing!r} unexpected={unexpected!r}"
    )


def _build_dep_dataclass(
    deps_type: type[Any],
    overrides: Mapping[str, Any] | None,
    defaults: Mapping[str, Any],
    *,
    label: str | None = None,
) -> Any:
    _validate_dep_defaults(deps_type, defaults, label=label)
    return deps_type(**_apply_overrides(overrides, defaults))


def _deps_provider(
    overrides: Mapping[str, Any] | None,
    deps_provider: _LazyOrchestrationDeps | None,
) -> _LazyOrchestrationDeps:
    return deps_provider or _LazyOrchestrationDeps(overrides)


def _bind_with_deps(deps_provider: _LazyOrchestrationDeps, func: AnyCallable) -> AnyCallable:
    @wraps(func)
    def call(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("deps") is None:
            kwargs["deps"] = deps_provider.get()
        return func(*args, **kwargs)

    return call


def _bind_many_with_deps(
    deps_provider: _LazyOrchestrationDeps,
    items: Mapping[str, AnyCallable],
) -> dict[str, AnyCallable]:
    return {name: _bind_with_deps(deps_provider, default) for name, default in items.items()}


# Workflow-level default implementations (thread overrides through nested helpers).


def _coerce_mapping_default(value: Any) -> dict[str, Any]:
    from orca_auto.core.utils import mapping_or_empty

    return mapping_or_empty(value)


def _normalize_text_default(value: Any) -> str:
    from orca_auto.core.utils import normalize_text

    return normalize_text(value)


def _safe_int_default(value: Any, *, default: int = 0) -> int:
    from orca_auto.core.utils import safe_int

    return safe_int(value, default=default)


def _normalize_text_override(overrides: Mapping[str, Any] | None = None) -> Any:
    return _override(overrides, "_normalize_text", _normalize_text_default)


def _stage_metadata_override(overrides: Mapping[str, Any] | None = None) -> Any:
    from orca_auto.flow.orchestration.support import stage_metadata_impl

    return _override(overrides, "_stage_metadata", stage_metadata_impl)


def _stage_failure_is_recoverable_override(
    overrides: Mapping[str, Any] | None = None,
) -> Any:
    override = _override(overrides, "_stage_failure_is_recoverable", None)
    if override is not None:
        return override

    def stage_failure_is_recoverable(stage: dict[str, Any]) -> bool:
        return _stage_failure_is_recoverable_default(stage, overrides=overrides)

    return stage_failure_is_recoverable


def _workflow_sync_only_default(
    payload: dict[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> bool:
    from orca_auto.flow.orchestration.lifecycle import workflow_sync_only_impl

    return workflow_sync_only_impl(
        payload,
        normalize_text_fn=_normalize_text_override(overrides),
    )


def _workflow_has_active_children_default(
    payload: dict[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> bool:
    from orca_auto.flow.orchestration.lifecycle import workflow_has_active_children_impl
    from orca_auto.flow.state import workflow_has_active_downstream

    return workflow_has_active_children_impl(
        payload,
        normalize_text_fn=_normalize_text_override(overrides),
        workflow_has_active_downstream_fn=workflow_has_active_downstream,
    )


def _stage_failure_is_recoverable_default(
    stage: dict[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> bool:
    from orca_auto.flow.orchestration.lifecycle import stage_failure_is_recoverable_impl

    return stage_failure_is_recoverable_impl(
        stage,
        normalize_text_fn=_normalize_text_override(overrides),
        stage_metadata_fn=_stage_metadata_override(overrides),
    )


def _recompute_workflow_status_default(
    payload: dict[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> str:
    from orca_auto.flow.orchestration.lifecycle import (
        effective_stage_status_impl,
        recompute_workflow_status_impl,
    )

    def effective_stage_status(stage: dict[str, Any]) -> str:
        return effective_stage_status_impl(
            stage,
            normalize_text_fn=_normalize_text_override(overrides),
            stage_failure_is_recoverable_fn=_stage_failure_is_recoverable_override(overrides),
        )

    return recompute_workflow_status_impl(
        payload,
        normalize_text_fn=_normalize_text_override(overrides),
        effective_stage_status_fn=effective_stage_status,
    )


def _persist_workflow_progress_default(
    workflow_root: Path,
    workspace_dir: Path,
    payload: dict[str, Any],
    *,
    sync_only: bool,
    overrides: Mapping[str, Any] | None = None,
) -> None:
    from orca_auto.flow.registry import sync_workflow_registry
    from orca_auto.flow.state import write_workflow_payload

    normalize = _normalize_text_override(overrides)
    if not sync_only:
        status = normalize(payload.get("status")).lower()
        if status not in {
            "completed",
            "failed",
            "cancel_requested",
            "cancelled",
            "cancel_failed",
        }:
            payload["status"] = "running"
    _override(overrides, "write_workflow_payload", write_workflow_payload)(workspace_dir, payload)
    _override(overrides, "sync_workflow_registry", sync_workflow_registry)(
        workflow_root,
        workspace_dir,
        payload,
    )


def _maybe_notify_workflow_phase_summary_default(
    payload: dict[str, Any],
    *,
    config_path: str | None,
    phase_engine: str,
    extra_lines: list[str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> bool:
    from orca_auto.flow.workflow.notifications import maybe_notify_workflow_phase_summary

    return maybe_notify_workflow_phase_summary(
        payload=payload,
        config_path=config_path,
        phase_engine=phase_engine,
        stage_failure_is_recoverable_fn=_stage_failure_is_recoverable_override(overrides),
        extra_lines=extra_lines,
    )


# Per-group stage dependency defaults. All five share the registry dispatch
# signature (overrides, deps_provider) and del what they do not need.


def _stage_builder_defaults(
    overrides: Mapping[str, Any] | None,
    deps_provider: _LazyOrchestrationDeps,
) -> dict[str, Any]:
    del overrides, deps_provider
    from orca_auto.flow.orchestration.stage_builders import new_xtb_stage_impl

    return {
        "_new_xtb_stage": new_xtb_stage_impl,
    }


def _stage_materialization_defaults(
    overrides: Mapping[str, Any] | None,
    deps_provider: _LazyOrchestrationDeps,
) -> dict[str, Any]:
    del overrides
    from orca_auto.flow.orchestration.materialization import (
        append_crest_orca_stages_impl,
        append_reaction_orca_stages_impl,
        append_reaction_xtb_stages_impl,
        append_scan_optts_stages_impl,
    )

    return _bind_many_with_deps(
        deps_provider,
        {
            "_append_crest_orca_stages": append_crest_orca_stages_impl,
            "_append_reaction_orca_stages": append_reaction_orca_stages_impl,
            "_append_reaction_xtb_stages": append_reaction_xtb_stages_impl,
            "_append_scan_optts_stages": append_scan_optts_stages_impl,
        },
    )


def _stage_runtime_defaults(
    overrides: Mapping[str, Any] | None,
    deps_provider: _LazyOrchestrationDeps,
) -> dict[str, Any]:
    del overrides
    from orca_auto.flow.orchestration.stage_runtime.crest import (
        completed_crest_roles_impl,
        completed_crest_stage_impl,
        ensure_crest_job_dir_impl,
        sync_crest_stage_impl,
    )
    from orca_auto.flow.orchestration.stage_runtime.orca import sync_orca_stage_impl
    from orca_auto.flow.orchestration.stage_runtime.shared import append_unique_artifact_impl
    from orca_auto.flow.orchestration.stage_runtime.xtb_handoff import xtb_handoff_status_impl
    from orca_auto.flow.orchestration.stage_runtime.xtb_path_jobs import (
        ensure_xtb_job_dir_impl,
        write_xtb_path_job_impl,
    )
    from orca_auto.flow.orchestration.stage_runtime.xtb_retry import (
        xtb_attempt_record_impl,
        xtb_current_attempt_number_impl,
        xtb_path_retry_limit_impl,
        xtb_retry_recipe_impl,
    )
    from orca_auto.flow.orchestration.stage_runtime.xtb_sync import sync_xtb_stage_impl

    return {
        **_bind_many_with_deps(
            deps_provider,
            {
                "_append_unique_artifact": append_unique_artifact_impl,
                "_completed_crest_roles": completed_crest_roles_impl,
                "_completed_crest_stage": completed_crest_stage_impl,
                "_ensure_crest_job_dir": ensure_crest_job_dir_impl,
                "_ensure_xtb_job_dir": ensure_xtb_job_dir_impl,
                "_sync_crest_stage": sync_crest_stage_impl,
                "_sync_orca_stage": sync_orca_stage_impl,
                "_sync_xtb_stage": sync_xtb_stage_impl,
                "_write_xtb_path_job": write_xtb_path_job_impl,
                "_xtb_attempt_record": xtb_attempt_record_impl,
                "_xtb_current_attempt_number": xtb_current_attempt_number_impl,
                "_xtb_handoff_status": xtb_handoff_status_impl,
                "_xtb_path_retry_limit": xtb_path_retry_limit_impl,
            },
        ),
        "_xtb_retry_recipe": xtb_retry_recipe_impl,
    }


def _stage_support_defaults(
    overrides: Mapping[str, Any] | None,
    deps_provider: _LazyOrchestrationDeps,
) -> dict[str, Any]:
    del overrides
    from orca_auto.flow.orchestration.support import (
        clear_reaction_xtb_handoff_error_if_recovering_impl,
        load_config_organized_root_impl,
        load_config_root_impl,
        reaction_orca_source_candidate_path_impl,
        reaction_ts_guess_error_impl,
        stage_metadata_impl,
        submission_target_impl,
        task_payload_dict_impl,
    )

    return {
        **_bind_many_with_deps(
            deps_provider,
            {
                "_clear_reaction_xtb_handoff_error_if_recovering": (
                    clear_reaction_xtb_handoff_error_if_recovering_impl
                ),
                "_load_config_organized_root": load_config_organized_root_impl,
                "_load_config_root": load_config_root_impl,
                "_reaction_orca_source_candidate_path": reaction_orca_source_candidate_path_impl,
                "_reaction_ts_guess_error": reaction_ts_guess_error_impl,
                "_submission_target": submission_target_impl,
            },
        ),
        "_coerce_mapping": _coerce_mapping_default,
        "_normalize_text": _normalize_text_default,
        "_safe_int": _safe_int_default,
        "_stage_metadata": stage_metadata_impl,
        "_task_payload_dict": task_payload_dict_impl,
    }


def _stage_workflow_defaults(
    overrides: Mapping[str, Any] | None,
    deps_provider: _LazyOrchestrationDeps,
) -> dict[str, Any]:
    del deps_provider
    return {
        "_maybe_notify_workflow_phase_summary": partial(
            _maybe_notify_workflow_phase_summary_default,
            overrides=overrides,
        ),
        "_persist_workflow_progress": partial(
            _persist_workflow_progress_default,
            overrides=overrides,
        ),
        "_recompute_workflow_status": partial(
            _recompute_workflow_status_default,
            overrides=overrides,
        ),
        "_stage_failure_is_recoverable": _stage_failure_is_recoverable_override(overrides),
        "_workflow_has_active_children": partial(
            _workflow_has_active_children_default,
            overrides=overrides,
        ),
        "_workflow_sync_only": partial(_workflow_sync_only_default, overrides=overrides),
    }


# Top-level OrchestrationDeps section builders.


def _build_contract_deps(overrides: Mapping[str, Any] | None) -> OrchestrationContractDeps:
    from orca_auto.flow.contracts import (
        CrestDownstreamPolicy,
        WorkflowStageInput,
        XtbDownstreamPolicy,
    )
    from orca_auto.flow.endpoint_pairing import EndpointPairingPolicy

    return _build_dep_dataclass(
        OrchestrationContractDeps,
        overrides,
        {
            "CrestDownstreamPolicy": CrestDownstreamPolicy,
            "EndpointPairingPolicy": EndpointPairingPolicy,
            "WorkflowStageInput": WorkflowStageInput,
            "XtbDownstreamPolicy": XtbDownstreamPolicy,
        },
    )


def _build_persistence_deps(
    overrides: Mapping[str, Any] | None,
) -> OrchestrationPersistenceDeps:
    from orca_auto.core.utils import now_utc_iso
    from orca_auto.flow.registry import sync_workflow_registry
    from orca_auto.flow.state import (
        acquire_workflow_lock,
        load_workflow_payload,
        resolve_workflow_workspace,
        write_workflow_payload,
    )

    return _build_dep_dataclass(
        OrchestrationPersistenceDeps,
        overrides,
        {
            "acquire_workflow_lock": acquire_workflow_lock,
            "load_workflow_payload": load_workflow_payload,
            "now_utc_iso": now_utc_iso,
            "resolve_workflow_workspace": resolve_workflow_workspace,
            "sync_workflow_registry": sync_workflow_registry,
            "write_workflow_payload": write_workflow_payload,
        },
    )


def _build_engine_deps(overrides: Mapping[str, Any] | None) -> OrchestrationEngineDeps:
    from orca_auto.flow._orca_stage_materialization import build_materialized_orca_stage, safe_name
    from orca_auto.flow.adapters.crest import (
        load_crest_artifact_contract,
        select_crest_downstream_inputs,
    )
    from orca_auto.flow.adapters.orca import load_orca_artifact_contract
    from orca_auto.flow.adapters.xtb import load_xtb_artifact_contract, select_xtb_downstream_inputs
    from orca_auto.flow.endpoint_pairing import select_endpoint_pairs
    from orca_auto.flow.engine_runtime import engine_runtime_paths
    from orca_auto.flow.submitters.crest import (
        cancel_target as crest_cancel_target,
    )
    from orca_auto.flow.submitters.crest import (
        submit_job_dir as submit_crest_job_dir,
    )
    from orca_auto.flow.submitters.orca import cancel_target as orca_cancel_target
    from orca_auto.flow.submitters.orca import submit_reaction_dir
    from orca_auto.flow.submitters.xtb import (
        cancel_target as xtb_cancel_target,
    )
    from orca_auto.flow.submitters.xtb import (
        submit_job_dir as submit_xtb_job_dir,
    )
    from orca_auto.flow.xyz_utils import choose_orca_geometry_frame

    return _build_dep_dataclass(
        OrchestrationEngineDeps,
        overrides,
        {
            "build_materialized_orca_stage": build_materialized_orca_stage,
            "choose_orca_geometry_frame": choose_orca_geometry_frame,
            "crest_cancel_target": crest_cancel_target,
            "load_crest_artifact_contract": load_crest_artifact_contract,
            "load_orca_artifact_contract": load_orca_artifact_contract,
            "load_xtb_artifact_contract": load_xtb_artifact_contract,
            "orca_cancel_target": orca_cancel_target,
            "safe_name": safe_name,
            "select_crest_downstream_inputs": select_crest_downstream_inputs,
            "select_endpoint_pairs": select_endpoint_pairs,
            "select_xtb_downstream_inputs": select_xtb_downstream_inputs,
            "engine_runtime_paths": engine_runtime_paths,
            "submit_crest_job_dir": submit_crest_job_dir,
            "submit_reaction_dir": submit_reaction_dir,
            "submit_xtb_job_dir": submit_xtb_job_dir,
            "xtb_cancel_target": xtb_cancel_target,
        },
    )


def _stage_dep_default_registry() -> _StageDepDefaultRegistry:
    return _StageDepDefaultRegistry(
        (
            _StageDepDefaultSpec(_ORCHESTRATION_STAGE_BUILDER_GROUP, _stage_builder_defaults),
            _StageDepDefaultSpec(
                _ORCHESTRATION_STAGE_MATERIALIZATION_GROUP,
                _stage_materialization_defaults,
            ),
            _StageDepDefaultSpec(_ORCHESTRATION_STAGE_RUNTIME_GROUP, _stage_runtime_defaults),
            _StageDepDefaultSpec(_ORCHESTRATION_STAGE_SUPPORT_GROUP, _stage_support_defaults),
            _StageDepDefaultSpec(_ORCHESTRATION_STAGE_WORKFLOW_GROUP, _stage_workflow_defaults),
        )
    )


def _build_stage_deps(
    overrides: Mapping[str, Any] | None,
    *,
    deps_provider: _LazyOrchestrationDeps | None = None,
) -> OrchestrationStageDeps:
    provider = _deps_provider(overrides, deps_provider)
    return _stage_dep_default_registry().build_deps(
        OrchestrationStageDeps,
        overrides,
        provider,
    )


def _build_advance_deps(
    overrides: Mapping[str, Any] | None,
    *,
    deps_provider: _LazyOrchestrationDeps | None = None,
) -> OrchestrationAdvanceDeps:
    from orca_auto.flow.orchestration.workflow_cancellation import (
        _cancel_active_workflow_stages,
        _cancel_stage_activity,
    )

    provider = _deps_provider(overrides, deps_provider)
    return _build_dep_dataclass(
        OrchestrationAdvanceDeps,
        overrides,
        {
            "_cancel_active_workflow_stages": _bind_with_deps(
                provider,
                _cancel_active_workflow_stages,
            ),
            "_cancel_stage_activity": _bind_with_deps(
                provider,
                _cancel_stage_activity,
            ),
        },
    )


__all__ = [
    "AnyCallable",
    "StageDepDefaultFactory",
    "_LazyOrchestrationDeps",
    "_StageDepDefaultGroup",
    "_StageDepDefaultRegistry",
    "_StageDepDefaultSpec",
    "_apply_overrides",
    "_bind_many_with_deps",
    "_bind_with_deps",
    "_build_advance_deps",
    "_build_contract_deps",
    "_build_dep_dataclass",
    "_build_engine_deps",
    "_build_persistence_deps",
    "_build_stage_deps",
    "_coerce_mapping_default",
    "_deps_provider",
    "_maybe_notify_workflow_phase_summary_default",
    "_normalize_text_default",
    "_normalize_text_override",
    "_override",
    "_persist_workflow_progress_default",
    "_recompute_workflow_status_default",
    "_safe_int_default",
    "_stage_builder_defaults",
    "_stage_dep_default_registry",
    "_stage_failure_is_recoverable_default",
    "_stage_failure_is_recoverable_override",
    "_stage_materialization_defaults",
    "_stage_metadata_override",
    "_stage_runtime_defaults",
    "_stage_support_defaults",
    "_stage_workflow_defaults",
    "_validate_dep_defaults",
    "_workflow_has_active_children_default",
    "_workflow_sync_only_default",
]
