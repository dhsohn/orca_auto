"""Read-only access and provenance verification for ORCA state artifacts."""

from __future__ import annotations

import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from orca_auto.core.artifacts import (
    MAX_RUN_ARTIFACT_JSON_BYTES,
    RUN_STATE_FILE,
)
from orca_auto.core.confined_io import read_confined_text
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.queue.generation_owner import require_direct_generation_owner
from orca_auto.core.utils.json_artifacts import read_json_artifact

from .generation_validation import (
    require_bound_generation_directory,
    require_generation_selected_input,
)
from .types import RunFinalResult, RunState


def state_path(reaction_dir: Path) -> Path:
    return reaction_dir / RUN_STATE_FILE


def state_payload_job_id(payload: Any) -> str:
    """Read the generation job id from legacy or normalized ORCA state."""

    if not isinstance(payload, dict):
        return ""
    job = payload.get("job")
    job = job if isinstance(job, dict) else {}
    return str(payload.get("job_id") or job.get("id") or "").strip()


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


class CorruptRunStateError(RuntimeError):
    """A ``job_state.json`` that exists but cannot be read as an ORCA state.

    Raised for a file that is not a JSON object, or for a schema-v1 ORCA state
    whose fields have the wrong types. A run never overwrites such a file: its
    content is evidence of what happened, and a reader that cannot tell must
    not guess.
    """


RunStateReadKind = Literal["valid", "missing", "unsupported", "corrupt"]


@dataclass(frozen=True)
class RunStateRead:
    """The outcome of reading one ``job_state.json``.

    ``unsupported`` is a readable JSON object that is not a schema-v1 ORCA
    state (a legacy or foreign state, which a new run may replace);
    ``corrupt`` is a file that cannot be read as one (see
    :class:`CorruptRunStateError`), with the reason in ``reason``.
    """

    kind: RunStateReadKind
    state: RunState | None = None
    reason: str = ""


def _schema_version(payload: Mapping[str, Any]) -> int:
    value = payload.get("schema_version")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise CorruptRunStateError("schema_version is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise CorruptRunStateError("schema_version is not an integer")


def _mapping_field(payload: Mapping[str, Any], key: str, *, where: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CorruptRunStateError(f"{where}{key} is not an object")
    return dict(value)


def _record_list_field(payload: Mapping[str, Any], key: str, *, where: str) -> list[Any]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise CorruptRunStateError(f"{where}{key} is not a list of objects")
    return list(value)


def _text_field(payload: Mapping[str, Any], key: str, *, where: str) -> str:
    value = payload.get(key)
    if value is None or isinstance(value, str):
        return normalized_text(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise CorruptRunStateError(f"{where}{key} is not text")


# Fields the envelope holds once, and the other places a reader could find a
# copy of each: a top-level flat key, another ``job`` key or an
# ``engine_payload`` key. The writer never writes a disagreeing copy; a state
# whose non-empty copy differs from the canonical field (an empty canonical
# field included) names two jobs (or two runs, directories, inputs or
# statuses) and is corrupt for every reader, so no reader picks one of them.
_COPIED_IDENTITY_FIELDS: tuple[tuple[tuple[str, str], tuple[tuple[str, str], ...]], ...] = (
    (
        ("job", "id"),
        (("", "job_id"), ("job", "task_id"), ("engine_payload", "job_id")),
    ),
    (("job", "queue_id"), (("", "queue_id"), ("engine_payload", "queue_id"))),
    (
        ("job", "dir"),
        (
            ("", "reaction_dir"),
            ("engine_payload", "job_dir"),
            ("engine_payload", "reaction_dir"),
            ("engine_payload", "original_run_dir"),
        ),
    ),
    (("status", "state"), (("engine_payload", "status"),)),
    (("input", "primary_path"), (("", "selected_inp"), ("engine_payload", "selected_inp"))),
    (("engine_payload", "run_id"), (("", "run_id"),)),
)


def _refuse_conflicting_copies(payload: Mapping[str, Any]) -> None:
    def section(name: str) -> Mapping[str, Any]:
        return payload if not name else _mapping_field(payload, name, where="")

    for (canonical_section, canonical_key), copies in _COPIED_IDENTITY_FIELDS:
        canonical = _text_field(
            section(canonical_section), canonical_key, where=f"{canonical_section}."
        )
        for copy_section, copy_key in copies:
            holder = section(copy_section)
            if copy_key not in holder:
                continue
            where = f"{copy_section}." if copy_section else ""
            value = _text_field(holder, copy_key, where=where)
            if value and value != canonical:
                raise CorruptRunStateError(
                    f"{where}{copy_key} {value!r} conflicts with "
                    f"{canonical_section}.{canonical_key} {canonical!r}"
                )


@dataclass(frozen=True)
class DecodedRunState:
    """A validated schema-v1 ORCA state: the flat :class:`RunState` and the
    envelope fields only the job index reads (``input.selected_xyz_path`` and
    ``resources``)."""

    state: RunState
    selected_xyz_path: str
    resource_request: Mapping[str, Any]
    resource_actual: Mapping[str, Any]


def state_from_normalized_payload(payload: dict[str, Any]) -> RunState | None:
    """The schema-v1 ORCA state in ``payload``; ``None`` for a legacy or foreign state.

    Raises:
        CorruptRunStateError: as :func:`decode_run_state`.
    """
    decoded = decode_run_state(payload)
    return None if decoded is None else decoded.state


def decode_run_state(payload: dict[str, Any]) -> DecodedRunState | None:
    """Decode the schema-v1 ORCA state in ``payload``; ``None`` for a legacy or foreign state.

    Raises:
        CorruptRunStateError: for a schema-v1 ORCA state whose fields have the
            wrong types. Every field is checked here, at the disk boundary, so
            no reader converts a wrong value into a plausible one (``"abc"``
            into ``["a", "b", "c"]``) or fails on it later.
    """
    if _schema_version(payload) != 1:
        return None
    if normalized_text(payload.get("engine")) != "orca":
        return None
    job = _mapping_field(payload, "job", where="")
    status = _mapping_field(payload, "status", where="")
    input_payload = _mapping_field(payload, "input", where="")
    timestamps = _mapping_field(payload, "timestamps", where="")
    engine_payload = _mapping_field(payload, "engine_payload", where="")
    _refuse_conflicting_copies(payload)
    final_result = engine_payload.get("final_result")
    if final_result is not None and not isinstance(final_result, Mapping):
        raise CorruptRunStateError("engine_payload.final_result is not an object")
    state: RunState = {
        "job_id": _text_field(job, "id", where="job."),
        "queue_id": _text_field(job, "queue_id", where="job."),
        "queue_generation": _text_field(job, "generation", where="job."),
        "run_id": _text_field(engine_payload, "run_id", where="engine_payload."),
        "reaction_dir": _text_field(job, "dir", where="job."),
        "selected_inp": _text_field(input_payload, "primary_path", where="input."),
        "status": _text_field(status, "state", where="status."),
        "started_at": _text_field(timestamps, "started_at", where="timestamps."),
        "updated_at": _text_field(timestamps, "updated_at", where="timestamps."),
        "attempts": _record_list_field(engine_payload, "attempts", where="engine_payload."),
        "scratch_publications": _record_list_field(
            engine_payload, "scratch_publications", where="engine_payload."
        ),
        "execution_provenance": _mapping_field(
            engine_payload, "execution_provenance", where="engine_payload."
        ),
        "final_result": cast(
            RunFinalResult | None, None if final_result is None else dict(final_result)
        ),
    }
    resources = _mapping_field(payload, "resources", where="")
    return DecodedRunState(
        state=state,
        selected_xyz_path=_text_field(input_payload, "selected_xyz_path", where="input."),
        resource_request=_mapping_field(resources, "request", where="resources."),
        resource_actual=_mapping_field(resources, "actual", where="resources."),
    )


def read_run_state(reaction_dir: Path) -> RunStateRead:
    """Read ``reaction_dir``'s ``job_state.json`` and classify the outcome."""
    read = read_json_artifact(state_path(reaction_dir), max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES)
    if read.kind == "missing":
        return RunStateRead("missing")
    if read.kind == "invalid":
        # Not a regular file (a FIFO is refused without blocking), too large,
        # changed while read, or not JSON: present but unusable.
        return RunStateRead("corrupt", reason=read.reason)
    raw = read.value
    if not isinstance(raw, dict):
        return RunStateRead("corrupt", reason="not a JSON object")
    try:
        state = state_from_normalized_payload(raw)
    except CorruptRunStateError as exc:
        return RunStateRead("corrupt", reason=str(exc))
    if state is None:
        return RunStateRead("unsupported")
    return RunStateRead("valid", state=state)


def load_state(reaction_dir: Path) -> RunState | None:
    """The valid state of ``reaction_dir``; ``None`` when missing, unsupported or corrupt.

    A caller that must tell a corrupt state from a missing one (a run about to
    write, a listing that reports it) uses :func:`read_run_state`.
    """
    return read_run_state(reaction_dir).state


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
    try:
        normalized = state_from_normalized_payload(payload)
    except CorruptRunStateError:
        return None
    if normalized is None:
        return None
    return payload, normalized


__all__ = [
    "CorruptRunStateError",
    "DecodedRunState",
    "RunStateRead",
    "decode_run_state",
    "read_run_state",
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
