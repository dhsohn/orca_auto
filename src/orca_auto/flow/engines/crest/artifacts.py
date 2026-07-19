from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.flow.engines.crest.runner import CrestRunResult
from orca_auto.flow.engines.crest.state import (
    is_recovery_pending,
    load_state,
    state_matches_job,
    write_state,
)


def _engine_fields(entry: Any, result: CrestRunResult) -> dict[str, Any]:
    return {
        "molecule_key": _engine_execution.entry_metadata_text(entry, "molecule_key"),
        "mode": result.mode,
        "execution_provenance": _engine_execution.sanitized_execution_provenance(entry),
    }


def _detail_fields(result: CrestRunResult) -> dict[str, Any]:
    return {
        "retained_conformer_count": result.retained_conformer_count,
        "retained_conformer_paths": list(result.retained_conformer_paths),
        "output_identities": dict(result.output_identities),
        "scratch_provenance": dict(result.scratch_provenance),
    }


def _result_artifact_fields(
    entry: Any,
    result: CrestRunResult,
) -> _engine_execution.EngineArtifactFields:
    return _engine_execution.EngineArtifactFields(
        selected_input_xyz=result.selected_input_xyz,
        engine="crest",
        engine_fields=_engine_fields(entry, result),
        detail_fields=_detail_fields(result),
    )


def _running_artifact_fields(entry: Any) -> _engine_execution.EngineArtifactFields:
    return _engine_execution.EngineArtifactFields(
        selected_input_xyz=_engine_execution.entry_metadata_text(
            entry,
            "selected_input_xyz",
        ),
        engine="crest",
        engine_fields={
            "molecule_key": _engine_execution.entry_metadata_text(entry, "molecule_key"),
            "mode": _engine_execution.entry_metadata_text(entry, "mode", "standard"),
        },
    )


def matching_result_state(
    entry: Any,
    result: CrestRunResult,
    job_dir: Path,
    *,
    load_state_fn: Any = load_state,
    state_matches_job_fn: Any = state_matches_job,
) -> dict[str, Any]:
    return _queue_execution.load_matching_state(
        job_dir,
        load_state_fn=load_state_fn,
        state_matches_job_fn=state_matches_job_fn,
        match_kwargs={
            "selected_input_xyz": result.selected_input_xyz,
            "mode": result.mode,
            "molecule_key": _engine_execution.entry_metadata_text(entry, "molecule_key"),
        },
    )


def write_execution_artifacts(
    entry: Any,
    result: CrestRunResult,
    *,
    load_state_fn: Any = load_state,
    state_matches_job_fn: Any = state_matches_job,
    write_state_fn: Any = write_state,
) -> None:
    job_dir_text = _engine_execution.entry_metadata_text(entry, "job_dir")
    if not job_dir_text:
        return

    job_dir = Path(job_dir_text).expanduser().resolve()
    previous_state = matching_result_state(
        entry,
        result,
        job_dir,
        load_state_fn=load_state_fn,
        state_matches_job_fn=state_matches_job_fn,
    )
    base_state = _queue_execution.coerce_mapping(previous_state)
    _engine_execution.write_terminal_engine_artifacts(
        entry,
        result,
        job_dir_text=job_dir_text,
        previous_state=base_state,
        resumed=bool(base_state.get("resumed", False)),
        artifact_fields=_result_artifact_fields(entry, result),
        write_state_fn=write_state_fn,
    )


def depsafe_now_utc_iso() -> str:
    from orca_auto.core.utils import now_utc_iso as dynamic_now_utc_iso

    return dynamic_now_utc_iso()


def resource_caps(cfg: Any) -> dict[str, int]:
    return _engine_execution.default_engine_resource_caps(cfg)


def entry_resource_request(cfg: Any, entry: Any) -> dict[str, int]:
    return _engine_execution.default_entry_resource_request(cfg, entry)


def write_running_state(
    cfg: Any,
    entry: Any,
    *,
    load_state_fn: Any = load_state,
    state_matches_job_fn: Any = state_matches_job,
    is_recovery_pending_fn: Any = is_recovery_pending,
    write_state_fn: Any = write_state,
    now_utc_iso_fn: Any = depsafe_now_utc_iso,
) -> None:
    job_dir_text = _engine_execution.entry_metadata_text(entry, "job_dir")
    if not job_dir_text:
        return
    job_dir = Path(job_dir_text).expanduser().resolve()
    resource_request = entry_resource_request(cfg, entry)
    previous_state = _queue_execution.load_matching_state(
        job_dir,
        load_state_fn=load_state_fn,
        state_matches_job_fn=state_matches_job_fn,
        match_kwargs={
            "selected_input_xyz": _engine_execution.entry_metadata_text(
                entry,
                "selected_input_xyz",
            ),
            "mode": _engine_execution.entry_metadata_text(entry, "mode", "standard"),
            "molecule_key": _engine_execution.entry_metadata_text(entry, "molecule_key"),
        },
    )
    resumed = bool(previous_state) and _engine_execution.is_resumed_state(
        previous_state,
        is_recovery_pending_fn=is_recovery_pending_fn,
    )
    started_at = entry.started_at or now_utc_iso_fn()
    updated_at = now_utc_iso_fn()
    _engine_execution.write_running_engine_state_artifact(
        entry,
        job_dir_text=job_dir_text,
        started_at=started_at,
        updated_at=updated_at,
        previous_state=previous_state,
        resumed=resumed,
        resource_request=resource_request,
        write_state_fn=write_state_fn,
        artifact_fields=_running_artifact_fields(entry),
    )


def build_terminal_result(
    entry: Any,
    *,
    job_dir: Path,
    selected_xyz: Path,
    mode: str,
    resource_request: dict[str, int],
    status: str,
    reason: str,
    exit_code: int = 1,
    command: tuple[str, ...] = (),
    now_utc_iso_fn: Any = depsafe_now_utc_iso,
) -> CrestRunResult:
    return _engine_execution.build_terminal_result(
        CrestRunResult,
        entry,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        log_prefix="crest",
        manifest_filename="crest_job.yaml",
        resource_request=resource_request,
        status=status,
        reason=reason,
        now_utc_iso_fn=now_utc_iso_fn,
        command=command,
        exit_code=exit_code,
        engine_fields={"mode": mode},
        detail_fields={
            "retained_conformer_count": 0,
            "retained_conformer_paths": (),
        },
    )


__all__ = [
    "build_terminal_result",
    "entry_resource_request",
    "matching_result_state",
    "resource_caps",
    "write_execution_artifacts",
    "write_running_state",
]
