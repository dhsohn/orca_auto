from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.orchestration.deps import (
    orchestration_context as _orchestration_context,
)
from orca_auto.flow.orchestration.stage_views import (
    _engine_stage_views,
    _engine_stages,
    _request_params,
)
from orca_auto.flow.state import workflow_workspace_internal_engine_paths

_CONFORMER_ORCA_STAGE_DIRNAME = "02_orca"


@dataclass(frozen=True)
class _CrestOrcaStagePlan:
    params: dict[str, Any]
    candidates: tuple[Any, ...]
    orca_allowed_root: Path


def _completed_crest_stage_for_orca(
    o: Any,
    payload: dict[str, Any],
    *,
    crest_config: str | None,
) -> Any | None:
    crest_stage = next(
        (
            view.raw
            for view in _engine_stage_views(o, payload, "crest")
            if view.status(o) == "completed"
        ),
        None,
    )
    if crest_stage is None:
        return None
    return o.stages.runtime._completed_crest_stage(crest_stage, crest_config=crest_config)


def _crest_orca_stage_plan(
    o: Any,
    payload: dict[str, Any],
    *,
    crest_config: str | None,
    orca_config: str | None,
) -> _CrestOrcaStagePlan | None:
    if _engine_stages(o, payload, "orca"):
        return None
    crest_contract = _completed_crest_stage_for_orca(
        o,
        payload,
        crest_config=crest_config,
    )
    if (
        crest_contract is None
        or o.stages.support._load_config_root(orca_config, engine="orca") is None
    ):
        return None
    payload_metadata = o.stages.support._coerce_mapping(payload.get("metadata"))
    workspace_dir_text = o.stages.support._normalize_text(payload_metadata.get("workspace_dir"))
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
    params = _request_params(o, payload)
    candidates = o.engines.select_crest_downstream_inputs(
        crest_contract,
        policy=o.contracts.CrestDownstreamPolicy.build(
            max_candidates=int(params.get("max_orca_stages", 3) or 3)
        ),
    )
    return _CrestOrcaStagePlan(
        params=params,
        candidates=candidates,
        orca_allowed_root=orca_runtime_paths["allowed_root"],
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
    deps: OrchestrationDeps | None = None,
) -> bool:
    o = _orchestration_context(deps)
    plan = _crest_orca_stage_plan(
        o,
        payload,
        crest_config=crest_config,
        orca_config=orca_config,
    )
    if plan is None:
        return False
    created = 0
    for candidate in plan.candidates:
        created += 1
        stage = o.engines.build_materialized_orca_stage(
            workflow_id=str(payload.get("workflow_id", "")),
            template_name=template_name,
            stage_id=f"{stage_id_prefix}_{created:02d}",
            stage_key=f"{created:02d}_{o.engines.safe_name(candidate.kind, fallback='conformer')}",
            stage_root_name="",
            workspace_dir=plan.orca_allowed_root,
            input_artifact_kind="crest_conformer",
            candidate=candidate,
            task_kind="opt",
            route_line=str(plan.params.get("orca_route_line", "! r2scan-3c Opt TightSCF")),
            charge=int(plan.params.get("charge", 0) or 0),
            multiplicity=int(plan.params.get("multiplicity", 1) or 1),
            max_cores=int(plan.params.get("max_cores", 8) or 8),
            max_memory_gb=int(plan.params.get("max_memory_gb", 32) or 32),
            priority=int(plan.params.get("priority", 10) or 10),
            xyz_filename=xyz_filename,
            inp_filename=inp_filename,
        ).to_dict()
        payload.setdefault("stages", []).append(stage)
    return created > 0
