"""Immutable working records passed between the binding stages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..input_references import OrcaFileReference


@dataclass(frozen=True)
class _SelectedSnapshotInput:
    source_inputs: dict[str, dict[str, Any]]
    selected_payload: bytes
    lines: list[str]
    references: list[OrcaFileReference]
    requests_moread: bool
    engrad_is_output: bool
    hessian_requested: bool
    same_stem_xyz_is_output: bool
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
