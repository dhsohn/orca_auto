from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.utils import mapping_or_empty, normalize_text
from orca_auto.flow.contracts import CrestDownstreamPolicy
from orca_auto.flow.contracts.workflow import workflow_request_parameters
from orca_auto.flow.orchestration.charge_spin import strict_int
from orca_auto.flow.orchestration.scan_orca_materialization import (
    _all_terminal_none_verified,
    _record_workflow_error,
)
from orca_auto.flow.orchestration.services import (
    OrchestrationServices,
    resolve_orchestration_services,
)
from orca_auto.flow.orchestration.stage_runtime.crest import completed_crest_stage_impl
from orca_auto.flow.orchestration.stage_views import (
    _engine_stage_views,
    _engine_stages,
)
from orca_auto.flow.orchestration.support import load_config_root_impl, required_stage_budget
from orca_auto.flow.state import workflow_workspace_internal_engine_paths

_CONFORMER_ORCA_STAGE_DIRNAME = "03_orca"


@dataclass(frozen=True)
class _CrestOrcaStagePlan:
    params: dict[str, Any]
    candidates: tuple[Any, ...]
    orca_allowed_root: Path


def _completed_crest_stage_for_orca(
    services: OrchestrationServices,
    payload: dict[str, Any],
    *,
    crest_config: str | None,
) -> Any | None:
    crest_stage = next(
        (
            view.raw
            for view in _engine_stage_views(payload, "crest")
            if view.status() == "completed"
        ),
        None,
    )
    if crest_stage is None:
        return None
    return completed_crest_stage_impl(
        crest_stage,
        crest_config=crest_config,
        services=services,
    )


def _crest_orca_stage_plan(
    services: OrchestrationServices,
    payload: dict[str, Any],
    *,
    crest_config: str | None,
    orca_config: str | None,
) -> _CrestOrcaStagePlan | None:
    if _engine_stages(payload, "orca"):
        return None
    has_completed_crest_stage = any(
        view.status() == "completed" for view in _engine_stage_views(payload, "crest")
    )
    crest_contract = _completed_crest_stage_for_orca(
        services,
        payload,
        crest_config=crest_config,
    )
    if crest_contract is None:
        if has_completed_crest_stage:
            _record_conformer_crest_handoff_failure(payload)
        return None
    if load_config_root_impl(orca_config, engine="orca", services=services) is None:
        return None
    payload_metadata = mapping_or_empty(payload.get("metadata"))
    workspace_dir_text = normalize_text(payload_metadata.get("workspace_dir"))
    workspace_dir = (
        Path(workspace_dir_text).expanduser().resolve()
        if workspace_dir_text
        else Path(".").resolve()
    )
    orca_runtime_paths = workflow_workspace_internal_engine_paths(
        workspace_dir,
        engine="orca",
        stage_dirname=_CONFORMER_ORCA_STAGE_DIRNAME,
    )
    params = workflow_request_parameters(payload)
    candidates = services.engines.select_crest_downstream_inputs(
        crest_contract,
        policy=CrestDownstreamPolicy.build(
            max_candidates=strict_int(
                required_stage_budget(params, "max_orca_stages"),
                field="max_orca_stages",
                minimum=1,
            )
        ),
    )
    return _CrestOrcaStagePlan(
        params=params,
        candidates=candidates,
        orca_allowed_root=orca_runtime_paths["allowed_root"],
    )


def _maybe_record_conformer_orca_exhausted(payload: dict[str, Any]) -> None:
    """Fail the workflow when every conformer ORCA optimization stage failed.

    conformer_screening materializes ORCA ``opt`` stages from the completed CREST
    stage in a single pass. If every one reaches a non-verifying terminal state,
    the run produced no optimized conformer; but a failed ORCA stage is engine-role
    non-fatal (conformer stages never set ``workflow_fatal``), so recompute reports
    the workflow COMPLETED. Record a failed workflow_error instead, mirroring the
    reaction/scan candidate-exhaustion guards. The shared ``_all_terminal_none_verified``
    returns False while any conformer is still running or on an in-progress
    cancellation, and when at least one conformer completed (partial success stays
    COMPLETED).
    """
    orca_stages = _engine_stages(payload, "orca")
    if not _all_terminal_none_verified(orca_stages):
        return
    stage_id = str(orca_stages[0].get("stage_id") or "")
    _record_workflow_error(
        payload,
        scope="conformer_screening_orca_conformers_exhausted",
        stage_id=stage_id,
        reason="conformers_failed",
        message="All conformer optimization stages failed; no optimized conformer was produced.",
    )


def _record_conformer_crest_handoff_failure(payload: dict[str, Any]) -> None:
    _record_workflow_error(
        payload,
        scope="conformer_screening_crest_handoff",
        stage_id="",
        reason="crest_no_usable_conformers",
        message="The completed CREST stage has no usable retained conformer geometry.",
    )


def append_crest_orca_stages_impl(
    payload: dict[str, Any],
    *,
    template_name: str,
    crest_config: str | None,
    orca_config: str | None,
    stage_id_prefix: str,
    xyz_filename: str,
    inp_filename: str,
    services: OrchestrationServices | None = None,
) -> bool:
    resolved = resolve_orchestration_services(services)
    plan = _crest_orca_stage_plan(
        resolved,
        payload,
        crest_config=crest_config,
        orca_config=orca_config,
    )
    if plan is None:
        _maybe_record_conformer_orca_exhausted(payload)
        return False
    if not plan.candidates:
        _record_conformer_crest_handoff_failure(payload)
        return False
    created = 0
    for candidate in plan.candidates:
        created += 1
        stage = resolved.engines.build_materialized_orca_stage(
            workflow_id=str(payload.get("workflow_id", "")),
            template_name=template_name,
            stage_id=f"{stage_id_prefix}_{created:02d}",
            stage_key=(
                f"{created:02d}_{resolved.engines.safe_name(candidate.kind, fallback='conformer')}"
            ),
            stage_root_name="",
            workspace_dir=plan.orca_allowed_root,
            input_artifact_kind="crest_conformer",
            candidate=candidate,
            task_kind="opt",
            route_line=plan.params.get("orca_route_line", "! r2scan-3c Opt TightSCF"),
            charge=strict_int(plan.params.get("charge", 0), field="charge"),
            multiplicity=strict_int(
                plan.params.get("multiplicity", 1), field="multiplicity", minimum=1
            ),
            max_cores=int(plan.params.get("max_cores", 8) or 8),
            max_memory_gb=int(plan.params.get("max_memory_gb", 32) or 32),
            priority=normalize_queue_priority(plan.params.get("priority")),
            xyz_filename=xyz_filename,
            inp_filename=inp_filename,
        ).to_dict()
        payload.setdefault("stages", []).append(stage)
    return created > 0
