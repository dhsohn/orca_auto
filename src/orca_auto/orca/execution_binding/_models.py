"""Immutable working records passed between the binding stages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..input_references import OrcaFileReference


@dataclass(frozen=True)
class _RouteOutputs:
    """What the route line and ``%neb`` block make ORCA write next to the input."""

    engrad_is_output: bool
    hessian_requested: bool
    neb_requested: bool
    neb_preopt_ends: bool
    same_stem_xyz_is_output: bool


@dataclass(frozen=True)
class _RecoveryPlan:
    """How a replacement generation is seeded from the crashed submission's generation."""

    previous_generation_name: str
    seed_dir: Path
    selected_sha256: str
    seed_basenames: set[str]
    seed_atom_signature: tuple[str, ...] | None
    submitted_dependency_identities: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class _SelectedSnapshotInput:
    source_inputs: dict[str, dict[str, Any]]
    selected_payload: bytes
    lines: list[str]
    references: list[OrcaFileReference]
    requests_moread: bool
    routes: _RouteOutputs
    consumed_bytes: int


@dataclass(frozen=True)
class _BoundDependency:
    source: Path
    effective_source: Path
    inline_same_stem_xyz: bool
    private_override: str | None
    source_payload: bytes | None


@dataclass(frozen=True)
class _MaterializedSnapshotInputs:
    source_inputs: dict[str, dict[str, Any]]
    seeded_roles: dict[str, dict[str, Any]]
    private_paths: dict[Path, Path]
    materialized_inputs: dict[str, dict[str, Any]]
    runtime_mutable_input_roles: list[str]
    inline_geometry_atoms: dict[Path, list[str]]
    dependency_paths: list[str]
    recovery_checkpoint_role: str
    recovery_checkpoint_private: Path | None
    consumed_bytes: int


@dataclass(frozen=True)
class _VerifiedSnapshotInputs:
    source_selected: str
    source_selected_path: Path
    verified_dependencies: list[tuple[str, Mapping[str, Any], Path]]
    verified_source_paths: list[Path]
    verified_private_paths: dict[str, Path]
    mutable_roles: set[str]
    recovery_checkpoint_role: str
