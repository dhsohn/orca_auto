from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import _contract_payload as _contract_payload
from ._models import (
    JobRuntimeContext,
    OrcaContractPayloadContext,
    OrcaContractResolvedFields,
)


@dataclass(frozen=True)
class _ResolvedPathFields:
    latest_known_path: str
    selected_inp: str
    selected_input_xyz: str
    last_out_path: str
    optimized_xyz_path: str


@dataclass(frozen=True)
class _ResolvedStatusFields:
    state_status: str
    status: str
    analyzer_status: str
    reason: str
    completed_at: str


@dataclass(frozen=True)
class _ResolvedResourceFields:
    resource_request: dict[str, int]
    resource_actual: dict[str, int]


def _resolved_path_fields(
    *,
    runtime: JobRuntimeContext,
    payloads: _contract_payload.RuntimePayloads,
    current_dir: Path | None,
    artifact_dir: Path | None,
    target: str,
    deps: Any,
) -> _ResolvedPathFields:
    latest_known_path = _contract_payload.latest_known_path(
        record=payloads.record,
        current_dir=current_dir,
        target=target,
        deps=deps,
    )
    if runtime.generation_invalid:
        return _ResolvedPathFields(
            latest_known_path=latest_known_path,
            selected_inp="",
            selected_input_xyz="",
            last_out_path="",
            optimized_xyz_path="",
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
            deps=deps,
        )
    )
    return _ResolvedPathFields(
        latest_known_path=latest_known_path,
        selected_inp=selected_inp,
        selected_input_xyz=selected_input_xyz,
        last_out_path=last_out_path,
        optimized_xyz_path=optimized_xyz_path,
    )


def _resolved_status_fields(
    *,
    runtime: JobRuntimeContext,
    payloads: _contract_payload.RuntimePayloads,
    deps: Any,
) -> _ResolvedStatusFields:
    if runtime.generation_invalid:
        return _ResolvedStatusFields(
            state_status="",
            status="unknown",
            analyzer_status="",
            reason="queue_generation_verification_failed",
            completed_at="",
        )
    status, analyzer_status, reason, completed_at = _contract_payload.resolved_status(
        record=payloads.record,
        queue_entry=payloads.queue_entry,
        state=payloads.state,
        report=payloads.report,
        deps=deps,
    )
    return _ResolvedStatusFields(
        state_status=deps.normalize_text(payloads.state.get("status")).lower(),
        status=status,
        analyzer_status=analyzer_status,
        reason=reason,
        completed_at=completed_at,
    )


def _resolved_resource_fields(
    *,
    payloads: _contract_payload.RuntimePayloads,
    deps: Any,
) -> _ResolvedResourceFields:
    resource_request, resource_actual = _contract_payload.runtime_resources(
        record=payloads.record,
        queue_entry=payloads.queue_entry,
        deps=deps,
    )
    return _ResolvedResourceFields(
        resource_request=resource_request,
        resource_actual=resource_actual,
    )


def resolved_contract_fields(
    *,
    runtime: JobRuntimeContext,
    payloads: _contract_payload.RuntimePayloads,
    current_dir: Path | None,
    artifact_dir: Path | None,
    target: str,
    run_id: str,
    deps: Any,
) -> OrcaContractResolvedFields:
    path_fields = _resolved_path_fields(
        runtime=runtime,
        payloads=payloads,
        current_dir=current_dir,
        artifact_dir=artifact_dir,
        target=target,
        deps=deps,
    )
    status_fields = _resolved_status_fields(runtime=runtime, payloads=payloads, deps=deps)
    resource_fields = _resolved_resource_fields(payloads=payloads, deps=deps)
    return OrcaContractResolvedFields(
        resolved_run_id=_contract_payload.resolved_run_id(
            run_id=run_id,
            state=payloads.state,
            report=payloads.report,
            queue_entry=payloads.queue_entry,
            deps=deps,
        ),
        latest_known_path=path_fields.latest_known_path,
        state_status=status_fields.state_status,
        status=status_fields.status,
        analyzer_status=status_fields.analyzer_status,
        reason=status_fields.reason,
        completed_at=status_fields.completed_at,
        selected_inp=path_fields.selected_inp,
        selected_input_xyz=path_fields.selected_input_xyz,
        last_out_path=path_fields.last_out_path,
        optimized_xyz_path=path_fields.optimized_xyz_path,
        resource_request=resource_fields.resource_request,
        resource_actual=resource_fields.resource_actual,
    )


def payload_context_from_runtime(
    *,
    runtime: JobRuntimeContext,
    target: str,
    run_id: str,
    reaction_dir: str,
    deps: Any,
    resolved_fields_fn: Any | None = None,
) -> OrcaContractPayloadContext:
    payloads = _contract_payload.runtime_payloads(runtime)
    resolved_target = "" if runtime.selector_miss else target
    resolved_reaction_dir = "" if runtime.selector_miss else reaction_dir
    current_dir = _contract_payload.runtime_current_dir(
        runtime,
        queue_entry=payloads.queue_entry,
        reaction_dir=resolved_reaction_dir,
        deps=deps,
    )
    artifact_dir = None if runtime.generation_invalid else runtime.artifact_dir or current_dir
    field_resolver = resolved_fields_fn or resolved_contract_fields
    resolved = field_resolver(
        runtime=runtime,
        payloads=payloads,
        current_dir=current_dir,
        artifact_dir=artifact_dir,
        target=resolved_target,
        run_id=run_id,
        deps=deps,
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
    deps: Any,
) -> dict[str, Any]:
    if ctx.missing:
        return {}
    payload = _contract_payload.orca_contract_payload(ctx, deps=deps)
    if ctx.runtime.selector_miss:
        payload["reason"] = "queue_generation_not_found"
    if not payload["queue_id"]:
        payload["queue_id"] = deps.normalize_text(queue_id)
    return payload
