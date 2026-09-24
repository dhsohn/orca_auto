"""Seeding a replacement generation from a crashed submission's frozen generation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orca_auto.core.confined_io import require_confined_regular_file
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    read_stable_regular_file,
)

from ..inp_rewriter import resume_checkpoint_input_path
from ..input_references import checkpoint_file_looks_intact
from ._constants import MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES
from ._inputs import _inline_geometry_atom_signature, _strict_xyz_atom_row, _xyz_atom_lines
from ._snapshot_identity import (
    _verified_identity_payload,
    orca_execution_snapshot_generation_dir,
)


def _recovery_seed_plan(
    job_dir: Path,
    recovery_from: Mapping[str, Any],
) -> tuple[Path, str, set[str], tuple[str, ...] | None, dict[str, dict[str, Any]]]:
    """Validate a crashed snapshot and plan seeding from its frozen generation."""

    seed_dir = orca_execution_snapshot_generation_dir(job_dir, recovery_from)
    raw_sources = recovery_from.get("source_inputs")
    raw_selected = raw_sources.get("selected_source") if isinstance(raw_sources, Mapping) else None
    if not isinstance(raw_selected, Mapping):
        raise ValueError("ORCA recovery snapshot has no selected source descriptor")
    selected_sha256 = str(raw_selected.get("sha256") or "").strip().lower()
    if len(selected_sha256) != 64:
        raise ValueError("ORCA recovery snapshot has no selected source digest")
    mutable_roles = recovery_from.get("runtime_mutable_input_roles")
    materialized = recovery_from.get("materialized_inputs")
    seed_basenames: set[str] = set()
    if isinstance(mutable_roles, Sequence) and isinstance(materialized, Mapping):
        for role in mutable_roles:
            identity = materialized.get(role)
            if not isinstance(identity, Mapping):
                continue
            name = Path(str(identity.get("path") or "")).name
            if name:
                seed_basenames.add(name)
    seed_atom_signature: tuple[str, ...] | None = None
    if seed_basenames:
        _bound_selected, bound_selected_payload = _verified_identity_payload(
            recovery_from.get("bound_selected_identity"),
            root=seed_dir,
            label="recovery bound selected input",
        )
        seed_atom_signature = _inline_geometry_atom_signature(
            _bound_selected,
            bound_selected_payload,
        )
    submitted_identities = _recovery_submitted_dependency_identities(
        job_dir,
        recovery_from,
    )
    return seed_dir, selected_sha256, seed_basenames, seed_atom_signature, submitted_identities


def _validated_submitted_dependency_identity(
    job_dir: Path,
    source_text: str,
    identity: Any,
) -> dict[str, Any]:
    try:
        source = Path(source_text)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("ORCA recovery dependency identity has an invalid source path") from exc
    if (
        not source_text
        or not source.is_absolute()
        or source_text != str(source)
        or ".." in source.parts
        or "\x00" in source_text
        or not source.is_relative_to(job_dir)
    ):
        raise ValueError("ORCA recovery dependency identity has an invalid source path")
    if not isinstance(identity, Mapping):
        raise ValueError(f"ORCA recovery dependency identity is invalid: {source_text}")
    digest = str(identity.get("sha256") or "").strip().lower()
    size = identity.get("size_bytes")
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError(f"ORCA recovery dependency identity is invalid: {source_text}")
    return {"sha256": digest, "size_bytes": size}


def _recovery_submitted_dependency_identities(
    job_dir: Path,
    recovery_from: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    recovery = recovery_from.get("recovery")
    propagated = (
        recovery.get("submitted_dependency_identities") if isinstance(recovery, Mapping) else None
    )
    if propagated is not None:
        if not isinstance(propagated, Mapping):
            raise ValueError("ORCA recovery submitted dependency identities are invalid")
        return {
            str(source): _validated_submitted_dependency_identity(
                job_dir,
                str(source),
                identity,
            )
            for source, identity in propagated.items()
        }

    dependency_paths = recovery_from.get("dependency_paths")
    source_inputs = recovery_from.get("source_inputs")
    if not isinstance(dependency_paths, list) or not isinstance(source_inputs, Mapping):
        raise ValueError("ORCA recovery snapshot has invalid dependency identities")
    submitted: dict[str, dict[str, Any]] = {}
    for index, raw_source in enumerate(dependency_paths):
        if not isinstance(raw_source, str):
            raise ValueError("ORCA recovery snapshot has invalid dependency identities")
        role = f"dependency_{index:06d}"
        descriptor = source_inputs.get(role)
        if (
            not isinstance(descriptor, Mapping)
            or str(descriptor.get("role") or "") != role
            or descriptor.get("source_path") != raw_source
        ):
            raise ValueError("ORCA recovery snapshot has invalid dependency identities")
        submitted[raw_source] = _validated_submitted_dependency_identity(
            job_dir,
            raw_source,
            descriptor,
        )
    return submitted


def _validated_recovery_source_payload(
    job_dir: Path,
    source: Path,
    submitted_identities: Mapping[str, Mapping[str, Any]],
) -> bytes:
    expected = submitted_identities.get(str(source))
    if expected is None:
        raise ValueError(
            "ORCA recovery has no submitted identity for dependency: "
            f"{source.relative_to(job_dir).as_posix()}"
        )
    payload = read_stable_regular_file(source, max_bytes=MAX_INPUT_SNAPSHOT_BYTES)
    if len(payload) != expected.get("size_bytes") or hashlib.sha256(
        payload
    ).hexdigest() != expected.get("sha256"):
        raise ValueError(
            "ORCA recovery dependency changed since the crashed submission: "
            f"{source.relative_to(job_dir).as_posix()}"
        )
    return payload


def _validated_recovery_seed(
    *,
    job_dir: Path,
    seed_dir: Path,
    basename: str,
    expected_atom_signature: tuple[str, ...],
    max_atoms: int,
) -> tuple[Path, bytes] | None:
    """Return the exact validated runtime seed bytes, or None to use the source.

    A crash can leave the runtime geometry absent, empty, or truncated
    mid-write; those fall back to the pristine submission geometry. A seed
    that parses but changes the submitted atom order or labels is evidence of
    substitution, not of a crash, and fails closed.
    """

    candidate = seed_dir / basename
    if not candidate.exists() and not candidate.is_symlink():
        return None
    seed = require_confined_regular_file(
        job_dir,
        candidate,
        label="ORCA recovery seed geometry",
    )
    payload = read_stable_regular_file(seed, max_bytes=MAX_INPUT_SNAPSHOT_BYTES)
    if not payload.strip():
        return None
    try:
        seed_atoms = _xyz_atom_lines(seed, payload, max_atoms=max_atoms)
    except ValueError:
        return None
    seed_atom_signature = tuple(_strict_xyz_atom_row(seed, line) for line in seed_atoms)
    if seed_atom_signature != expected_atom_signature:
        raise ValueError(
            "ORCA recovery seed geometry does not preserve the submitted atom count, order, "
            "and labels: "
            f"{basename}"
        )
    return seed, payload


def recovery_checkpoint_private_name(source_selected: Path) -> str:
    return f"{source_selected.stem}.moinp.gbw"


def recovery_checkpoint_source_names(source_selected: Path) -> list[str]:
    """Checkpoint basenames for the submitted input and interrupted-run recovery."""
    return [
        source_selected.with_suffix(".gbw").name,
        resume_checkpoint_input_path(source_selected).with_suffix(".gbw").name,
    ]


def _is_recovery_checkpoint_source_name(name: str, *, source_selected: Path) -> bool:
    """Match only the checkpoint names generated by the current execution contract."""

    stem = re.escape(source_selected.stem)
    return re.fullmatch(rf"{stem}(?:\.resume)?\.gbw", name) is not None


def _validated_recovery_checkpoint(
    *,
    job_dir: Path,
    seed_dir: Path,
    source_selected: Path,
    scanned_dependencies: Sequence[Path],
    estimated_source_bytes: int,
) -> Path | None:
    """Return the frozen runtime checkpoint to seed from, or None to skip.

    Considers the base and resume stems and picks the
    newest-written intact candidate. Skipping (absent, empty, torn with a
    zero-filled head, over the snapshot byte budgets, or a basename collision
    with a scanned dependency) degrades to geometry-only recovery; it never
    fails the rebind. A symlinked or otherwise non-regular candidate still
    fails closed via the confinement check.
    """

    best: tuple[int, int, Path] | None = None
    source_names = recovery_checkpoint_source_names(
        source_selected,
    )
    for rank, name in enumerate(source_names):
        candidate = seed_dir / name
        if not candidate.exists() and not candidate.is_symlink():
            continue
        checkpoint = require_confined_regular_file(
            job_dir,
            candidate,
            label="ORCA recovery checkpoint",
        )
        size = checkpoint.stat().st_size
        if size == 0 or size > MAX_INPUT_SNAPSHOT_BYTES:
            continue
        if not checkpoint_file_looks_intact(checkpoint):
            continue
        if estimated_source_bytes + size > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
            continue
        key = (checkpoint.stat().st_mtime_ns, rank)
        if best is None or key > (best[0], best[1]):
            best = (key[0], key[1], checkpoint)
    if best is None:
        return None
    private_name = recovery_checkpoint_private_name(source_selected)
    if any(dependency.name == private_name for dependency in scanned_dependencies):
        return None
    return best[2]
