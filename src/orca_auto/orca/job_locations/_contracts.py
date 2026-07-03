from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..state import (
    REPORT_JSON_NAME,
    REPORT_MD_NAME,
    STATE_FILE_NAME,
    load_report_json,
    load_state,
)
from . import _artifacts as _artifacts
from . import _contract_context as _contract_context
from . import _contract_payload as _contract_payload
from . import _runtime_context as _runtime_context
from ._models import (
    JobArtifactContext,
    JobRuntimeContext,
    OrcaContractPayloadContext,
)
from ._models import (
    OrcaContractResolvedFields as _OrcaContractResolvedFields,
)
from ._records import list_job_location_records, resolve_record_job_dir
from ._utils import (
    QUEUE_FILE_NAME,
    attempt_count,
    coerce_attempts,
    derive_selected_input_xyz,
    final_result_payload,
    is_subpath,
    load_json_list,
    max_retries,
    normalize_bool,
    normalize_text,
    prefer_orca_optimized_xyz,
    queue_entry_metadata_value,
    resolve_artifact_path,
    resolve_existing_job_dir,
    resource_dict_from_any,
    status_from_payloads,
)


@dataclass(frozen=True)
class _JobLocationDeps:
    JobRuntimeContext: Any
    _runtime_paths: Any
    list_job_location_records: Any
    load_report_json: Any
    load_state: Any
    resolve_record_job_dir: Any
    _first_artifact_context: Any
    _job_artifact_context: Any
    final_result_payload: Any
    queue_entry_metadata_value: Any
    status_from_payloads: Any
    _find_queue_entry: Any
    attempt_count: Any
    coerce_attempts: Any
    derive_selected_input_xyz: Any
    is_subpath: Any
    max_retries: Any
    normalize_bool: Any
    normalize_text: Any
    prefer_orca_optimized_xyz: Any
    resolve_artifact_path: Any
    resolve_existing_job_dir: Any
    resource_dict_from_any: Any


def _job_location_deps() -> _JobLocationDeps:
    return _JobLocationDeps(
        JobRuntimeContext=JobRuntimeContext,
        _runtime_paths=_runtime_paths,
        list_job_location_records=list_job_location_records,
        load_report_json=load_report_json,
        load_state=load_state,
        resolve_record_job_dir=resolve_record_job_dir,
        _first_artifact_context=_artifacts.first_artifact_context,
        _job_artifact_context=_artifacts.job_artifact_context,
        final_result_payload=final_result_payload,
        queue_entry_metadata_value=queue_entry_metadata_value,
        status_from_payloads=status_from_payloads,
        _find_queue_entry=_find_queue_entry,
        attempt_count=attempt_count,
        coerce_attempts=coerce_attempts,
        derive_selected_input_xyz=derive_selected_input_xyz,
        is_subpath=is_subpath,
        max_retries=max_retries,
        normalize_bool=normalize_bool,
        normalize_text=normalize_text,
        prefer_orca_optimized_xyz=prefer_orca_optimized_xyz,
        resolve_artifact_path=resolve_artifact_path,
        resolve_existing_job_dir=resolve_existing_job_dir,
        resource_dict_from_any=resource_dict_from_any,
    )


def _queue_entry_matches(
    entry: dict[str, Any],
    *,
    target: str,
    queue_id: str,
    run_id: str,
    direct_target: Path | None,
    resolved_reaction_dir: Path | None,
) -> bool:
    entry_queue_id = normalize_text(entry.get("queue_id"))
    entry_task_id = normalize_text(entry.get("task_id"))
    entry_run_id = normalize_text(queue_entry_metadata_value(entry, "run_id"))
    entry_reaction_dir = resolve_existing_job_dir(queue_entry_metadata_value(entry, "reaction_dir"))

    return (
        (bool(queue_id) and entry_queue_id == queue_id)
        or (bool(target) and entry_queue_id == target)
        or (bool(target) and entry_task_id == target)
        or (bool(run_id) and entry_run_id == run_id)
        or (bool(target) and entry_run_id == target)
        or (resolved_reaction_dir is not None and entry_reaction_dir == resolved_reaction_dir)
        or (direct_target is not None and entry_reaction_dir == direct_target)
    )


def _find_queue_entry(
    *,
    index_root: Path,
    target: str,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> dict[str, Any] | None:
    entries = load_json_list(index_root / QUEUE_FILE_NAME)
    if not entries:
        return None

    direct_target = resolve_existing_job_dir(target)
    resolved_reaction_dir = resolve_existing_job_dir(reaction_dir)

    for entry in reversed(entries):
        if _queue_entry_matches(
            entry,
            target=target,
            queue_id=queue_id,
            run_id=run_id,
            direct_target=direct_target,
            resolved_reaction_dir=resolved_reaction_dir,
        ):
            return dict(entry)
    return None


def resolve_latest_job_dir(index_root: str | Path, target: str) -> Path | None:
    return _artifacts.resolve_latest_job_dir(index_root, target)


def load_job_artifact_context(
    index_root: str | Path,
    target: str,
) -> JobArtifactContext:
    return _artifacts.load_job_artifact_context(index_root, target)


def load_job_runtime_context(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> JobRuntimeContext:
    return _load_job_runtime_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=_job_location_deps(),
    )


def _load_job_runtime_context(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
    deps: _JobLocationDeps,
) -> JobRuntimeContext:
    return _runtime_context.load_job_runtime_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=deps,
    )


def _runtime_paths(current_dir: Path | None) -> dict[str, str]:
    return _contract_payload.runtime_paths(
        current_dir,
        state_file_name=STATE_FILE_NAME,
        report_json_name=REPORT_JSON_NAME,
        report_md_name=REPORT_MD_NAME,
    )


def _orca_contract_resolved_fields(
    *,
    runtime: JobRuntimeContext,
    payloads: _contract_payload.RuntimePayloads,
    current_dir: Path | None,
    target: str,
    run_id: str,
    deps: _JobLocationDeps,
) -> _OrcaContractResolvedFields:
    return _contract_context.resolved_contract_fields(
        runtime=runtime,
        payloads=payloads,
        current_dir=current_dir,
        target=target,
        run_id=run_id,
        deps=deps,
    )


def _orca_contract_payload_context(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
    deps: _JobLocationDeps,
) -> OrcaContractPayloadContext:
    runtime = _load_job_runtime_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=deps,
    )
    return _contract_context.payload_context_from_runtime(
        runtime=runtime,
        target=target,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=deps,
        resolved_fields_fn=_orca_contract_resolved_fields,
    )


def load_orca_contract_payload(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
) -> dict[str, Any]:
    deps = _job_location_deps()
    ctx = _orca_contract_payload_context(
        index_root,
        target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=deps,
    )
    return _contract_context.payload_from_context(ctx, queue_id=queue_id, deps=deps)


def load_job_artifacts(
    index_root: str | Path,
    target: str,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    return _artifacts.load_job_artifacts(index_root, target)
