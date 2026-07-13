from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.indexing import JobLocationRecord, resolve_job_location
from orca_auto.core.utils.coercion import coerce_int_mapping

from ..contracts.xtb import (
    WorkflowStageInput,
    XtbArtifactContract,
    XtbCandidateArtifact,
    XtbDownstreamPolicy,
)
from ..xyz_utils import has_xyz_geometry
from . import _engine_adapter_helpers as _adapter_helpers

_ACTIVE_PAYLOAD_STATUSES = frozenset(
    {"queued", "running", "submitted", "cancel_requested", "retrying"}
)


def _job_type_from_record(record: JobLocationRecord | None, fallback: str) -> str:
    if record is None:
        return fallback
    value = _adapter_helpers.normalize_text(record.job_type)
    if value.startswith("xtb_"):
        value = value[4:]
    return value or fallback


def _load_candidate_details(
    payload: dict[str, Any],
    *,
    fields: _adapter_helpers.ContractFieldReader,
    artifact_roots: tuple[Path, ...],
    require_identity: bool,
) -> tuple[XtbCandidateArtifact, ...]:
    raw_items = payload.get("candidate_details")
    if not isinstance(raw_items, list):
        return ()
    details: list[XtbCandidateArtifact] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        detail = XtbCandidateArtifact.from_raw(raw)
        if detail.path:
            resolved = fields.resolved_path(detail.path, roots=artifact_roots)
            if not resolved:
                raise ValueError(f"xTB candidate artifact escapes job_dir: {detail.path}")
            identity = detail.metadata.get("output_identity")
            if require_identity and not isinstance(identity, dict):
                identity = _engine_runner.confined_output_identity(artifact_roots[0], resolved)
                detail = replace(
                    detail,
                    metadata={
                        **detail.metadata,
                        "output_identity": identity,
                        "identity_backfilled_from_legacy_artifact": True,
                    },
                )
            if isinstance(identity, dict):
                verified = _engine_runner.verify_confined_output_identity(
                    artifact_roots[0],
                    identity,
                    path_override=resolved,
                )
                identity = {**identity, "path": str(verified)}
                detail = replace(
                    detail,
                    metadata={**detail.metadata, "output_identity": identity},
                )
            details.append(replace(detail, path=resolved))
    return tuple(details)


def _ordered_xtb_candidate_details(
    contract: XtbArtifactContract,
    policy: XtbDownstreamPolicy,
    *,
    require_geometry: bool,
) -> list[XtbCandidateArtifact]:
    selected_order = {path: index for index, path in enumerate(contract.selected_candidate_paths)}
    kind_priority = {kind: index for index, kind in enumerate(policy.preferred_kinds)}
    allowed_kinds = {kind for kind in policy.allowed_kinds if kind}
    details = list(contract.candidate_details)

    if policy.selected_only:
        filtered = [item for item in details if item.selected or item.path in selected_order]
        details = filtered or details

    def _sort_key(item: XtbCandidateArtifact) -> tuple[int, int, int, str]:
        path_rank = selected_order.get(item.path, 10_000)
        kind_rank = kind_priority.get(item.kind, len(kind_priority) + 100)
        item_rank = item.rank if item.rank > 0 else 10_000
        return (path_rank, kind_rank, item_rank, item.path)

    details = sorted(details, key=_sort_key)
    if policy.preferred_kinds:
        preferred = [item for item in details if item.kind in kind_priority]
        other = [item for item in details if item.kind not in kind_priority]
        details = preferred + other
    if allowed_kinds:
        details = [item for item in details if item.kind in allowed_kinds]
    if require_geometry:
        details = [item for item in details if has_xyz_geometry(item.path)]
    return details


def _stage_input_from_xtb_candidate(
    contract: XtbArtifactContract,
    detail: XtbCandidateArtifact,
) -> WorkflowStageInput:
    return WorkflowStageInput(
        source_job_id=contract.job_id,
        source_job_type=contract.job_type,
        reaction_key=contract.reaction_key,
        selected_input_xyz=contract.selected_input_xyz,
        rank=detail.rank,
        kind=detail.kind,
        artifact_path=detail.path,
        selected=detail.selected,
        score=detail.score,
        metadata=dict(detail.metadata),
    )


def load_xtb_artifact_contract(*, xtb_index_root: str | Path, target: str) -> XtbArtifactContract:
    bundle = _adapter_helpers.load_contract_artifact_bundle(
        index_root=xtb_index_root,
        target=target,
        resolve_job_location_fn=resolve_job_location,
        load_json_dict_fn=_adapter_helpers.load_json_dict,
        report_filename="job_report.json",
        state_filename="job_state.json",
        missing_label="xTB",
        expected_app_name="orca_auto_xtb",
        coerce_resource_dict_fn=coerce_int_mapping,
        select_payload_fn=partial(
            _adapter_helpers.select_active_artifact_payload,
            active_statuses=_ACTIVE_PAYLOAD_STATUSES,
        ),
    )
    fields = _adapter_helpers.ContractFieldReader(bundle)
    payload = fields.payload
    artifact_roots = fields.artifact_roots()

    status = fields.payload_record_text("status", "status", default="unknown")
    candidate_details = _load_candidate_details(
        payload,
        fields=fields,
        artifact_roots=artifact_roots,
        require_identity=status == "completed",
    )

    raw_selected_paths = fields.payload_sequence("selected_candidate_paths")
    selected_candidate_paths = fields.resolved_paths(
        raw_selected_paths,
        roots=artifact_roots,
    )
    if len(selected_candidate_paths) != len(raw_selected_paths):
        raise ValueError("xTB selected candidate artifact escapes job_dir")
    selected_candidate_paths = selected_candidate_paths or tuple(
        item.path for item in candidate_details if item.selected
    )

    job_type = _adapter_helpers.first_normalized_text(
        payload.get("job_type"),
        default=_job_type_from_record(fields.record, "unknown"),
    )
    reason = _adapter_helpers.normalize_text(payload.get("reason"))
    job_id = fields.payload_record_text("job_id", "job_id")
    reaction_key = fields.payload_record_text("reaction_key", "molecule_key")
    selected_input_xyz = fields.payload_record_text("selected_input_xyz", "selected_input_xyz")
    if selected_input_xyz:
        selected_input_xyz = fields.resolved_path(selected_input_xyz, roots=artifact_roots)
        if not selected_input_xyz:
            raise ValueError("xTB selected input artifact escapes job_dir")
    latest_known_path = bundle.latest_known_path

    analysis_summary = payload.get("analysis_summary")
    if not isinstance(analysis_summary, dict):
        analysis_summary = {}

    return XtbArtifactContract(
        job_id=job_id,
        job_type=job_type,
        status=status,
        reason=reason,
        job_dir=str(fields.job_dir),
        latest_known_path=latest_known_path,
        reaction_key=reaction_key,
        selected_input_xyz=selected_input_xyz,
        selected_candidate_paths=selected_candidate_paths,
        candidate_details=candidate_details,
        analysis_summary=dict(analysis_summary),
        resource_request=bundle.resource_request,
        resource_actual=bundle.resource_actual,
    )


def select_xtb_downstream_inputs(
    contract: XtbArtifactContract,
    *,
    policy: XtbDownstreamPolicy | None = None,
    require_geometry: bool = False,
) -> tuple[WorkflowStageInput, ...]:
    active_policy = policy or XtbDownstreamPolicy.build()
    details = _ordered_xtb_candidate_details(
        contract, active_policy, require_geometry=require_geometry
    )

    selected_inputs: list[WorkflowStageInput] = []
    for detail in details:
        selected_inputs.append(_stage_input_from_xtb_candidate(contract, detail))
        if len(selected_inputs) >= active_policy.max_candidates:
            break

    return tuple(selected_inputs)


__all__ = [
    "load_xtb_artifact_contract",
    "select_xtb_downstream_inputs",
]
