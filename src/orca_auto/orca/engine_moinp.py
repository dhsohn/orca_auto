"""Writable engine-time MOInp copies, separate from immutable bound GBWs.

The reserved basename in the bound input encodes its immutable source. Both
scratch staging and in-place execution use this mapping; only the runner
materializes the writable copy, immediately before launching the engine.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from orca_auto.core.confined_io import MAX_AGGREGATE_SNAPSHOT_BYTES, MAX_INPUT_SNAPSHOT_BYTES
from orca_auto.core.utils import stable_fs

from .input_blocks import InputSource, parse_input
from .input_references import orca_moinp_references

ENGINE_MOINP_PREFIX = "orca_auto_moinp_"


def engine_moinp_name(source_name: str) -> str:
    if (
        not source_name
        or Path(source_name).name != source_name
        or source_name in {".", ".."}
        or source_name.startswith(ENGINE_MOINP_PREFIX)
        or any(c in source_name for c in ("\x00", "\r", "\n", "\\"))
    ):
        raise ValueError(f"Unsafe or reserved ORCA MOInp source basename: {source_name!r}")
    return ENGINE_MOINP_PREFIX + source_name


def engine_moinp_sources(lines: InputSource) -> dict[str, str]:
    """Map each bound MOInp engine basename to its immutable source basename."""
    result: dict[str, str] = {}
    for reference in orca_moinp_references(parse_input(lines)):
        target = reference.value
        source = target.removeprefix(ENGINE_MOINP_PREFIX)
        if engine_moinp_name(source) != target:
            raise ValueError("ORCA bound MOInp must name its private engine copy")
        result[target] = source
    return result


def engine_moinp_sources_from_payload(payload: bytes) -> dict[str, str]:
    return engine_moinp_sources(payload.decode("utf-8", errors="strict").splitlines())


def prepare_engine_moinp(
    inp: Path,
    *,
    directory_fd: int,
) -> None:
    """Create new, single-link 0600 copies through the pinned engine directory.

    Existing targets (including symlinks and hardlinks) are never reused or
    overwritten. A partially created copy is execution evidence, so recovery
    must rebind into a fresh generation, just as after a started engine.
    """
    payload, _ = stable_fs.read_stable_regular_file_at(
        inp.name,
        dir_fd=directory_fd,
        max_bytes=MAX_AGGREGATE_SNAPSHOT_BYTES,
        require_single_link=True,
    )
    for target, source in engine_moinp_sources_from_payload(payload).items():
        try:
            checkpoint, details = stable_fs.read_stable_regular_file_at(
                source,
                dir_fd=directory_fd,
                max_bytes=MAX_INPUT_SNAPSHOT_BYTES,
                require_single_link=True,
            )
        except stable_fs.StableFsError as exc:
            raise ValueError(f"Unsafe ORCA bound MOInp source: {source}") from exc
        if stat.S_IMODE(details.st_mode) != 0o400:
            raise ValueError("ORCA bound MOInp source must remain immutable (0400)")
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(checkpoint)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
    os.fsync(directory_fd)
