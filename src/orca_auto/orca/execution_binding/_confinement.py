"""Confinement of referenced inputs to the job directory and the flat generation."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    EXECUTION_PROVENANCE_FILE,
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
)
from orca_auto.core.confined_io import (
    MAX_INPUT_SNAPSHOT_BYTES,
    atomic_write_confined_bytes,
    read_stable_regular_file,
    require_confined_regular_file,
)
from orca_auto.core.utils.persistence import durable_mkdir

from ._constants import MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES
from ._models import _RouteOutputs

_GENERATION_RUNTIME_FILE_NAMES = frozenset(
    {
        EXECUTION_PROVENANCE_FILE,
        RUN_REPORT_JSON_FILE,
        RUN_STATE_FILE,
    }
)

# Names ORCA 6.1 NEB-family routes (NEB, NEB-CI, NEB-TS, NEB-MMFTS, NEB-IDPP and
# their ZOOM-/FAST-/LOOSE-/TIGHT- variants) write after the input stem.
_NEB_OUTPUT_STEM_SUFFIX_RE = re.compile(
    r"\.(?:NEB\.log|interp|final\.interp|lastneb|opt|hess)"
    r"|_(?:MEP|NEB-|MMF|XTB|initial_path|zoom|TSOpt|optimization|geom|spline).*"
    r"|_trj\.xyz"
    r"|_im\d+(?:\..*)?"
)
# Reactant/product sub-jobs written only when %neb preoptimizes the end points.
_NEB_PREOPT_STEM_SUFFIX_RE = re.compile(r"_(?:reactant|product)(?:[._].*)?")


def _reference_source(job_dir: Path, selected_inp: Path, reference: str) -> Path:
    raw_path = Path(reference).expanduser()
    candidate = raw_path if raw_path.is_absolute() else selected_inp.parent / raw_path
    return require_confined_regular_file(
        job_dir,
        candidate,
        label="ORCA referenced input",
    )


def _confined_reference_path(job_dir: Path, selected_inp: Path, reference: str) -> Path:
    """Resolve a reference without requiring its final file to still exist."""

    raw_path = Path(reference).expanduser()
    candidate = raw_path if raw_path.is_absolute() else selected_inp.parent / raw_path
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(job_dir):
        raise ValueError(f"ORCA referenced input must stay inside its root: {candidate}")
    current = candidate
    while current != job_dir and current != current.parent:
        if current.is_symlink():
            raise ValueError(f"ORCA referenced input must not contain symlinks: {current}")
        current = current.parent
    return resolved


def _payload_with_budget(
    source: Path,
    payload: bytes,
    *,
    role: str,
    consumed_bytes: int,
) -> tuple[dict[str, Any], bytes, int]:
    if consumed_bytes + len(payload) > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
        raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
    descriptor = {
        "role": role,
        "source_path": str(source.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }
    return descriptor, payload, consumed_bytes + len(payload)


def _source_with_budget(
    source: Path,
    *,
    role: str,
    consumed_bytes: int,
) -> tuple[dict[str, Any], bytes, int]:
    payload = read_stable_regular_file(source, max_bytes=MAX_INPUT_SNAPSHOT_BYTES)
    return _payload_with_budget(
        source,
        payload,
        role=role,
        consumed_bytes=consumed_bytes,
    )


def _private_input_path(
    execution_dir: Path,
    *,
    source: Path,
) -> Path:
    name = source.name
    if not name or any(character in name for character in ("\x00", "\r", "\n", "\\")):
        raise ValueError(f"ORCA referenced input filename cannot be preserved safely: {source}")
    return execution_dir / name


def _validate_unique_dependency_basenames(
    dependencies: list[Path],
    *,
    job_dir: Path,
) -> None:
    source_by_basename: dict[str, Path] = {}
    for source in dependencies:
        previous = source_by_basename.setdefault(source.name, source)
        if previous == source:
            continue
        previous_display = previous.relative_to(job_dir).as_posix()
        source_display = source.relative_to(job_dir).as_posix()
        raise ValueError(
            "ORCA referenced inputs from different source paths use the same basename, "
            "which is not supported by a flat generation: "
            f"{source.name} ({previous_display}, {source_display})"
        )


def _validate_dependency_basename(
    source: Path,
    selected_inp: Path,
    routes: _RouteOutputs,
    *,
    inline_same_stem_xyz: bool,
) -> None:
    name = source.name
    runtime_owned_names = set(_GENERATION_RUNTIME_FILE_NAMES)
    runtime_owned_names.add(selected_inp.with_suffix(".out").name)
    runtime_owned_names.add(selected_inp.with_suffix(".gbw").name)
    if routes.engrad_is_output:
        runtime_owned_names.add(selected_inp.with_suffix(".engrad").name)
    if routes.hessian_requested:
        runtime_owned_names.add(selected_inp.with_suffix(".hess").name)
    if routes.same_stem_xyz_is_output and not inline_same_stem_xyz:
        runtime_owned_names.add(selected_inp.with_suffix(".xyz").name)
    neb_patterns = [_NEB_OUTPUT_STEM_SUFFIX_RE]
    if routes.neb_preopt_ends:
        neb_patterns.append(_NEB_PREOPT_STEM_SUFFIX_RE)
    stem = selected_inp.stem
    neb_output = (
        routes.neb_requested
        and name.startswith(stem)
        and any(pattern.fullmatch(name, len(stem)) is not None for pattern in neb_patterns)
    )
    if neb_output or name in runtime_owned_names:
        raise ValueError(
            "ORCA referenced input basename conflicts with a generation runtime/output file: "
            f"{name}; rename the referenced file so this job cannot overwrite it"
        )


def _write_private_input(
    execution_dir: Path,
    target: Path,
    payload: bytes,
    *,
    label: str,
) -> None:
    durable_mkdir(target.parent, mode=0o700, parents=True, exist_ok=True)
    atomic_write_confined_bytes(
        execution_dir,
        target,
        payload,
        label=label,
        mode=0o400,
    )
