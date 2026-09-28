"""Read-only access and provenance verification for ORCA state artifacts."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from orca_auto.core.artifacts import (
    MAX_RUN_ARTIFACT_JSON_BYTES,
    RUN_STATE_FILE,
)
from orca_auto.core.confined_io import read_confined_text
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.generation_owner import require_direct_generation_owner
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils.persistence import load_json_mapping_file

from .generation_validation import (
    require_bound_generation_directory,
    require_generation_selected_input,
)
from .types import RunFinalResult, RunState

STATE_FILE_NAME = RUN_STATE_FILE


def state_path(reaction_dir: Path) -> Path:
    return reaction_dir / STATE_FILE_NAME


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
        generation_dir = require_bound_generation_directory(
            resolved_reaction_dir, raw_generation_dir, (device, inode)
        )
        reaction_status = resolved_reaction_dir.stat()
        raw_selected = Path(selected_text).expanduser()
        selected = require_generation_selected_input(
            generation_dir,
            raw_selected,
            label="ORCA generation selected input",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        device < 0
        or inode <= 0
        or normalized_text(bound_selected_identity.get("path")) != str(selected)
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


def payload_matches_expected_job_id(payload: Any, expected_job_id: str | None) -> bool:
    expected = str(expected_job_id or "").strip()
    return not expected or state_payload_job_id(payload) == expected


def get_run_id_from_state(
    reaction_dir: str,
    *,
    expected_job_id: str | None = None,
) -> str | None:
    """Try to read run_id from the reaction_dir's job_state.json."""
    state = load_state(Path(reaction_dir))
    if state and payload_matches_expected_job_id(state, expected_job_id):
        return state.get("run_id")
    return None


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


__all__ = [
    "STATE_FILE_NAME",
    "get_run_id_from_state",
    "load_generation_state",
    "load_state",
    "normalized_text",
    "payload_matches_expected_job_id",
    "state_from_normalized_payload",
    "state_path",
    "state_payload_job_id",
    "verified_generation_artifact_target",
]
