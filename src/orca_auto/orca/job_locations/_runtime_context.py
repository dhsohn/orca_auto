from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._tracking import TrackedJobDirDeps
from ._tracking import matching_tracked_job_dirs as _matching_tracked_job_dirs


def matching_tracked_job_dirs(index_root: str | Path, target: str, *, deps: Any) -> list[Path]:
    return _matching_tracked_job_dirs(
        index_root,
        target,
        deps=TrackedJobDirDeps(
            normalize_text=deps.normalize_text,
            list_job_location_records=deps.list_job_location_records,
            resolve_record_job_dir=deps.resolve_record_job_dir,
            load_state=deps.load_state,
            load_report_json=deps.load_report_json,
            resolve_existing_job_dir=deps.resolve_existing_job_dir,
        ),
    )


@dataclass(frozen=True)
class _RuntimeInputs:
    index_root: Path
    target: str
    queue_id: str
    run_id: str
    reaction_dir: str
    queue_entry: dict[str, Any] | None


def _queue_reaction_dir(queue_entry: dict[str, Any] | None, *, deps: Any) -> Path | None:
    return deps.resolve_existing_job_dir(
        deps.queue_entry_metadata_value(queue_entry, "reaction_dir")
    )


def _initial_artifact_context(
    *,
    index_root: Path,
    target: str,
    run_id: str,
    reaction_dir: str,
    queue_entry: dict[str, Any] | None,
    deps: Any,
) -> Any:
    artifact = deps._first_artifact_context(index_root, (target, run_id, reaction_dir))
    queue_reaction_dir = _queue_reaction_dir(queue_entry, deps=deps)
    if artifact.job_dir is not None or queue_reaction_dir is None:
        return artifact
    return deps._first_artifact_context(
        index_root,
        (str(queue_reaction_dir), target, run_id, reaction_dir),
    )


def _hydrate_artifact_context(artifact: Any, *, deps: Any) -> Any:
    return deps._job_artifact_context(
        record=artifact.record,
        job_dir=artifact.job_dir,
        state=artifact.state,
        report=artifact.report,
    )


def _runtime_inputs(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
    deps: Any,
) -> _RuntimeInputs:
    resolved_index_root = Path(index_root).expanduser().resolve()
    queue_entry = deps._find_queue_entry(
        index_root=resolved_index_root,
        target=target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
    )
    return _RuntimeInputs(
        index_root=resolved_index_root,
        target=target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        queue_entry=queue_entry,
    )


def _load_initial_runtime_artifact(inputs: _RuntimeInputs, *, deps: Any) -> Any:
    artifact = _initial_artifact_context(
        index_root=inputs.index_root,
        target=inputs.target,
        run_id=inputs.run_id,
        reaction_dir=inputs.reaction_dir,
        queue_entry=inputs.queue_entry,
        deps=deps,
    )
    return _hydrate_artifact_context(artifact, deps=deps)


def load_job_runtime_context(
    index_root: str | Path,
    target: str,
    *,
    queue_id: str = "",
    run_id: str = "",
    reaction_dir: str = "",
    deps: Any,
) -> Any:
    inputs = _runtime_inputs(
        index_root,
        target=target,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=reaction_dir,
        deps=deps,
    )
    artifact = _load_initial_runtime_artifact(inputs, deps=deps)

    return deps.JobRuntimeContext(
        artifact=artifact,
        queue_entry=inputs.queue_entry,
    )
