from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.utils.coercion import (
    coerce_int_mapping,
    normalize_text,
)
from orca_auto.orca.job_locations import _contract_payload as _canonical_payload
from orca_auto.orca.job_locations import _utils as _contract_status

from ..contracts.orca import OrcaArtifactContract
from . import _orca_contract_context as _contract_context
from . import _orca_local_lookup as _local_lookup
from . import _orca_path_helpers as _path_helpers

ContractPayload = dict[str, Any]


@dataclass(frozen=True)
class _StatusPayload:
    status: str
    analyzer_status: str
    reason: str
    completed_at: str


@dataclass(frozen=True)
class _ArtifactPaths:
    selected_inp: str
    selected_input_xyz: str
    optimized_xyz_path: str
    last_out_path: str


@dataclass(frozen=True)
class _ContractPayloadContext:
    resolved_run_id: str
    status: str
    reason: str
    state_status: str
    reaction_dir: str
    current_dir: Path | None
    artifact_dir: Path | None
    latest_known_path: str
    optimized_xyz_path: str
    queue_entry: ContractPayload | None
    selected_inp: str
    selected_input_xyz: str
    analyzer_status: str
    completed_at: str
    last_out_path: str
    state: ContractPayload
    report: ContractPayload
    resource_request: dict[str, int]
    resource_actual: dict[str, int]


def load_orca_artifact_contract_impl(
    *,
    target: str,
    orca_allowed_root: str | Path | None,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> OrcaArtifactContract:
    request = _contract_context.LoadRequest(
        target=target, queue_id=queue_id, run_id=run_id, reaction_dir=reaction_dir
    )
    roots = _contract_context.resolve_roots(orca_allowed_root)
    context = _load_contract_context(request, roots)
    return _contract_from_context(request, context)


def contract_from_orca_payload_impl(
    *,
    payload: ContractPayload,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> OrcaArtifactContract:
    request = _contract_context.LoadRequest(
        target=target, queue_id=queue_id, run_id=run_id, reaction_dir=reaction_dir
    )
    return _contract_from_payload(payload, request)


def _contract_from_payload(
    payload: ContractPayload,
    request: _contract_context.LoadRequest,
) -> OrcaArtifactContract:
    # The payload producers already normalize every key they emit. Only the
    # three request fallbacks below take raw caller input, so they keep their
    # normalization; the rest pass straight through.
    return OrcaArtifactContract(
        run_id=payload.get("run_id", ""),
        status=payload.get("status") or "unknown",
        reason=payload.get("reason", ""),
        state_status=payload.get("state_status", ""),
        reaction_dir=payload.get("reaction_dir") or normalize_text(request.reaction_dir),
        latest_known_path=payload.get("latest_known_path") or normalize_text(request.target),
        optimized_xyz_path=payload.get("optimized_xyz_path", ""),
        queue_id=payload.get("queue_id") or normalize_text(request.queue_id),
        queue_status=payload.get("queue_status", ""),
        cancel_requested=payload.get("cancel_requested", False),
        selected_inp=payload.get("selected_inp", ""),
        selected_input_xyz=payload.get("selected_input_xyz", ""),
        analyzer_status=payload.get("analyzer_status", ""),
        completed_at=payload.get("completed_at", ""),
        last_out_path=payload.get("last_out_path", ""),
        run_state_path=payload.get("run_state_path", ""),
        report_json_path=payload.get("report_json_path", ""),
        attempt_count=payload.get("attempt_count", 0),
        attempts=payload.get("attempts", ()),
        final_result=payload.get("final_result", {}),
        resource_request=payload.get("resource_request", {}),
        resource_actual=payload.get("resource_actual", {}),
    )


def _load_contract_context(
    request: _contract_context.LoadRequest,
    roots: _contract_context.LoadRoots,
) -> _contract_context.LoaderContext:
    context = _contract_context.load_context(request, roots)
    _contract_context.set_current_dir(request, context)
    _contract_context.load_context_payloads(context)
    context.resolved_run_id = _contract_context.resolve_run_id(request, context)
    return context


def _contract_from_context(
    request: _contract_context.LoadRequest,
    context: _contract_context.LoaderContext,
) -> OrcaArtifactContract:
    return _contract_from_payload(
        _payload_from_context(request, context),
        request,
    )


def _payload_from_context(
    request: _contract_context.LoadRequest,
    context: _contract_context.LoaderContext,
) -> ContractPayload:
    latest_known_path = _latest_known_path(request, context)
    status = _contract_status_payload(context)
    paths = _artifact_paths(context, latest_known_path)
    resource_request, resource_actual = _resource_payloads(context)
    _ensure_orca_record(context.tracked_record)
    queue = dict(context.queue_entry) if context.queue_entry is not None else None
    payload_context = _ContractPayloadContext(
        resolved_run_id=context.resolved_run_id,
        status=status.status,
        reason=status.reason,
        state_status=normalize_text(context.state.get("status")).lower(),
        reaction_dir=request.reaction_dir,
        current_dir=context.current_dir,
        artifact_dir=context.tracked_artifact_dir,
        latest_known_path=latest_known_path,
        optimized_xyz_path=paths.optimized_xyz_path,
        queue_entry=queue,
        selected_inp=paths.selected_inp,
        selected_input_xyz=paths.selected_input_xyz,
        analyzer_status=status.analyzer_status,
        completed_at=status.completed_at,
        last_out_path=paths.last_out_path,
        state=context.state,
        report=context.report,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
    return _canonical_payload.orca_contract_payload(payload_context)


def _latest_known_path(
    request: _contract_context.LoadRequest,
    context: _contract_context.LoaderContext,
) -> str:
    record_path = (
        normalize_text(context.tracked_record.latest_known_path)
        if context.tracked_record is not None
        else ""
    )
    if record_path:
        return record_path
    if context.current_dir is not None:
        return str(context.current_dir)
    return normalize_text(request.target)


def _contract_status_payload(context: _contract_context.LoaderContext) -> _StatusPayload:
    status, analyzer_status, reason, completed_at = _contract_status.status_from_payloads(
        queue_entry=context.queue_entry,
        state=context.state,
        report=context.report,
    )
    tracked_status = normalize_text(
        context.tracked_record.status if context.tracked_record is not None else ""
    ).lower()
    if status == "unknown" and tracked_status:
        status = tracked_status
    return _StatusPayload(status, analyzer_status, reason, completed_at)


def _artifact_paths(
    context: _contract_context.LoaderContext,
    latest_known_path: str,
) -> _ArtifactPaths:
    selected_inp = _path_helpers.resolve_artifact_path_impl(
        _selected_input_source(context), context.current_dir
    )
    if Path(normalize_text(selected_inp)).suffix.lower() != ".inp":
        selected_inp = ""
    last_out_path = _path_helpers.resolve_artifact_path_impl(
        _last_out_source(context), context.current_dir
    )
    selected_input_xyz = _path_helpers.resolve_artifact_path_impl(
        _selected_xyz_source(context), context.current_dir
    )
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = ""
    selected_input_xyz = selected_input_xyz or _path_helpers.derive_selected_input_xyz_impl(
        selected_inp
    )
    optimized_xyz_path = ""
    if selected_inp and (context.state or context.report):
        optimized_xyz_path = _path_helpers.prefer_orca_optimized_xyz_impl(
            selected_inp=selected_inp,
            selected_input_xyz=selected_input_xyz,
            current_dir=context.tracked_artifact_dir,
            latest_known_path=latest_known_path,
            last_out_path=last_out_path,
        )
    return _ArtifactPaths(selected_inp, selected_input_xyz, optimized_xyz_path, last_out_path)


def _selected_input_source(
    context: _contract_context.LoaderContext,
) -> Any:
    record_selected_inp = (
        context.tracked_record.selected_input_xyz if context.tracked_record is not None else ""
    )
    if Path(normalize_text(record_selected_inp)).suffix.lower() != ".inp":
        record_selected_inp = ""
    return (
        _local_lookup.queue_entry_metadata_value_impl(context.queue_entry, "selected_inp")
        or context.state.get("selected_inp")
        or context.report.get("selected_inp")
        or record_selected_inp
    )


def _selected_xyz_source(
    context: _contract_context.LoaderContext,
) -> Any:
    candidates = (
        _local_lookup.queue_entry_metadata_value_impl(context.queue_entry, "selected_input_xyz"),
        context.state.get("selected_input_xyz"),
        context.report.get("selected_input_xyz"),
        context.tracked_record.selected_input_xyz if context.tracked_record is not None else "",
    )
    for candidate in candidates:
        if Path(normalize_text(candidate)).suffix.lower() == ".xyz":
            return candidate
    return ""


def _last_out_source(context: _contract_context.LoaderContext) -> Any:
    state_final = context.state.get("final_result")
    report_final = context.report.get("final_result")
    state_final_payload = state_final if isinstance(state_final, dict) else {}
    report_final_payload = report_final if isinstance(report_final, dict) else {}
    return state_final_payload.get("last_out_path") or report_final_payload.get("last_out_path")


def _resource_payloads(
    context: _contract_context.LoaderContext,
) -> tuple[dict[str, int], dict[str, int]]:
    queue = context.queue_entry or {}
    queue_request = _local_lookup.queue_entry_metadata_value_impl(queue, "resource_request")
    queue_actual = _local_lookup.queue_entry_metadata_value_impl(queue, "resource_actual")
    resource_request = coerce_int_mapping(
        queue_request if isinstance(queue_request, dict) else {}
    ) or coerce_int_mapping(
        context.tracked_record.resource_request if context.tracked_record is not None else {}
    )
    resource_actual = coerce_int_mapping(
        queue_actual if isinstance(queue_actual, dict) else {}
    ) or coerce_int_mapping(
        context.tracked_record.resource_actual if context.tracked_record is not None else {}
    )
    return resource_request, resource_actual or dict(resource_request)


def _ensure_orca_record(tracked_record: Any) -> None:
    if (
        tracked_record is not None
        and tracked_record.app_name
        and tracked_record.app_name != ORCA_AUTO_ORCA_APP_NAME
    ):
        raise ValueError(f"Expected orca_auto_orca index record, got: {tracked_record.app_name}")


__all__ = [
    "contract_from_orca_payload_impl",
    "load_orca_artifact_contract_impl",
]
