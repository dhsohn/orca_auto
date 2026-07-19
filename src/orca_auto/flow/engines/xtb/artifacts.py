from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.flow.engines.xtb.runner import XtbRunResult


def _required_dependency(explicit: Any, name: str) -> Any:
    if explicit is not None:
        return explicit
    raise TypeError(f"missing required dependency: {name}")


def _candidate_paths(result: XtbRunResult) -> list[Any]:
    candidate_paths = list(result.analysis_summary.get("candidate_paths", []))
    if not candidate_paths and isinstance(result.input_summary, dict):
        candidate_paths = list(result.input_summary.get("candidate_paths", []))
    return candidate_paths


def _engine_fields(entry: Any, result: XtbRunResult) -> dict[str, Any]:
    return {
        "job_type": result.job_type,
        "reaction_key": result.reaction_key,
        "input_summary": dict(result.input_summary),
        "execution_provenance": _engine_execution.sanitized_execution_provenance(entry),
    }


def _detail_fields(result: XtbRunResult) -> dict[str, Any]:
    return {
        "candidate_count": result.candidate_count,
        "candidate_paths": _candidate_paths(result),
        "selected_candidate_paths": list(result.selected_candidate_paths),
        "candidate_details": [dict(item) for item in result.candidate_details],
        "analysis_summary": dict(result.analysis_summary),
        "output_identities": dict(result.output_identities),
        "scratch_provenance": dict(result.scratch_provenance),
    }


def _result_artifact_fields(
    entry: Any, result: XtbRunResult
) -> _engine_execution.EngineArtifactFields:
    return _engine_execution.EngineArtifactFields(
        selected_input_xyz=result.selected_input_xyz,
        engine="xtb",
        engine_fields=_engine_fields(entry, result),
        detail_fields=_detail_fields(result),
    )


def _running_artifact_fields(
    entry: Any,
    *,
    input_summary_payload: dict[str, Any],
    job_type: str,
    reaction_key: str,
) -> _engine_execution.EngineArtifactFields:
    return _engine_execution.EngineArtifactFields(
        selected_input_xyz=_engine_execution.entry_metadata_text(
            entry,
            "selected_input_xyz",
        ),
        engine="xtb",
        engine_fields={
            "job_type": job_type,
            "reaction_key": reaction_key,
            "input_summary": input_summary_payload,
        },
        detail_fields={
            "candidate_count": int(input_summary_payload.get("candidate_count", 0) or 0),
            "candidate_paths": list(input_summary_payload.get("candidate_paths", [])),
            "selected_candidate_paths": [],
            "candidate_details": [],
            "analysis_summary": {},
        },
    )


def write_execution_artifacts(
    entry: Any,
    result: XtbRunResult,
    *,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
    coerce_mapping_fn: Callable[[Any], dict[str, Any]] | None = None,
    write_state_fn: Callable[..., Any] | None = None,
) -> None:
    job_dir_text = _engine_execution.entry_metadata_text(entry, "job_dir")
    if not job_dir_text:
        return
    coerce_mapping = coerce_mapping_fn or _queue_execution.coerce_mapping
    write_state = _required_dependency(write_state_fn, "write_state_fn")

    base_state = coerce_mapping(previous_state)
    _engine_execution.write_terminal_engine_artifacts(
        entry,
        result,
        job_dir_text=job_dir_text,
        previous_state=base_state,
        resumed=resumed,
        artifact_fields=_result_artifact_fields(entry, result),
        write_state_fn=write_state,
    )


def write_running_state(
    cfg: Any,
    entry: Any,
    *,
    worker_job_pid: int | None = None,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
    input_summary_fn: Callable[[Any], dict[str, Any]] | None = None,
    entry_resource_request_fn: Callable[[Any, Any], dict[str, int]] | None = None,
    coerce_mapping_fn: Callable[[Any], dict[str, Any]] | None = None,
    now_utc_iso_fn: Callable[[], str] | None = None,
    job_type_fn: Callable[[Any], str] | None = None,
    reaction_key_fn: Callable[[Any, Path], str] | None = None,
    write_state_fn: Callable[..., Any] | None = None,
) -> None:
    job_dir_text = _engine_execution.entry_metadata_text(entry, "job_dir")
    if not job_dir_text:
        return
    input_summary = _required_dependency(input_summary_fn, "input_summary_fn")
    entry_resource_request = _required_dependency(
        entry_resource_request_fn,
        "entry_resource_request_fn",
    )
    coerce_mapping = coerce_mapping_fn or _queue_execution.coerce_mapping
    now_utc_iso = _required_dependency(now_utc_iso_fn, "now_utc_iso_fn")
    job_type = _required_dependency(job_type_fn, "job_type_fn")
    reaction_key = _required_dependency(reaction_key_fn, "reaction_key_fn")
    write_state = _required_dependency(write_state_fn, "write_state_fn")
    job_dir = Path(job_dir_text).expanduser().resolve()
    input_summary_payload = input_summary(entry)
    resource_request = entry_resource_request(cfg, entry)
    base_state = coerce_mapping(previous_state)
    started_at = entry.started_at or now_utc_iso()
    updated_at = now_utc_iso()
    job_type_value = job_type(entry)
    reaction_key_value = reaction_key(entry, job_dir)
    _engine_execution.write_running_engine_state_artifact(
        entry,
        job_dir_text=job_dir_text,
        started_at=started_at,
        updated_at=updated_at,
        previous_state=base_state,
        resumed=resumed,
        resource_request=resource_request,
        write_state_fn=write_state,
        artifact_fields=_running_artifact_fields(
            entry,
            input_summary_payload=input_summary_payload,
            job_type=job_type_value,
            reaction_key=reaction_key_value,
        ),
        worker_job_pid=worker_job_pid,
    )


def resource_caps(cfg: Any) -> dict[str, int]:
    return _engine_execution.default_engine_resource_caps(cfg)


def entry_resource_request(cfg: Any, entry: Any) -> dict[str, int]:
    return _engine_execution.default_entry_resource_request(cfg, entry)


def build_terminal_result(
    entry: Any,
    *,
    job_dir: Path,
    selected_xyz: Path,
    job_type: str,
    reaction_key: str,
    input_summary: dict[str, Any],
    resource_request: dict[str, int],
    status: str,
    reason: str,
    exit_code: int = 1,
    command: tuple[str, ...] = (),
    now_utc_iso_fn: Callable[[], str] | None = None,
) -> XtbRunResult:
    now_utc_iso = _required_dependency(now_utc_iso_fn, "now_utc_iso_fn")
    return _engine_execution.build_terminal_result(
        XtbRunResult,
        entry,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        log_prefix="xtb",
        manifest_filename="xtb_job.yaml",
        resource_request=resource_request,
        status=status,
        reason=reason,
        now_utc_iso_fn=now_utc_iso,
        command=command,
        exit_code=exit_code,
        engine_fields={
            "job_type": job_type,
            "reaction_key": reaction_key,
            "input_summary": input_summary,
        },
        detail_fields={
            "candidate_count": 0,
            "selected_candidate_paths": (),
            "candidate_details": (),
            "analysis_summary": {},
        },
    )
