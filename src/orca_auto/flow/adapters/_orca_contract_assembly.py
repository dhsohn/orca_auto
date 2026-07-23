from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.utils.coercion import normalize_bool, normalize_text, safe_int
from orca_auto.orca.job_locations import _contract_payload as _canonical_payload

from . import _orca_contract_context as _contract_context

ContractPayload = dict[str, Any]
StatusTuple = tuple[str, str, str, str]


@dataclass(frozen=True)
class _StatusPayload:
    status: str
    analyzer_status: str
    reason: str
    completed_at: str


@dataclass(frozen=True)
class OrcaContractLoaderDeps:
    path_type: type[Path]
    tracked_runtime_context_fn: Callable[
        ...,
        tuple[
            Path | None,
            Path | None,
            Any,
            ContractPayload,
            ContractPayload,
            ContractPayload | None,
        ]
        | None,
    ]
    tracked_artifact_context_fn: Callable[
        ..., tuple[Path | None, Any, ContractPayload, ContractPayload]
    ]
    find_queue_entry_fn: Callable[..., ContractPayload | None]
    queue_entry_metadata_value_fn: Callable[[ContractPayload | None, str], Any]
    resolve_candidate_path_fn: Callable[[Any], Path | None]
    direct_dir_target_fn: Callable[[str], Path | None]
    load_json_dict_fn: Callable[[Path], ContractPayload]
    status_from_payloads_fn: Callable[..., StatusTuple]
    resolve_artifact_path_fn: Callable[[Any, Path | None], str]
    derive_selected_input_xyz_fn: Callable[[str], str]
    prefer_orca_optimized_xyz_fn: Callable[..., str]
    coerce_resource_dict_fn: Callable[[Any], dict[str, int]]
    attempt_count_fn: Callable[[ContractPayload, ContractPayload], int]
    max_retries_fn: Callable[[ContractPayload, ContractPayload], int]
    coerce_attempts_fn: Callable[[ContractPayload, ContractPayload], tuple[ContractPayload, ...]]
    final_result_payload_fn: Callable[[ContractPayload, ContractPayload], ContractPayload]
    contract_cls: type


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


class _ContractPayloadDeps:
    def __init__(self, deps: OrcaContractLoaderDeps) -> None:
        self._deps = deps

    def normalize_text(self, value: Any) -> str:
        return normalize_text(value)

    def normalize_bool(self, value: Any) -> bool:
        return normalize_bool(value)

    def _runtime_paths(
        self,
        current_dir: Path | None,
        *,
        include_state: bool = True,
        include_report: bool = True,
        queue_entry: ContractPayload | None = None,
    ) -> dict[str, str]:
        return _canonical_payload.runtime_paths(
            current_dir,
            state_file_name="job_state.json",
            report_json_name="job_report.json",
            include_state=include_state,
            include_report=include_report,
            queue_entry=queue_entry,
        )

    def attempt_count(self, state: ContractPayload, report: ContractPayload) -> int:
        return self._deps.attempt_count_fn(state, report)

    def max_retries(self, state: ContractPayload, report: ContractPayload) -> int:
        return self._deps.max_retries_fn(state, report)

    def coerce_attempts(
        self, state: ContractPayload, report: ContractPayload
    ) -> tuple[ContractPayload, ...]:
        return self._deps.coerce_attempts_fn(state, report)

    def final_result_payload(
        self, state: ContractPayload, report: ContractPayload
    ) -> ContractPayload:
        return self._deps.final_result_payload_fn(state, report)


def load_orca_artifact_contract_impl(
    *,
    target: str,
    orca_allowed_root: str | Path | None,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
    deps: OrcaContractLoaderDeps,
) -> Any:
    request = _contract_context.LoadRequest(
        target=target, queue_id=queue_id, run_id=run_id, reaction_dir=reaction_dir
    )
    roots = _contract_context.resolve_roots(orca_allowed_root, deps)
    context = _load_contract_context(request, roots, deps)
    return _contract_from_context(request, roots, context, deps)


def contract_from_orca_payload_impl(
    *,
    payload: ContractPayload,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
    deps: OrcaContractLoaderDeps,
) -> Any:
    request = _contract_context.LoadRequest(
        target=target, queue_id=queue_id, run_id=run_id, reaction_dir=reaction_dir
    )
    return _contract_from_payload(payload, request, deps)


def _contract_from_payload(
    payload: ContractPayload,
    request: _contract_context.LoadRequest,
    deps: OrcaContractLoaderDeps,
) -> Any:
    final_result = payload.get("final_result")
    return deps.contract_cls(
        run_id=normalize_text(payload.get("run_id")),
        status=normalize_text(payload.get("status")) or "unknown",
        reason=normalize_text(payload.get("reason")),
        state_status=normalize_text(payload.get("state_status")),
        reaction_dir=normalize_text(payload.get("reaction_dir") or request.reaction_dir),
        latest_known_path=normalize_text(payload.get("latest_known_path") or request.target),
        optimized_xyz_path=normalize_text(payload.get("optimized_xyz_path")),
        queue_id=normalize_text(payload.get("queue_id") or request.queue_id),
        queue_status=normalize_text(payload.get("queue_status")).lower(),
        cancel_requested=normalize_bool(payload.get("cancel_requested")),
        selected_inp=normalize_text(payload.get("selected_inp")),
        selected_input_xyz=normalize_text(payload.get("selected_input_xyz")),
        analyzer_status=normalize_text(payload.get("analyzer_status")),
        completed_at=normalize_text(payload.get("completed_at")),
        last_out_path=normalize_text(payload.get("last_out_path")),
        run_state_path=normalize_text(payload.get("run_state_path")),
        report_json_path=normalize_text(payload.get("report_json_path")),
        attempt_count=safe_int(payload.get("attempt_count"), default=0),
        max_retries=safe_int(payload.get("max_retries"), default=0),
        attempts=_payload_attempts(payload),
        final_result=dict(final_result) if isinstance(final_result, dict) else {},
        resource_request=deps.coerce_resource_dict_fn(payload.get("resource_request")),
        resource_actual=deps.coerce_resource_dict_fn(payload.get("resource_actual")),
    )


def _load_contract_context(
    request: _contract_context.LoadRequest,
    roots: _contract_context.LoadRoots,
    deps: OrcaContractLoaderDeps,
) -> _contract_context.LoaderContext:
    context = _contract_context.load_context(request, roots, deps)
    _contract_context.set_current_dir(request, context, deps)
    _contract_context.load_context_payloads(context, deps)
    context.resolved_run_id = _contract_context.resolve_run_id(request, context, deps)
    return context


def _payload_attempts(payload: ContractPayload) -> tuple[ContractPayload, ...]:
    attempts_payload = payload.get("attempts")
    if not isinstance(attempts_payload, (list, tuple)):
        return ()
    return tuple(dict(item) for item in attempts_payload if isinstance(item, dict))


def _contract_from_context(
    request: _contract_context.LoadRequest,
    roots: _contract_context.LoadRoots,
    context: _contract_context.LoaderContext,
    deps: OrcaContractLoaderDeps,
) -> Any:
    return _contract_from_payload(
        _payload_from_context(request, roots, context, deps),
        request,
        deps,
    )


def _payload_from_context(
    request: _contract_context.LoadRequest,
    roots: _contract_context.LoadRoots,
    context: _contract_context.LoaderContext,
    deps: OrcaContractLoaderDeps,
) -> ContractPayload:
    latest_known_path = _latest_known_path(request, context, deps)
    status = _contract_status(context, deps)
    paths = _artifact_paths(context, latest_known_path, deps)
    resource_request, resource_actual = _resource_payloads(context, deps)
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
    return _canonical_payload.orca_contract_payload(
        payload_context,
        deps=_ContractPayloadDeps(deps),
    )


def _latest_known_path(
    request: _contract_context.LoadRequest,
    context: _contract_context.LoaderContext,
    deps: OrcaContractLoaderDeps,
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


def _contract_status(
    context: _contract_context.LoaderContext, deps: OrcaContractLoaderDeps
) -> _StatusPayload:
    status, analyzer_status, reason, completed_at = deps.status_from_payloads_fn(
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
    deps: OrcaContractLoaderDeps,
) -> _ArtifactPaths:
    selected_inp = deps.resolve_artifact_path_fn(
        _selected_input_source(context, deps), context.current_dir
    )
    if Path(normalize_text(selected_inp)).suffix.lower() != ".inp":
        selected_inp = ""
    last_out_path = deps.resolve_artifact_path_fn(_last_out_source(context), context.current_dir)
    selected_input_xyz = deps.resolve_artifact_path_fn(
        _selected_xyz_source(context, deps), context.current_dir
    )
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = ""
    selected_input_xyz = selected_input_xyz or deps.derive_selected_input_xyz_fn(selected_inp)
    optimized_xyz_path = ""
    if selected_inp and (context.state or context.report):
        optimized_xyz_path = deps.prefer_orca_optimized_xyz_fn(
            selected_inp=selected_inp,
            selected_input_xyz=selected_input_xyz,
            current_dir=context.tracked_artifact_dir,
            latest_known_path=latest_known_path,
            last_out_path=last_out_path,
        )
    return _ArtifactPaths(selected_inp, selected_input_xyz, optimized_xyz_path, last_out_path)


def _selected_input_source(
    context: _contract_context.LoaderContext,
    deps: OrcaContractLoaderDeps,
) -> Any:
    record_selected_inp = (
        context.tracked_record.selected_input_xyz if context.tracked_record is not None else ""
    )
    if Path(normalize_text(record_selected_inp)).suffix.lower() != ".inp":
        record_selected_inp = ""
    return (
        deps.queue_entry_metadata_value_fn(context.queue_entry, "selected_inp")
        or context.state.get("selected_inp")
        or context.report.get("selected_inp")
        or record_selected_inp
    )


def _selected_xyz_source(
    context: _contract_context.LoaderContext,
    deps: OrcaContractLoaderDeps,
) -> Any:
    candidates = (
        deps.queue_entry_metadata_value_fn(context.queue_entry, "selected_input_xyz"),
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
    deps: OrcaContractLoaderDeps,
) -> tuple[dict[str, int], dict[str, int]]:
    queue = context.queue_entry or {}
    queue_request = deps.queue_entry_metadata_value_fn(queue, "resource_request")
    queue_actual = deps.queue_entry_metadata_value_fn(queue, "resource_actual")
    resource_request = deps.coerce_resource_dict_fn(
        queue_request if isinstance(queue_request, dict) else {}
    ) or deps.coerce_resource_dict_fn(
        context.tracked_record.resource_request if context.tracked_record is not None else {}
    )
    resource_actual = deps.coerce_resource_dict_fn(
        queue_actual if isinstance(queue_actual, dict) else {}
    ) or deps.coerce_resource_dict_fn(
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
    "OrcaContractLoaderDeps",
    "contract_from_orca_payload_impl",
    "load_orca_artifact_contract_impl",
]
