from __future__ import annotations

from functools import partial
from pathlib import Path

from orca_auto.core.indexing import resolve_job_location
from orca_auto.core.utils.coercion import coerce_int_mapping

from ..contracts.crest import CrestArtifactContract, CrestDownstreamPolicy, to_workflow_stage_inputs
from ..contracts.xtb import WorkflowStageInput
from . import _engine_adapter_helpers as _adapter_helpers

_ACTIVE_PAYLOAD_STATUSES = frozenset(
    {"queued", "running", "submitted", "cancel_requested", "retrying"}
)


def load_crest_artifact_contract(
    *, crest_index_root: str | Path, target: str
) -> CrestArtifactContract:
    bundle = _adapter_helpers.load_contract_artifact_bundle(
        index_root=crest_index_root,
        target=target,
        resolve_job_location_fn=resolve_job_location,
        load_json_dict_fn=_adapter_helpers.load_json_dict,
        report_filename="job_report.json",
        state_filename="job_state.json",
        missing_label="CREST",
        expected_app_name="orca_auto_crest",
        coerce_resource_dict_fn=coerce_int_mapping,
        select_payload_fn=partial(
            _adapter_helpers.select_active_artifact_payload,
            active_statuses=_ACTIVE_PAYLOAD_STATUSES,
        ),
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
    selected_input_xyz = fields.resolved_path(selected_input_xyz, roots=artifact_roots)
    retained_paths = fields.resolved_paths(retained_paths, roots=artifact_roots)
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
