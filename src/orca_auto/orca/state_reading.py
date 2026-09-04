"""Read-only access and provenance verification for ORCA state artifacts."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import (
    MAX_RUN_ARTIFACT_JSON_BYTES,
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
)
from orca_auto.core.engine_process import read_confined_text, require_confined_regular_file
from orca_auto.core.machine_observation import (
    artifact_receipt,
    results_payload_from_observation,
    verify_available_artifacts,
)
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils.persistence import load_json_mapping_file

from .types import RunFinalResult, RunState

STATE_FILE_NAME = RUN_STATE_FILE
REPORT_JSON_NAME = RUN_REPORT_JSON_FILE


def state_path(reaction_dir: Path) -> Path:
    return reaction_dir / STATE_FILE_NAME


def report_json_path(reaction_dir: Path) -> Path:
    return reaction_dir / REPORT_JSON_NAME


def state_payload_job_id(payload: Any) -> str:
    """Read the generation job id from legacy or normalized ORCA state."""

    if not isinstance(payload, dict):
        return ""
    job = payload.get("job")
    job = job if isinstance(job, dict) else {}
    return str(payload.get("job_id") or job.get("id") or "").strip()


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    return load_json_mapping_file(path)


def normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _selected_input_text(payload: Mapping[str, Any]) -> str:
    selected = normalized_text(payload.get("selected_inp"))
    if selected:
        return selected
    input_payload = payload.get("input")
    if isinstance(input_payload, Mapping):
        return normalized_text(input_payload.get("primary_path"))
    return ""


def _execution_provenance(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    provenance = payload.get("execution_provenance")
    if isinstance(provenance, Mapping):
        return provenance
    engine_payload = payload.get("engine_payload")
    if isinstance(engine_payload, Mapping):
        provenance = engine_payload.get("execution_provenance")
        if isinstance(provenance, Mapping):
            return provenance
    return {}


def verified_generation_artifact_target(
    reaction_dir: Path,
    payload: Mapping[str, Any],
) -> tuple[Path, tuple[int, int]] | None:
    selected_text = _selected_input_text(payload)
    provenance = _execution_provenance(payload)
    execution_dir_text = normalized_text(provenance.get("execution_dir"))
    raw_identity = provenance.get("execution_dir_identity")
    bound_selected_identity = provenance.get("bound_selected_identity")
    generation_owner_token = normalized_text(provenance.get("generation_owner_token"))
    if (
        not selected_text
        or not execution_dir_text
        or not generation_owner_token
        or not isinstance(raw_identity, Mapping)
        or not isinstance(bound_selected_identity, Mapping)
    ):
        return None
    try:
        device = int(raw_identity.get("device", -1))
        inode = int(raw_identity.get("inode", -1))
        resolved_reaction_dir = reaction_dir.expanduser().resolve(strict=True)
        raw_generation_dir = Path(execution_dir_text).expanduser()
        generation_dir = raw_generation_dir.resolve(strict=True)
        generation_status = raw_generation_dir.lstat()
        reaction_status = resolved_reaction_dir.stat()
        raw_selected = Path(selected_text).expanduser()
        selected = require_confined_regular_file(
            generation_dir,
            raw_selected,
            label="ORCA generation selected input",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        device < 0
        or inode <= 0
        or not raw_generation_dir.is_absolute()
        or raw_generation_dir != generation_dir
        or raw_generation_dir.is_symlink()
        or generation_dir.parent != resolved_reaction_dir
        or not is_visible_generation_name(generation_dir.name)
        or not generation_dir.is_dir()
        or raw_selected != selected
        or selected.parent != generation_dir
        or normalized_text(bound_selected_identity.get("path")) != str(selected)
        or (int(generation_status.st_dev), int(generation_status.st_ino)) != (device, inode)
    ):
        return None
    try:
        require_direct_generation_owner(
            resolved_reaction_dir,
            namespace=generation_dir.name,
            expected_job_identity=(
                int(reaction_status.st_dev),
                int(reaction_status.st_ino),
            ),
            expected_generation_identity=(device, inode),
            owner_token=generation_owner_token,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return generation_dir, (device, inode)


def state_from_normalized_payload(payload: dict[str, Any]) -> RunState | None:
    if int(payload.get("schema_version", 0) or 0) != 1:
        return None
    if normalized_text(payload.get("engine")) != "orca":
        return None
    job = _dict(payload.get("job"))
    status = _dict(payload.get("status"))
    input_payload = _dict(payload.get("input"))
    timestamps = _dict(payload.get("timestamps"))
    engine_payload = _dict(payload.get("engine_payload"))
    state: RunState = {
        "job_id": normalized_text(job.get("id")),
        "queue_id": normalized_text(job.get("queue_id")),
        "queue_generation": normalized_text(job.get("generation")),
        "run_id": normalized_text(engine_payload.get("run_id")),
        "reaction_dir": normalized_text(job.get("dir")),
        "selected_inp": normalized_text(input_payload.get("primary_path")),
        "max_retries": int(engine_payload.get("max_retries", 0) or 0),
        "status": normalized_text(status.get("state")),
        "started_at": normalized_text(timestamps.get("started_at")),
        "updated_at": normalized_text(timestamps.get("updated_at")),
        "attempts": list(engine_payload.get("attempts") or []),
        "scratch_publications": list(engine_payload.get("scratch_publications") or []),
        "execution_provenance": _dict(engine_payload.get("execution_provenance")),
        "final_result": cast(RunFinalResult | None, engine_payload.get("final_result")),
    }
    return state


def load_state(reaction_dir: Path) -> RunState | None:
    raw = _load_json_dict(state_path(reaction_dir))
    if raw is None:
        return None
    return state_from_normalized_payload(raw)


def load_generation_state(
    generation_dir: Path,
) -> tuple[dict[str, Any], RunState] | None:
    """Load a state artifact without following links outside its generation."""

    raw_generation_dir = generation_dir.expanduser()
    if (
        not raw_generation_dir.is_absolute()
        or raw_generation_dir.is_symlink()
        or not is_visible_generation_name(raw_generation_dir.name)
    ):
        return None
    try:
        resolved_generation_dir = raw_generation_dir.resolve(strict=True)
        before = resolved_generation_dir.stat()
        if raw_generation_dir != resolved_generation_dir or not stat.S_ISDIR(before.st_mode):
            return None
        payload = json.loads(
            read_confined_text(
                resolved_generation_dir,
                state_path(resolved_generation_dir),
                label="ORCA generation state",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
        )
        after = resolved_generation_dir.stat()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or (before.st_dev, before.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        return None
    normalized = state_from_normalized_payload(payload)
    if normalized is None:
        return None
    return payload, normalized


def machine_lifecycle(status: str) -> tuple[str, str]:
    normalized = status.strip().lower()
    if normalized in {"created", "pending", "queued"}:
        return "queued", "pending"
    if normalized in {"running", "retrying"}:
        return "running", "pending"
    if normalized == "completed":
        return "finished", "succeeded"
    if normalized == "cancelled":
        return "finished", "cancelled"
    if normalized == "failed":
        return "finished", "failed"
    return "finished", "uncertain"


def load_report_json(
    generation_dir: Path,
    *,
    require_consumable_success: bool = False,
) -> dict[str, Any] | None:
    """Load one provenance-verified ORCA report from an exact visible generation."""

    loaded = load_report_json_with_output_receipt(
        generation_dir,
        require_consumable_success=require_consumable_success,
    )
    return None if loaded is None else loaded[0]


def load_report_json_with_output_receipt(
    generation_dir: Path,
    *,
    require_consumable_success: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Load one verified report together with the ``orca-output`` receipt it accepted.

    The second element is the receipt this load re-hashed from disk and found
    equal to the one the machine observation records, so a reader that must
    prove it read the observed bytes can compare its own digest against it
    instead of re-deriving one from a file it opened later. It is ``None`` when
    the generation records no terminal output, and carries a ``missing`` or
    ``invalid`` status when the recorded output is not a file the observation
    could bind — the caller decides what an unavailable receipt means for it.
    """

    raw_generation_dir = generation_dir.expanduser()
    if (
        not raw_generation_dir.is_absolute()
        or raw_generation_dir.is_symlink()
        or not is_visible_generation_name(raw_generation_dir.name)
    ):
        return None
    report_path = report_json_path(raw_generation_dir)
    try:
        resolved_generation_dir = raw_generation_dir.resolve(strict=True)
        generation_before = resolved_generation_dir.stat()
        before = report_path.lstat()
        observation = json.loads(
            read_confined_text(
                resolved_generation_dir,
                report_path,
                label="ORCA generation report",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        raw_generation_dir != resolved_generation_dir
        or not stat.S_ISDIR(generation_before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not isinstance(observation, dict)
    ):
        return None
    result_data = results_payload_from_observation(observation)
    if result_data is None:
        return None
    if (
        observation.get("producer", {}).get("name") != "orca_auto"
        or observation.get("operation", {}).get("kind") != "chemistry/orca-run"
        or result_data.get("result_kind") != "engine-run"
        or result_data.get("engine") != "orca"
        or not verify_available_artifacts(observation, resolved_generation_dir)
    ):
        return None
    loaded_state = load_generation_state(resolved_generation_dir)
    if loaded_state is None:
        return None
    payload, _state = loaded_state
    job = _dict(payload.get("job"))
    status = _dict(payload.get("status"))
    input_payload = _dict(payload.get("input"))
    engine_payload = _dict(payload.get("engine_payload"))
    final_result = _dict(engine_payload.get("final_result"))
    summary = _dict(result_data.get("summary"))
    results = _dict(result_data.get("results"))
    operation_id = normalized_text(observation.get("operation", {}).get("id"))
    expected_phase, expected_outcome = machine_lifecycle(normalized_text(status.get("state")))
    lifecycle = _dict(observation.get("lifecycle"))
    attempts = engine_payload.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0
    if (
        operation_id
        not in {
            normalized_text(job.get("id")),
            normalized_text(engine_payload.get("run_id")),
        }
        or lifecycle.get("phase") != expected_phase
        or lifecycle.get("outcome") != expected_outcome
        or summary.get("status") != normalized_text(status.get("state"))
        or summary.get("reason")
        != normalized_text(final_result.get("reason") or status.get("reason"))
        or summary.get("analyzer_status") != normalized_text(final_result.get("analyzer_status"))
        or results.get("run_id") != normalized_text(engine_payload.get("run_id"))
        or results.get("attempt_count") != attempt_count
        or results.get("max_retries") != int(engine_payload.get("max_retries", 0) or 0)
        or results.get("resumed") != bool(final_result.get("resumed", False))
        or results.get("skipped_execution") != bool(final_result.get("skipped_execution", False))
        or results.get("runner_error") != normalized_text(final_result.get("runner_error"))
    ):
        return None
    artifacts = observation.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    expected_input = artifact_receipt(
        resolved_generation_dir,
        Path(normalized_text(input_payload.get("primary_path"))),
        required=True,
        role="source",
        media_type="text/plain",
    )
    if artifacts.get("input") != expected_input:
        return None
    outcome = expected_outcome
    last_out_path = normalized_text(final_result.get("last_out_path"))
    accepted_output_receipt: dict[str, Any] | None = None
    if last_out_path or outcome == "succeeded":
        expected_output = artifact_receipt(
            resolved_generation_dir,
            Path(last_out_path) if last_out_path else None,
            required=outcome == "succeeded",
            role="log",
            media_type="text/plain",
        )
        if artifacts.get("orca-output") != expected_output:
            return None
        accepted_output_receipt = expected_output
    if require_consumable_success and outcome == "succeeded":
        handoff = _dict(observation.get("handoff"))
        delivery = _dict(observation.get("delivery"))
        if handoff.get("status") != "ready" or delivery.get("status") != "complete":
            return None
    target = verified_generation_artifact_target(resolved_generation_dir.parent, payload)
    if target is None or target[0] != resolved_generation_dir:
        return None
    provenance = _execution_provenance(payload)
    bound_selected_identity = provenance.get("bound_selected_identity")
    selected_text = _selected_input_text(payload)
    if not isinstance(bound_selected_identity, Mapping) or not selected_text:
        return None
    try:
        selected = require_confined_regular_file(
            resolved_generation_dir,
            Path(selected_text).expanduser(),
            label="ORCA report selected input",
        )
        if _engine_runner.executable_identity(selected) != dict(bound_selected_identity):
            return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    try:
        after = report_path.lstat()
        generation_details = resolved_generation_dir.stat()
    except OSError:
        return None
    if (
        (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or (
            int(generation_before.st_dev),
            int(generation_before.st_ino),
        )
        != (
            int(generation_details.st_dev),
            int(generation_details.st_ino),
        )
        or target[1] != (int(generation_details.st_dev), int(generation_details.st_ino))
    ):
        return None
    return payload, accepted_output_receipt


__all__ = [
    "REPORT_JSON_NAME",
    "STATE_FILE_NAME",
    "load_generation_state",
    "load_report_json",
    "load_report_json_with_output_receipt",
    "load_state",
    "machine_lifecycle",
    "normalized_text",
    "report_json_path",
    "state_from_normalized_payload",
    "state_path",
    "state_payload_job_id",
    "verified_generation_artifact_target",
]
