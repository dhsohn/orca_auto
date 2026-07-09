from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.metadata import mapping_metadata_value as queue_entry_metadata_value

from ._generation import current_generation_payloads


@dataclass(frozen=True)
class RuntimePayloads:
    record: Any
    queue_entry: dict[str, Any]
    state: dict[str, Any]
    report: dict[str, Any]


def runtime_paths(
    current_dir: Path | None,
    *,
    state_file_name: str,
    report_json_name: str,
    report_md_name: str,
) -> dict[str, str]:
    return {
        "run_state_path": str((current_dir / state_file_name).resolve())
        if current_dir is not None and (current_dir / state_file_name).exists()
        else "",
        "report_json_path": str((current_dir / report_json_name).resolve())
        if current_dir is not None and (current_dir / report_json_name).exists()
        else "",
        "report_md_path": str((current_dir / report_md_name).resolve())
        if current_dir is not None and (current_dir / report_md_name).exists()
        else "",
    }


def runtime_payloads(runtime: Any) -> RuntimePayloads:
    artifact = runtime.artifact
    queue_entry = dict(runtime.queue_entry) if isinstance(runtime.queue_entry, dict) else {}
    state = dict(artifact.state) if isinstance(artifact.state, dict) else {}
    report = dict(artifact.report) if isinstance(artifact.report, dict) else {}
    state, report = current_generation_payloads(queue_entry, state, report)
    return RuntimePayloads(
        record=artifact.record,
        queue_entry=queue_entry,
        state=state,
        report=report,
    )


def runtime_current_dir(
    runtime: Any,
    *,
    queue_entry: dict[str, Any],
    reaction_dir: str,
    deps: Any,
) -> Path | None:
    return (
        runtime.artifact.job_dir
        or deps.resolve_existing_job_dir(reaction_dir)
        or deps.resolve_existing_job_dir(queue_entry_metadata_value(queue_entry, "reaction_dir"))
    )


def resolved_run_id(
    *,
    run_id: str,
    state: dict[str, Any],
    report: dict[str, Any],
    queue_entry: dict[str, Any],
    deps: Any,
) -> str:
    return (
        deps.normalize_text(run_id)
        or deps.normalize_text(state.get("run_id"))
        or deps.normalize_text(report.get("run_id"))
        or deps.normalize_text(queue_entry_metadata_value(queue_entry, "run_id"))
    )


def latest_known_path(
    *,
    record: Any,
    current_dir: Path | None,
    target: str,
    deps: Any,
) -> str:
    if record is not None and deps.normalize_text(record.latest_known_path):
        return deps.normalize_text(record.latest_known_path)
    if current_dir is not None:
        return str(current_dir)
    return deps.normalize_text(target)


def selected_artifact_paths(
    *,
    record: Any,
    queue_entry: dict[str, Any],
    state: dict[str, Any],
    report: dict[str, Any],
    current_dir: Path | None,
    latest_known_path: str,
    deps: Any,
) -> tuple[str, str, str, str]:
    record_selected_inp = record.selected_input_xyz if record is not None else ""
    if Path(deps.normalize_text(record_selected_inp)).suffix.lower() != ".inp":
        record_selected_inp = ""
    selected_inp = deps.resolve_artifact_path(
        queue_entry_metadata_value(queue_entry, "selected_inp")
        or state.get("selected_inp")
        or report.get("selected_inp")
        or record_selected_inp,
        current_dir,
    )
    if Path(deps.normalize_text(selected_inp)).suffix.lower() != ".inp":
        selected_inp = ""
    state_final_result = state.get("final_result")
    state_final = state_final_result if isinstance(state_final_result, dict) else {}
    report_final_result = report.get("final_result")
    report_final = report_final_result if isinstance(report_final_result, dict) else {}
    last_out_path = deps.resolve_artifact_path(
        state_final.get("last_out_path") or report_final.get("last_out_path"),
        current_dir,
    )
    selected_input_xyz = deps.resolve_artifact_path(
        _selected_xyz_source(
            record=record,
            queue_entry=queue_entry,
            state=state,
            report=report,
            deps=deps,
        ),
        current_dir,
    )
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = ""
    selected_input_xyz = selected_input_xyz or deps.derive_selected_input_xyz(selected_inp)
    optimized_xyz_path = deps.prefer_orca_optimized_xyz(
        selected_inp=selected_inp,
        selected_input_xyz=selected_input_xyz,
        current_dir=current_dir,
        latest_known_path=latest_known_path,
        last_out_path=last_out_path,
    )
    return selected_inp, selected_input_xyz, last_out_path, optimized_xyz_path


def _payload_selected_xyz(payload: dict[str, Any]) -> Any:
    input_payload = payload.get("input")
    normalized_input = input_payload if isinstance(input_payload, dict) else {}
    return payload.get("selected_input_xyz") or normalized_input.get("selected_xyz_path")


def _selected_xyz_source(
    *,
    record: Any,
    queue_entry: dict[str, Any],
    state: dict[str, Any],
    report: dict[str, Any],
    deps: Any,
) -> Any:
    candidates = (
        queue_entry_metadata_value(queue_entry, "selected_input_xyz"),
        _payload_selected_xyz(state),
        _payload_selected_xyz(report),
        record.selected_input_xyz if record is not None else "",
    )
    for candidate in candidates:
        if Path(deps.normalize_text(candidate)).suffix.lower() == ".xyz":
            return candidate
    return ""


def runtime_resources(
    *,
    record: Any,
    queue_entry: dict[str, Any],
    deps: Any,
) -> tuple[dict[str, int], dict[str, int]]:
    resource_request = deps.resource_dict_from_any(
        queue_entry_metadata_value(queue_entry, "resource_request")
    ) or deps.resource_dict_from_any(record.resource_request if record is not None else {})
    resource_actual = (
        deps.resource_dict_from_any(queue_entry_metadata_value(queue_entry, "resource_actual"))
        or deps.resource_dict_from_any(record.resource_actual if record is not None else {})
        or dict(resource_request)
    )
    return resource_request, resource_actual


def resolved_status(
    *,
    record: Any,
    queue_entry: dict[str, Any],
    state: dict[str, Any],
    report: dict[str, Any],
    deps: Any,
) -> tuple[str, str, str, str]:
    status, analyzer_status, reason, completed_at = deps.status_from_payloads(
        queue_entry=queue_entry,
        state=state,
        report=report,
    )
    tracked_status = deps.normalize_text(record.status if record is not None else "").lower()
    if status == "unknown" and tracked_status:
        status = tracked_status
    return status, analyzer_status, reason, completed_at


def orca_contract_payload(ctx: Any, *, deps: Any) -> dict[str, Any]:
    return {
        "run_id": ctx.resolved_run_id,
        "status": ctx.status,
        "reason": ctx.reason,
        "state_status": ctx.state_status,
        "reaction_dir": str(current_dir)
        if (current_dir := ctx.current_dir) is not None
        else deps.normalize_text(ctx.reaction_dir),
        "latest_known_path": ctx.latest_known_path,
        "optimized_xyz_path": ctx.optimized_xyz_path,
        "queue_id": deps.normalize_text(ctx.queue_entry.get("queue_id") or ""),
        "queue_status": deps.normalize_text(ctx.queue_entry.get("status")).lower(),
        "cancel_requested": deps.normalize_bool(ctx.queue_entry.get("cancel_requested")),
        "selected_inp": ctx.selected_inp,
        "selected_input_xyz": ctx.selected_input_xyz,
        "analyzer_status": ctx.analyzer_status,
        "completed_at": ctx.completed_at,
        "last_out_path": ctx.last_out_path,
        **deps._runtime_paths(ctx.current_dir),
        "attempt_count": deps.attempt_count(ctx.state, ctx.report),
        "max_retries": deps.max_retries(ctx.state, ctx.report),
        "attempts": deps.coerce_attempts(ctx.state, ctx.report),
        "final_result": deps.final_result_payload(ctx.state, ctx.report),
        "resource_request": ctx.resource_request,
        "resource_actual": ctx.resource_actual,
    }
