from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import RUN_REPORT_JSON_FILE, RUN_STATE_FILE
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.core.queue.engine.snapshot_intent import SNAPSHOT_INTENT_TOKEN_KEY
from orca_auto.core.queue.generation import is_visible_generation_name

from ..execution_binding import (
    ORCA_EXECUTION_SNAPSHOT_VERSION,
    orca_execution_provenance,
    orca_execution_snapshot_generation_dir,
)
from ._generation import payload_matches_queue_generation
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


@dataclass(frozen=True)
class _ExecutionGeneration:
    job_dir: Path
    artifact_dir: Path


@dataclass(frozen=True)
class _ExecutionGenerationClaim:
    required: bool
    generation: _ExecutionGeneration | None = None
    provenance: dict[str, Any] | None = None
    payload_required: bool = False


@dataclass(frozen=True)
class _LoadedGenerationPayload:
    payload: dict[str, Any] | None
    invalid: bool = False


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


def _strict_identity(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    device = value.get("device")
    inode = value.get("inode")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode < 0
    ):
        return None
    return device, inode


def _requires_schema_two_generation(inputs: _RuntimeInputs, *, deps: Any) -> bool:
    snapshot = deps.queue_entry_metadata_value(inputs.queue_entry, "execution_snapshot")
    return (
        isinstance(snapshot, Mapping) and snapshot.get("version") == ORCA_EXECUTION_SNAPSHOT_VERSION
    )


def _execution_generation(inputs: _RuntimeInputs, *, deps: Any) -> _ExecutionGeneration | None:
    queue_entry = inputs.queue_entry
    snapshot = deps.queue_entry_metadata_value(queue_entry, "execution_snapshot")
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("version") != ORCA_EXECUTION_SNAPSHOT_VERSION
    ):
        return None

    reaction_text = deps.normalize_text(
        deps.queue_entry_metadata_value(queue_entry, "reaction_dir")
    )
    try:
        raw_job_dir = Path(reaction_text).expanduser()
        if not reaction_text or not raw_job_dir.is_absolute() or raw_job_dir.is_symlink():
            return None
        job_dir = raw_job_dir.resolve(strict=True)
        if raw_job_dir != job_dir or not job_dir.is_dir():
            return None
        job_dir.relative_to(inputs.index_root)
    except (OSError, RuntimeError, ValueError):
        return None

    job_identity = _strict_identity(snapshot.get("job_dir_identity"))
    generation_identity = _strict_identity(snapshot.get("execution_dir_identity"))
    bound_selected_identity = snapshot.get("bound_selected_identity")
    generation_owner_token = deps.normalize_text(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY))
    if (
        job_identity is None
        or generation_identity is None
        or not generation_owner_token
        or not isinstance(bound_selected_identity, Mapping)
    ):
        return None
    try:
        job_details = job_dir.stat()
        if (int(job_details.st_dev), int(job_details.st_ino)) != job_identity:
            return None
        generation_name = deps.normalize_text(snapshot.get("generation_name"))
        raw_execution_dir = Path(deps.normalize_text(snapshot.get("execution_dir"))).expanduser()
        expected_execution_dir = job_dir / generation_name
        if raw_execution_dir != expected_execution_dir:
            return None
        artifact_dir = orca_execution_snapshot_generation_dir(job_dir, snapshot)
        if artifact_dir != expected_execution_dir:
            return None
        generation_details = artifact_dir.stat()
        if (int(generation_details.st_dev), int(generation_details.st_ino)) != generation_identity:
            return None
        require_direct_generation_owner(
            job_dir,
            namespace=generation_name,
            expected_job_identity=job_identity,
            expected_generation_identity=generation_identity,
            owner_token=generation_owner_token,
        )
        selected_path_text = deps.normalize_text(bound_selected_identity.get("path"))
        raw_selected = Path(selected_path_text).expanduser()
        selected = require_confined_regular_file(
            artifact_dir,
            raw_selected,
            label="Historical ORCA selected input",
        )
        queue_selected = deps.normalize_text(
            deps.queue_entry_metadata_value(queue_entry, "selected_inp")
        )
        snapshot_selected = deps.normalize_text(snapshot.get("selected_inp"))
        if (
            not selected_path_text
            or raw_selected != selected
            or selected.parent != artifact_dir
            or selected.suffix.lower() != ".inp"
            or (queue_selected and queue_selected != str(selected))
            or (snapshot_selected and snapshot_selected != str(selected))
            or _engine_runner.executable_identity(selected) != dict(bound_selected_identity)
        ):
            return None
        final_job_details = job_dir.stat()
        if (int(final_job_details.st_dev), int(final_job_details.st_ino)) != job_identity:
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return _ExecutionGeneration(job_dir=job_dir, artifact_dir=artifact_dir)


def _payload_execution_provenance(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    provenance = payload.get("execution_provenance")
    if isinstance(provenance, Mapping):
        return dict(provenance)
    engine_payload = payload.get("engine_payload")
    if isinstance(engine_payload, Mapping):
        provenance = engine_payload.get("execution_provenance")
        if isinstance(provenance, Mapping):
            return dict(provenance)
    return None


def _payload_selected_input(payload: Any, *, deps: Any) -> str:
    if not isinstance(payload, Mapping):
        return ""
    selected = deps.normalize_text(payload.get("selected_inp"))
    if selected:
        return selected
    input_payload = payload.get("input")
    if isinstance(input_payload, Mapping):
        return deps.normalize_text(input_payload.get("primary_path"))
    return ""


def _artifact_visible_provenance(artifact: Any) -> tuple[bool, dict[str, Any] | None]:
    claims: list[dict[str, Any]] = []
    for payload in (getattr(artifact, "state", None), getattr(artifact, "report", None)):
        provenance = _payload_execution_provenance(payload)
        if provenance is None:
            continue
        if (
            provenance.get("execution_dir_identity") is not None
            or provenance.get("generation_owner_token") is not None
        ):
            claims.append(provenance)
    if not claims:
        return False, None
    first = claims[0]
    if any(claim != first for claim in claims[1:]):
        return True, None
    return True, first


def _provenance_execution_generation(
    inputs: _RuntimeInputs,
    artifact: Any,
    provenance: Mapping[str, Any],
    *,
    deps: Any,
) -> _ExecutionGeneration | None:
    generation_identity = _strict_identity(provenance.get("execution_dir_identity"))
    bound_selected_identity = provenance.get("bound_selected_identity")
    owner_token = deps.normalize_text(provenance.get("generation_owner_token"))
    if (
        generation_identity is None
        or not isinstance(bound_selected_identity, Mapping)
        or not owner_token
    ):
        return None
    try:
        raw_execution_dir = Path(deps.normalize_text(provenance.get("execution_dir"))).expanduser()
        if (
            not raw_execution_dir.is_absolute()
            or raw_execution_dir.is_symlink()
            or not is_visible_generation_name(raw_execution_dir.name)
        ):
            return None
        artifact_dir = raw_execution_dir.resolve(strict=True)
        if raw_execution_dir != artifact_dir or not artifact_dir.is_dir():
            return None
        job_dir = artifact_dir.parent
        job_dir.relative_to(inputs.index_root)
        artifact_job_dir = getattr(artifact, "job_dir", None)
        if artifact_job_dir is not None:
            resolved_artifact_job_dir = Path(artifact_job_dir).expanduser().resolve(strict=True)
            if resolved_artifact_job_dir not in {job_dir, artifact_dir}:
                return None
        job_details = job_dir.stat()
        generation_details = artifact_dir.stat()
        if (
            int(generation_details.st_dev),
            int(generation_details.st_ino),
        ) != generation_identity:
            return None
        job_identity = (int(job_details.st_dev), int(job_details.st_ino))
        require_direct_generation_owner(
            job_dir,
            namespace=artifact_dir.name,
            expected_job_identity=job_identity,
            expected_generation_identity=generation_identity,
            owner_token=owner_token,
        )
        selected_path_text = deps.normalize_text(bound_selected_identity.get("path"))
        raw_selected = Path(selected_path_text).expanduser()
        selected = require_confined_regular_file(
            artifact_dir,
            raw_selected,
            label="Visible ORCA provenance selected input",
        )
        if (
            not selected_path_text
            or raw_selected != selected
            or selected.parent != artifact_dir
            or selected.suffix.lower() != ".inp"
            or _engine_runner.executable_identity(selected) != dict(bound_selected_identity)
        ):
            return None
        for payload in (getattr(artifact, "state", None), getattr(artifact, "report", None)):
            payload_selected = _payload_selected_input(payload, deps=deps)
            if payload_selected and payload_selected != str(selected):
                return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return _ExecutionGeneration(job_dir=job_dir, artifact_dir=artifact_dir)


def _execution_generation_claim(
    inputs: _RuntimeInputs,
    artifact: Any,
    *,
    deps: Any,
) -> _ExecutionGenerationClaim:
    if _requires_schema_two_generation(inputs, deps=deps):
        snapshot = deps.queue_entry_metadata_value(inputs.queue_entry, "execution_snapshot")
        try:
            canonical_provenance = (
                orca_execution_provenance(snapshot) if isinstance(snapshot, Mapping) else None
            )
        except (TypeError, ValueError):
            canonical_provenance = None
        return _ExecutionGenerationClaim(
            required=True,
            generation=(
                _execution_generation(inputs, deps=deps)
                if canonical_provenance is not None
                else None
            ),
            provenance=canonical_provenance,
        )
    required, provenance = _artifact_visible_provenance(artifact)
    if not required:
        return _ExecutionGenerationClaim(required=False)
    if provenance is None:
        return _ExecutionGenerationClaim(required=True)
    return _ExecutionGenerationClaim(
        required=True,
        generation=_provenance_execution_generation(
            inputs,
            artifact,
            provenance,
            deps=deps,
        ),
        provenance=provenance,
        payload_required=True,
    )


def _direct_file_identity(path: Path, parent: Path) -> tuple[int, int, int, int] | None:
    try:
        details = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        if resolved.parent != parent:
            return None
    except (OSError, RuntimeError):
        return None
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _load_generation_payload(
    inputs: _RuntimeInputs,
    generation: _ExecutionGeneration,
    *,
    source_artifact: Any,
    file_name: str,
    loader: Any,
    deps: Any,
) -> _LoadedGenerationPayload:
    path = generation.artifact_dir / file_name
    try:
        os.lstat(path)
    except FileNotFoundError:
        return _LoadedGenerationPayload(payload=None)
    except OSError:
        return _LoadedGenerationPayload(payload=None, invalid=True)
    before = _direct_file_identity(path, generation.artifact_dir)
    current_claim = _execution_generation_claim(inputs, source_artifact, deps=deps)
    if before is None or current_claim.generation != generation:
        return _LoadedGenerationPayload(payload=None, invalid=True)
    try:
        payload = loader(generation.artifact_dir)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _LoadedGenerationPayload(payload=None, invalid=True)
    after = _direct_file_identity(path, generation.artifact_dir)
    final_claim = _execution_generation_claim(inputs, source_artifact, deps=deps)
    if before != after or final_claim.generation != generation or not isinstance(payload, dict):
        return _LoadedGenerationPayload(payload=None, invalid=True)
    return _LoadedGenerationPayload(payload=dict(payload))


def _requested_run_matches_generation(
    inputs: _RuntimeInputs,
    artifact: Any,
    *,
    deps: Any,
) -> bool:
    requested_run_id = deps.normalize_text(inputs.run_id)
    queue_run_id = deps.normalize_text(
        deps.queue_entry_metadata_value(inputs.queue_entry, "run_id")
    )
    if not requested_run_id or queue_run_id:
        return True
    if not _requires_schema_two_generation(inputs, deps=deps):
        return False

    queue_entry = dict(inputs.queue_entry or {})
    raw_metadata = queue_entry.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
    metadata["run_id"] = requested_run_id
    queue_entry["metadata"] = metadata
    payloads = [
        payload for payload in (artifact.state, artifact.report) if isinstance(payload, dict)
    ]
    return bool(payloads) and all(
        payload_matches_queue_generation(queue_entry, payload) for payload in payloads
    )


def _direct_generation_artifact(
    generation_dir: Path,
    path_text: str,
    *,
    suffix: str,
) -> bool:
    if not path_text:
        return True
    try:
        raw_path = Path(path_text).expanduser()
        resolved = require_confined_regular_file(
            generation_dir,
            raw_path,
            label="Historical ORCA artifact",
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        raw_path.is_absolute()
        and raw_path == resolved
        and resolved.parent == generation_dir
        and resolved.suffix.lower() == suffix
    )


def _generation_payload_paths_are_confined(
    payload: dict[str, Any],
    generation: _ExecutionGeneration,
    provenance: Mapping[str, Any],
    *,
    deps: Any,
) -> bool:
    generation_dir = generation.artifact_dir
    bound_selected = provenance.get("bound_selected_identity")
    if not isinstance(bound_selected, Mapping):
        return False
    expected_selected = deps.normalize_text(bound_selected.get("path"))
    payload_selected = _payload_selected_input(payload, deps=deps)
    if payload_selected != expected_selected or not _direct_generation_artifact(
        generation_dir,
        payload_selected,
        suffix=".inp",
    ):
        return False

    engine_payload = payload.get("engine_payload")
    normalized_engine_payload = engine_payload if isinstance(engine_payload, Mapping) else {}
    raw_attempts = payload.get("attempts")
    if raw_attempts is None:
        raw_attempts = normalized_engine_payload.get("attempts")
    if raw_attempts is not None:
        if not isinstance(raw_attempts, list):
            return False
        for attempt in raw_attempts:
            if not isinstance(attempt, Mapping):
                return False
            for key, suffix in (("inp_path", ".inp"), ("out_path", ".out")):
                path_text = deps.normalize_text(attempt.get(key))
                if path_text and not _direct_generation_artifact(
                    generation_dir,
                    path_text,
                    suffix=suffix,
                ):
                    return False

    final_result = payload.get("final_result")
    if not isinstance(final_result, Mapping):
        final_result = normalized_engine_payload.get("final_result")
    if isinstance(final_result, Mapping):
        last_out_path = deps.normalize_text(final_result.get("last_out_path"))
        if last_out_path and not _direct_generation_artifact(
            generation_dir,
            last_out_path,
            suffix=".out",
        ):
            return False
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, Mapping):
        last_out_path = deps.normalize_text(artifacts.get("last_out_path"))
        if last_out_path and not _direct_generation_artifact(
            generation_dir,
            last_out_path,
            suffix=".out",
        ):
            return False
    return True


def _historical_generation_artifact(
    inputs: _RuntimeInputs,
    artifact: Any,
    *,
    deps: Any,
) -> tuple[Any, Path | None, bool]:
    claim = _execution_generation_claim(inputs, artifact, deps=deps)
    generation = claim.generation
    if generation is None:
        if not claim.required:
            return artifact, None, False
        return (
            deps._job_artifact_context(
                record=artifact.record,
                job_dir=artifact.job_dir,
                state=None,
                report=None,
            ),
            None,
            True,
        )
    state_result = _load_generation_payload(
        inputs,
        generation,
        source_artifact=artifact,
        file_name=RUN_STATE_FILE,
        loader=deps.load_state,
        deps=deps,
    )
    report_result = _load_generation_payload(
        inputs,
        generation,
        source_artifact=artifact,
        file_name=RUN_REPORT_JSON_FILE,
        loader=deps.load_report_json,
        deps=deps,
    )
    final_claim = _execution_generation_claim(inputs, artifact, deps=deps)
    provenance_mismatch = False
    artifact_paths_invalid = False
    if claim.provenance is not None:
        loaded_payloads = [
            payload
            for payload in (state_result.payload, report_result.payload)
            if isinstance(payload, dict)
        ]
        provenance_mismatch = (claim.payload_required and not loaded_payloads) or any(
            _payload_execution_provenance(payload) != claim.provenance
            for payload in loaded_payloads
        )
        artifact_paths_invalid = any(
            not _generation_payload_paths_are_confined(
                payload,
                generation,
                claim.provenance,
                deps=deps,
            )
            for payload in loaded_payloads
        )
    if (
        state_result.invalid
        or report_result.invalid
        or final_claim.generation != generation
        or provenance_mismatch
        or artifact_paths_invalid
    ):
        return (
            deps._job_artifact_context(
                record=artifact.record,
                job_dir=artifact.job_dir,
                state=None,
                report=None,
            ),
            None,
            True,
        )
    return (
        deps._job_artifact_context(
            record=artifact.record,
            job_dir=generation.job_dir,
            state=state_result.payload,
            report=report_result.payload,
        ),
        generation.artifact_dir,
        False,
    )


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
    if inputs.queue_entry is None and (
        deps.normalize_text(inputs.queue_id) or deps.normalize_text(inputs.run_id)
    ):
        return deps.JobRuntimeContext(selector_miss=True)
    artifact = _load_initial_runtime_artifact(inputs, deps=deps)
    artifact, artifact_dir, generation_invalid = _historical_generation_artifact(
        inputs,
        artifact,
        deps=deps,
    )
    if not generation_invalid and not _requested_run_matches_generation(
        inputs,
        artifact,
        deps=deps,
    ):
        return deps.JobRuntimeContext(selector_miss=True)

    return deps.JobRuntimeContext(
        artifact=artifact,
        queue_entry=inputs.queue_entry,
        artifact_dir=artifact_dir,
        generation_invalid=generation_invalid,
    )
