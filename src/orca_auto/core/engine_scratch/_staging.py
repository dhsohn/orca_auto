"""Input-closure capture and staging: read the selected durable input and its
declared dependencies into memory under the root lock, size the closure for
the capacity check, write the copies into the workspace, and later verify
that the durable sources did not change while the engine ran.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._constants import (
    _DURABLE_RESERVED_FILE_NAMES,
    _SCRATCH_CONTROL_FILE_NAMES,
)
from ._errors import EngineScratchError
from ._fs import (
    _atomic_write_bytes_at,
    _read_stable_regular_file_at,
    _regular_file_sha256_at,
)


@dataclass(frozen=True)
class _StagedInput:
    source: Path
    name: str
    source_sha256: str
    source_size_bytes: int
    staged_sha256: str
    staged_size_bytes: int
    mode: int


@dataclass(frozen=True)
class _CapturedInput:
    name: str
    payload: bytes
    mode: int


def _capture_input_closure(
    durable_dir_fd: int,
    durable_dir: Path,
    selected_name: str,
    *,
    dependency_names_from_primary: Callable[[bytes], Sequence[str]] | None,
) -> dict[str, _CapturedInput]:
    selected_payload, selected_mode = _read_stable_regular_file_at(
        durable_dir_fd,
        selected_name,
        display_path=durable_dir / selected_name,
    )
    captured = {
        selected_name: _CapturedInput(
            name=selected_name,
            payload=selected_payload,
            mode=selected_mode,
        )
    }
    dependencies = (
        dependency_names_from_primary(selected_payload)
        if dependency_names_from_primary is not None
        else ()
    )
    for dependency_name in dependencies:
        raw = Path(str(dependency_name))
        if raw.is_absolute() or raw.name != str(dependency_name) or raw.name in {"", ".", ".."}:
            raise EngineScratchError(
                "RAM scratch requires input dependencies to use one relative basename: "
                f"{dependency_name!r}"
            )
        if raw.name in _SCRATCH_CONTROL_FILE_NAMES or raw.name in _DURABLE_RESERVED_FILE_NAMES:
            raise EngineScratchError(
                f"ORCA RAM scratch dependency collides with runtime state: {raw.name}"
            )
        payload, mode = _read_stable_regular_file_at(
            durable_dir_fd,
            raw.name,
            display_path=durable_dir / raw.name,
        )
        captured.setdefault(
            raw.name,
            _CapturedInput(name=raw.name, payload=payload, mode=mode),
        )
    return captured


def _input_closure_size_bytes(
    selected_name: str,
    captured_inputs: dict[str, _CapturedInput],
    *,
    normalize_primary_newline: bool,
) -> int:
    required = sum(len(item.payload) for item in captured_inputs.values())
    selected_payload = captured_inputs[selected_name].payload
    if normalize_primary_newline and selected_payload and not selected_payload.endswith(b"\n"):
        required += 1
    return required


def _stage_input_closure(
    durable_input: Path,
    *,
    workspace_dir_fd: int,
    captured_inputs: dict[str, _CapturedInput],
    normalize_primary_newline: bool,
) -> dict[str, _StagedInput]:
    staged: dict[str, _StagedInput] = {}
    for name, captured in captured_inputs.items():
        source = durable_input.parent / name
        payload = captured.payload
        staged_payload = payload
        if (
            normalize_primary_newline
            and source == durable_input
            and payload
            and not payload.endswith(b"\n")
        ):
            staged_payload += b"\n"
        item = _StagedInput(
            source=source,
            name=name,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_size_bytes=len(payload),
            staged_sha256=hashlib.sha256(staged_payload).hexdigest(),
            staged_size_bytes=len(staged_payload),
            mode=captured.mode,
        )
        _atomic_write_bytes_at(
            workspace_dir_fd,
            name,
            staged_payload,
            mode=item.mode,
        )
        staged[name] = item
    return staged


def _verify_staged_sources_unchanged(
    durable_dir_fd: int,
    durable_dir: Path,
    staged: dict[str, _StagedInput],
) -> None:
    for item in staged.values():
        digest, size = _regular_file_sha256_at(
            durable_dir_fd,
            item.name,
            display_path=durable_dir / item.name,
        )
        if digest != item.source_sha256 or size != item.source_size_bytes:
            raise EngineScratchError(
                f"Durable engine input changed during scratch run: {item.source}"
            )
