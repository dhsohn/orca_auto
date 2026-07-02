from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.metadata import mapping_metadata_value as queue_entry_metadata_value


@dataclass(frozen=True)
class RuntimePayloads:
    record: Any
    queue_entry: dict[str, Any]
    state: dict[str, Any]
    report: dict[str, Any]
    organized_ref: dict[str, Any]


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
    return RuntimePayloads(
        record=artifact.record,
        queue_entry=dict(runtime.queue_entry) if isinstance(runtime.queue_entry, dict) else {},
        state=dict(artifact.state) if isinstance(artifact.state, dict) else {},
        report=dict(artifact.report) if isinstance(artifact.report, dict) else {},
        organized_ref=dict(artifact.organized_ref)
        if isinstance(artifact.organized_ref, dict)
        else {},
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
    organized_ref: dict[str, Any],
    queue_entry: dict[str, Any],
    deps: Any,
) -> str:
    return (
        deps.normalize_text(run_id)
        or deps.normalize_text(state.get("run_id"))
        or deps.normalize_text(report.get("run_id"))
        or deps.normalize_text(organized_ref.get("run_id"))
        or deps.normalize_text(queue_entry_metadata_value(queue_entry, "run_id"))
    )


def latest_known_path(
    *,
    record: Any,
    runtime: Any,
    current_dir: Path | None,
    target: str,
    deps: Any,
) -> str:
    if record is not None and deps.normalize_text(record.latest_known_path):
        return deps.normalize_text(record.latest_known_path)
    if runtime.organized_dir is not None:
        return str(runtime.organized_dir)
    if current_dir is not None:
        return str(current_dir)
    return deps.normalize_text(target)


def selected_artifact_paths(
    *,
    record: Any,
    state: dict[str, Any],
    report: dict[str, Any],
    organized_ref: dict[str, Any],
    current_dir: Path | None,
    organized_dir: Path | None,
    latest_known_path: str,
    deps: Any,
) -> tuple[str, str, str, str]:
    selected_inp = deps.resolve_artifact_path(
        state.get("selected_inp")
        or report.get("selected_inp")
        or organized_ref.get("selected_inp")
        or organized_ref.get("selected_input_xyz")
        or (record.selected_input_xyz if record is not None else ""),
        current_dir,
    )
    state_final_result = state.get("final_result")
    state_final = state_final_result if isinstance(state_final_result, dict) else {}
    report_final_result = report.get("final_result")
    report_final = report_final_result if isinstance(report_final_result, dict) else {}
    last_out_path = deps.resolve_artifact_path(
        state_final.get("last_out_path") or report_final.get("last_out_path"),
        current_dir,
    )
    selected_input_xyz = deps.resolve_artifact_path(
        organized_ref.get("selected_input_xyz")
        or (record.selected_input_xyz if record is not None else ""),
        current_dir,
    )
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = ""
    selected_input_xyz = selected_input_xyz or deps.derive_selected_input_xyz(selected_inp)
    optimized_xyz_path = deps.prefer_orca_optimized_xyz(
        selected_inp=selected_inp,
        selected_input_xyz=selected_input_xyz,
        current_dir=current_dir,
        organized_dir=organized_dir,
        latest_known_path=latest_known_path,
        last_out_path=last_out_path,
    )
    return selected_inp, selected_input_xyz, last_out_path, optimized_xyz_path


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


def organized_output_dir(
    *,
    record: Any,
    organized_ref: dict[str, Any],
    organized_dir: Path | None,
    current_dir: Path | None,
    organized_root: str | Path | None,
    deps: Any,
) -> str:
    resolved_organized_root = (
        Path(organized_root).expanduser().resolve() if organized_root else None
    )
    return deps.normalize_text(
        (record.organized_output_dir if record is not None else "")
        or organized_ref.get("organized_output_dir")
        or (str(organized_dir) if organized_dir is not None else "")
        or (
            str(current_dir)
            if current_dir is not None and deps.is_subpath(current_dir, resolved_organized_root)
            else ""
        )
    )


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
        "organized_output_dir": ctx.organized_output_dir,
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
