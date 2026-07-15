from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.engines.artifacts import load_engine_artifact_payload
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.core.queue.engine.snapshot_intent import SNAPSHOT_INTENT_TOKEN_KEY
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.metadata import mapping_metadata_value as queue_entry_metadata_value

from ._generation import (
    current_generation_payloads,
    payload_matches_queue_generation,
    queue_has_generation_identity,
)

_REPORT_MARKDOWN_IDENTITY_PREFIXES = {
    "- Job ID: `": "job_id",
    "- run_id: `": "run_id",
}


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
    include_state: bool = True,
    include_report: bool = True,
    queue_entry: dict[str, Any] | None = None,
    report_md_dir: Path | None = None,
) -> dict[str, str]:
    state_path = current_dir / state_file_name if current_dir is not None else None
    report_json_path = current_dir / report_json_name if current_dir is not None else None
    markdown_dir = report_md_dir if report_md_dir is not None else current_dir
    report_md_path = markdown_dir / report_md_name if markdown_dir is not None else None
    visible_state_path = (
        _runtime_payload_path_for_generation(state_path, current_dir, queue_entry)
        if include_state
        else None
    )
    visible_report_json_path = (
        _runtime_payload_path_for_generation(report_json_path, current_dir, queue_entry)
        if include_report
        else None
    )
    visible_report_md_path = (
        _report_markdown_path_for_generation(report_md_path, markdown_dir, queue_entry)
        if visible_report_json_path is not None
        else None
    )
    return {
        "run_state_path": str(visible_state_path) if visible_state_path is not None else "",
        "report_json_path": (
            str(visible_report_json_path) if visible_report_json_path is not None else ""
        ),
        "report_md_path": (
            str(visible_report_md_path) if visible_report_md_path is not None else ""
        ),
    }


def _direct_runtime_file(path: Path | None, current_dir: Path | None) -> Path | None:
    if path is None or current_dir is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
        resolved_path = path.resolve()
        if resolved_path.parent != current_dir.resolve():
            return None
    except (OSError, RuntimeError):
        return None
    return resolved_path


def _runtime_payload_path_for_generation(
    path: Path | None,
    current_dir: Path | None,
    queue_entry: dict[str, Any] | None,
) -> Path | None:
    direct_path = _direct_runtime_file(path, current_dir)
    if direct_path is None:
        return None
    if not queue_has_generation_identity(queue_entry):
        return direct_path
    payload = load_engine_artifact_payload(direct_path)
    if payload is None or str(payload.get("engine") or "").strip() != "orca":
        return None
    if not payload_matches_queue_generation(queue_entry, payload):
        return None
    return direct_path if _direct_runtime_file(path, current_dir) == direct_path else None


def _report_markdown_path_for_generation(
    path: Path | None,
    current_dir: Path | None,
    queue_entry: dict[str, Any] | None,
) -> Path | None:
    direct_path = _direct_runtime_file(path, current_dir)
    if direct_path is None:
        return None
    if not queue_has_generation_identity(queue_entry):
        return direct_path
    try:
        lines = direct_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    identity: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        for prefix, key in _REPORT_MARKDOWN_IDENTITY_PREFIXES.items():
            if not stripped.startswith(prefix) or not stripped.endswith("`"):
                continue
            value = stripped[len(prefix) : -1].strip()
            existing = identity.get(key)
            if existing is not None and existing != value:
                return None
            identity[key] = value
    if not identity or not payload_matches_queue_generation(queue_entry, identity):
        return None
    return direct_path if _direct_runtime_file(path, current_dir) == direct_path else None


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
    if runtime.selector_miss:
        return None
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
    optimized_search_allowed, optimized_search_dir, excluded_xyz_paths = (
        _generation_optimized_xyz_policy(
            queue_entry=queue_entry,
            state=state,
            report=report,
            current_dir=current_dir,
            deps=deps,
        )
    )
    optimized_xyz_path = ""
    if optimized_search_allowed:
        optimized_current_dir = optimized_search_dir or current_dir
        optimized_latest_path = (
            str(optimized_search_dir) if optimized_search_dir is not None else latest_known_path
        )
        optimized_xyz_path = deps.prefer_orca_optimized_xyz(
            selected_inp=selected_inp,
            selected_input_xyz=selected_input_xyz,
            current_dir=optimized_current_dir,
            latest_known_path=optimized_latest_path,
            last_out_path=last_out_path,
            excluded_xyz_paths=excluded_xyz_paths,
        )
    return selected_inp, selected_input_xyz, last_out_path, optimized_xyz_path


def _payload_execution_provenance(payload: dict[str, Any]) -> dict[str, Any] | None:
    provenance = payload.get("execution_provenance")
    if isinstance(provenance, dict):
        return provenance
    engine_payload = payload.get("engine_payload")
    if isinstance(engine_payload, dict):
        provenance = engine_payload.get("execution_provenance")
        if isinstance(provenance, dict):
            return provenance
    return None


def _generation_optimized_source(
    queue_entry: dict[str, Any],
    state: dict[str, Any],
    report: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    snapshot = queue_entry_metadata_value(queue_entry, "execution_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("version") == 2:
        return snapshot, str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    for payload in (state, report):
        provenance = _payload_execution_provenance(payload)
        if provenance is not None and (
            provenance.get("execution_dir_identity") is not None
            or provenance.get("generation_owner_token") is not None
        ):
            return provenance, str(provenance.get("generation_owner_token") or "").strip()
    return None, ""


def _generation_optimized_xyz_policy(
    *,
    queue_entry: dict[str, Any],
    state: dict[str, Any],
    report: dict[str, Any],
    current_dir: Path | None,
    deps: Any,
) -> tuple[bool, Path | None, tuple[str, ...]]:
    """Keep schema-2 staged XYZ inputs from masquerading as calculated outputs."""

    source, owner_token = _generation_optimized_source(queue_entry, state, report)
    if source is None:
        return True, None, ()
    if not owner_token or not deps.coerce_attempts(state, report):
        return False, None, ()
    materialized_inputs = source.get("materialized_inputs")
    mutable_roles = source.get("runtime_mutable_input_roles")
    raw_generation_identity = source.get("execution_dir_identity")
    bound_selected_identity = source.get("bound_selected_identity")
    if (
        not isinstance(materialized_inputs, dict)
        or not isinstance(mutable_roles, list)
        or not isinstance(raw_generation_identity, dict)
        or not isinstance(bound_selected_identity, dict)
    ):
        return False, None, ()
    if (
        any(not isinstance(role, str) or not role for role in mutable_roles)
        or len(mutable_roles) != len(set(mutable_roles))
        or not set(mutable_roles).issubset(materialized_inputs)
    ):
        return False, None, ()
    try:
        device = int(raw_generation_identity.get("device", -1))
        inode = int(raw_generation_identity.get("inode", -1))
        raw_generation_dir = Path(str(source.get("execution_dir") or "")).expanduser()
        generation_dir = raw_generation_dir.resolve(strict=True)
        generation_status = raw_generation_dir.lstat()
        job_dir = generation_dir.parent
        job_status = job_dir.stat()
        raw_selected = Path(str(bound_selected_identity.get("path") or "")).expanduser()
        selected = raw_selected.resolve(strict=True)
        selected_identity = _engine_runner.executable_identity(selected)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, None, ()
    if (
        device < 0
        or inode <= 0
        or not raw_generation_dir.is_absolute()
        or raw_generation_dir != generation_dir
        or raw_generation_dir.is_symlink()
        or not is_visible_generation_name(generation_dir.name)
        or (int(generation_status.st_dev), int(generation_status.st_ino)) != (device, inode)
        or raw_selected != selected
        or raw_selected.is_symlink()
        or not selected.is_file()
        or selected.parent != generation_dir
        or selected_identity != dict(bound_selected_identity)
    ):
        return False, None, ()
    if current_dir is not None:
        try:
            resolved_current_dir = current_dir.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return False, None, ()
        if resolved_current_dir not in {job_dir, generation_dir}:
            return False, None, ()
    try:
        require_direct_generation_owner(
            job_dir,
            namespace=generation_dir.name,
            expected_job_identity=(int(job_status.st_dev), int(job_status.st_ino)),
            expected_generation_identity=(device, inode),
            owner_token=owner_token,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False, None, ()
    excluded: list[str] = []
    for role, raw_identity in materialized_inputs.items():
        if not isinstance(role, str) or not role or not isinstance(raw_identity, dict):
            return False, None, ()
        raw_path_text = deps.normalize_text(raw_identity.get("path"))
        try:
            raw_path = Path(raw_path_text).expanduser()
            materialized_path = raw_path.resolve(strict=True)
        except (OSError, RuntimeError):
            return False, None, ()
        if (
            not raw_path_text
            or raw_path != materialized_path
            or raw_path.is_symlink()
            or not materialized_path.is_file()
            or materialized_path.parent != generation_dir
        ):
            return False, None, ()
        if materialized_path.suffix.lower() != ".xyz":
            continue
        if role in mutable_roles:
            try:
                current_identity = _engine_runner.executable_identity(materialized_path)
                original_size = int(raw_identity.get("size_bytes", -1))
            except (OSError, RuntimeError, TypeError, ValueError):
                return False, None, ()
            original_digest = deps.normalize_text(raw_identity.get("sha256"))
            if (
                original_digest
                and original_size >= 0
                and (
                    current_identity.get("sha256") != original_digest
                    or current_identity.get("size_bytes") != original_size
                )
            ):
                continue
        excluded.append(str(materialized_path))
    return True, generation_dir, tuple(excluded)


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
        **deps._runtime_paths(
            getattr(ctx, "artifact_dir", ctx.current_dir),
            queue_entry=ctx.queue_entry,
            report_md_dir=ctx.current_dir,
        ),
        "attempt_count": deps.attempt_count(ctx.state, ctx.report),
        "max_retries": deps.max_retries(ctx.state, ctx.report),
        "attempts": deps.coerce_attempts(ctx.state, ctx.report),
        "final_result": deps.final_result_payload(ctx.state, ctx.report),
        "resource_request": ctx.resource_request,
        "resource_actual": ctx.resource_actual,
    }
