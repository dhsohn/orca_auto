from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.indexing import resolve_job_location
from orca_auto.core.utils.coercion import coerce_int_mapping

from ..contracts.crest import CrestArtifactContract, CrestDownstreamPolicy, to_workflow_stage_inputs
from ..contracts.xtb import WorkflowStageInput
from . import _engine_adapter_helpers as _adapter_helpers


def load_crest_artifact_contract(
    *, crest_index_root: str | Path, target: str
) -> CrestArtifactContract:
    bundle = _adapter_helpers.load_contract_artifact_bundle(
        index_root=crest_index_root,
        target=target,
        resolve_job_location_fn=resolve_job_location,
        load_json_dict_fn=_adapter_helpers.load_json_dict,
        state_filename="job_state.json",
        missing_label="CREST",
        expected_engine="crest",
        expected_app_name="orca_auto_crest",
        coerce_resource_dict_fn=coerce_int_mapping,
    )
    fields = _adapter_helpers.ContractFieldReader(bundle)
    payload = fields.payload

    retained_paths = fields.payload_sequence("retained_conformer_paths")
    retained_count = int(
        payload.get("retained_conformer_count", len(retained_paths)) or len(retained_paths)
    )
    status = fields.payload_record_text("status", "status", default="unknown")
    reason = _adapter_helpers.normalize_text(payload.get("reason"))
    job_id = fields.payload_record_text("job_id", "job_id")
    mode = fields.payload_record_text("mode", "job_type", default="standard")
    molecule_key = fields.payload_record_text("molecule_key", "molecule_key")
    selected_input_xyz = fields.payload_record_text("selected_input_xyz", "selected_input_xyz")
    artifact_roots = fields.artifact_roots()
    if selected_input_xyz:
        selected_input_xyz = fields.resolved_path(selected_input_xyz, roots=artifact_roots)
        if not selected_input_xyz:
            raise ValueError("CREST selected input artifact escapes job_dir")
    raw_retained_paths = retained_paths
    retained_paths = fields.resolved_paths(raw_retained_paths, roots=artifact_roots)
    if len(retained_paths) != len(raw_retained_paths):
        raise ValueError("CREST retained artifact escapes job_dir")
    raw_output_identities = payload.get("output_identities")
    output_identities: dict[str, dict[str, Any]] = {}
    for raw_path, current_path in zip(raw_retained_paths, retained_paths, strict=True):
        identity = (
            raw_output_identities.get(raw_path) if isinstance(raw_output_identities, dict) else None
        )
        if identity is not None:
            if not isinstance(identity, dict):
                raise ValueError("CREST output identity must be a mapping")
            verified = _engine_runner.verify_confined_output_identity(
                fields.job_dir,
                identity,
                path_override=current_path,
            )
            output_identities[str(verified)] = {**identity, "path": str(verified)}
    if status == "completed":
        for path in retained_paths:
            if path not in output_identities:
                raise ValueError(
                    f"completed CREST retained artifact is missing its output identity: {path}"
                )
    latest_known_path = bundle.latest_known_path

    return CrestArtifactContract(
        job_id=job_id,
        mode=mode,
        status=status,
        reason=reason,
        job_dir=str(fields.job_dir),
        latest_known_path=latest_known_path,
        molecule_key=molecule_key,
        selected_input_xyz=selected_input_xyz,
        retained_conformer_count=retained_count,
        retained_conformer_paths=retained_paths,
        output_identities=output_identities,
        resource_request=bundle.resource_request,
        resource_actual=bundle.resource_actual,
    )


def select_crest_downstream_inputs(
    contract: CrestArtifactContract,
    *,
    policy: CrestDownstreamPolicy | None = None,
) -> tuple[WorkflowStageInput, ...]:
    return to_workflow_stage_inputs(contract, policy=policy)


__all__ = [
    "load_crest_artifact_contract",
    "select_crest_downstream_inputs",
]
