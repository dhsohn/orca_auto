from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import _contract_payload as _contract_payload
from . import _runtime_context as _runtime_context
from ._models import (
    JobRuntimeContext,
    OrcaContractPayloadContext,
    OrcaContractResolvedFields,
)
from ._utils import normalize_text


def resolved_contract_fields(
    *,
    runtime: JobRuntimeContext,
    payloads: _contract_payload.RuntimePayloads,
    current_dir: Path | None,
    artifact_dir: Path | None,
    target: str,
    run_id: str,
) -> OrcaContractResolvedFields:
    resolved_run_id = _contract_payload.resolved_run_id(
        run_id=run_id,
        state=payloads.state,
        report=payloads.report,
        queue_entry=payloads.queue_entry,
    )
    latest_known_path = _contract_payload.latest_known_path(
        record=payloads.record,
        current_dir=current_dir,
        target=target,
    )
    resource_request, resource_actual = _contract_payload.runtime_resources(
        record=payloads.record,
        queue_entry=payloads.queue_entry,
    )
    if runtime.generation_invalid:
        return OrcaContractResolvedFields(
            resolved_run_id=resolved_run_id,
            latest_known_path=latest_known_path,
            state_status="",
            status="unknown",
            analyzer_status="",
            reason="queue_generation_verification_failed",
            completed_at="",
            selected_inp="",
            selected_input_xyz="",
            last_out_path="",
            optimized_xyz_path="",
            resource_request=resource_request,
            resource_actual=resource_actual,
        )
    artifact_latest_known_path = (
        str(runtime.artifact_dir) if runtime.artifact_dir is not None else latest_known_path
    )
    selected_inp, selected_input_xyz, last_out_path, optimized_xyz_path = (
        _contract_payload.selected_artifact_paths(
            record=payloads.record,
            queue_entry=payloads.queue_entry,
            state=payloads.state,
            report=payloads.report,
            current_dir=artifact_dir,
            latest_known_path=artifact_latest_known_path,
        )
    )
    status, analyzer_status, reason, completed_at = _contract_payload.resolved_status(
        record=payloads.record,
        queue_entry=payloads.queue_entry,
        state=payloads.state,
        report=payloads.report,
    )
    return OrcaContractResolvedFields(
        resolved_run_id=resolved_run_id,
        latest_known_path=latest_known_path,
        state_status=normalize_text(payloads.state.get("status")).lower(),
        status=status,
        analyzer_status=analyzer_status,
        reason=reason,
        completed_at=completed_at,
        selected_inp=selected_inp,
        selected_input_xyz=selected_input_xyz,
        last_out_path=last_out_path,
        optimized_xyz_path=optimized_xyz_path,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )


def payload_context_from_runtime(
    *,
    runtime: JobRuntimeContext,
    target: str,
    run_id: str,
    reaction_dir: str,
) -> OrcaContractPayloadContext:
    payloads = _contract_payload.runtime_payloads(runtime)
    resolved_target = "" if runtime.selector_miss else target
    resolved_reaction_dir = "" if runtime.selector_miss else reaction_dir
    current_dir = _contract_payload.runtime_current_dir(
        runtime,
        queue_entry=payloads.queue_entry,
        reaction_dir=resolved_reaction_dir,
    )
    # Only a provenance-verified execution generation may expose runtime
    # state/report paths. Reports exist only inside their bound generation.
    artifact_dir = None if runtime.generation_invalid else runtime.artifact_dir
    resolved = resolved_contract_fields(
        runtime=runtime,
        payloads=payloads,
        current_dir=current_dir,
        artifact_dir=artifact_dir,
        target=resolved_target,
        run_id=run_id,
    )

    return OrcaContractPayloadContext(
        runtime=runtime,
        target=resolved_target,
        reaction_dir=resolved_reaction_dir,
        record=payloads.record,
        queue_entry=payloads.queue_entry,
        state=payloads.state,
        report=payloads.report,
        current_dir=current_dir,
        artifact_dir=artifact_dir,
        **asdict(resolved),
    )


def payload_from_context(
    ctx: OrcaContractPayloadContext,
    *,
    queue_id: str,
) -> dict[str, Any]:
    if ctx.missing:
        return {}
    payload = _contract_payload.orca_contract_payload(ctx)
    if ctx.runtime.selector_miss:
        payload["reason"] = "queue_generation_not_found"
    if not payload["queue_id"]:
        payload["queue_id"] = normalize_text(queue_id)
    return payload


def load_orca_contract_payload(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> dict[str, Any]:
    runtime = _runtime_context.load_job_runtime_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )
    ctx = payload_context_from_runtime(
        runtime=runtime,
        target=target,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )
    return payload_from_context(ctx, queue_id=queue_id)
