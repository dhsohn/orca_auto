"""Claim-time verification of a queued generation against its snapshot."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file

from .. import input_references
from ..input_blocks import validate_supported_xyz_geometry_syntax
from ._confinement import (
    _reference_source,
    _validate_dependency_basename,
    _validate_unique_dependency_basenames,
)
from ._constants import ORCA_EXECUTION_SNAPSHOT_VERSION
from ._inputs import (
    _inline_geometry_atom_count,
    _route_requests_hessian,
    _route_writes_engrad,
    _route_writes_same_stem_xyz,
)
from ._models import _VerifiedSnapshotInputs
from ._recovery import _is_recovery_checkpoint_source_name, recovery_checkpoint_private_name
from ._snapshot_identity import (
    _file_identity,
    _verify_identity,
    orca_execution_snapshot_generation_dir,
    verify_orca_snapshot_executable,
)


def orca_execution_started_evidence(job_dir: str | Path, snapshot: Any) -> bool:
    """Report whether a queued generation shows evidence of started execution.

    Evidence is structural and content-independent: any directory entry beyond
    the materialized snapshot files, or a registered runtime-mutable input
    whose bytes moved. An unreadable mutable input counts as evidence so a
    crashed run never silently reuses its generation.
    """

    if not isinstance(snapshot, Mapping):
        raise ValueError("ORCA execution snapshot must be an object")
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    execution_dir = orca_execution_snapshot_generation_dir(resolved_job_dir, snapshot)
    expected_names = {Path(str(snapshot.get("selected_inp") or "")).name}
    materialized = snapshot.get("materialized_inputs")
    if isinstance(materialized, Mapping):
        for identity in materialized.values():
            if isinstance(identity, Mapping):
                name = Path(str(identity.get("path") or "")).name
                if name:
                    expected_names.add(name)
    expected_names.discard("")
    with os.scandir(execution_dir) as entries:
        for entry in entries:
            if entry.name not in expected_names:
                return True
    mutable_roles = snapshot.get("runtime_mutable_input_roles")
    if isinstance(mutable_roles, Sequence) and isinstance(materialized, Mapping):
        for role in mutable_roles:
            identity = materialized.get(role)
            if not isinstance(identity, Mapping):
                continue
            path = Path(str(identity.get("path") or ""))
            try:
                current = _file_identity(path)
            except (OSError, ValueError):
                return True
            if current != dict(identity):
                return True
    return False


def _verify_source_descriptor(
    descriptor: Any,
    *,
    role: str,
    expected_source: str,
    job_dir: Path,
) -> tuple[Mapping[str, Any], Path]:
    if not isinstance(descriptor, Mapping) or str(descriptor.get("role") or "") != role:
        raise ValueError(f"Queued ORCA source descriptor {role!r} is invalid")
    raw_source = descriptor.get("source_path")
    if not isinstance(raw_source, str) or not raw_source:
        raise ValueError(f"Queued ORCA source descriptor {role!r} has an invalid path")
    source_text = raw_source
    if source_text != expected_source:
        raise ValueError(f"Queued ORCA source descriptor {role!r} has a mismatched path")
    try:
        source_path = Path(source_text)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Queued ORCA source descriptor {role!r} has an invalid path") from exc
    if (
        not source_path.is_absolute()
        or source_text != str(source_path)
        or ".." in source_path.parts
        or "\x00" in source_text
    ):
        raise ValueError(
            f"Queued ORCA source descriptor {role!r} does not store a canonical absolute path"
        )
    if not source_path.is_relative_to(job_dir):
        raise ValueError(f"Queued ORCA source descriptor {role!r} escapes its job directory")
    digest = str(descriptor.get("sha256") or "").strip().lower()
    size = descriptor.get("size_bytes")
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError(f"Queued ORCA source descriptor {role!r} has invalid content identity")
    return descriptor, source_path


def _verify_snapshot_input_tree(
    snapshot: Mapping[str, Any],
    *,
    resolved_job_dir: Path,
    execution_dir: Path,
    expected_source_selected_inp: str | Path,
    allow_runtime_outputs: bool,
) -> _VerifiedSnapshotInputs:
    raw_inputs = snapshot.get("source_inputs")
    materialized_inputs = snapshot.get("materialized_inputs")
    dependency_paths = snapshot.get("dependency_paths")
    runtime_mutable_input_roles = snapshot.get("runtime_mutable_input_roles")
    if (
        not isinstance(raw_inputs, Mapping)
        or not isinstance(materialized_inputs, Mapping)
        or not isinstance(dependency_paths, list)
        or not isinstance(runtime_mutable_input_roles, list)
        or any(not isinstance(path, str) or not path.strip() for path in dependency_paths)
        or any(not isinstance(role, str) or not role for role in runtime_mutable_input_roles)
        or len(runtime_mutable_input_roles) != len(set(runtime_mutable_input_roles))
    ):
        raise ValueError("Queue metadata 'execution_snapshot' has invalid ORCA inputs")
    expected_dependency_roles = {
        f"dependency_{index:06d}" for index in range(len(dependency_paths))
    }
    if set(raw_inputs) != {"selected_source"} | expected_dependency_roles:
        raise ValueError("Queued ORCA execution snapshot has unexpected source roles")
    if set(materialized_inputs) != expected_dependency_roles:
        raise ValueError("Queued ORCA execution snapshot has unexpected private input roles")
    mutable_roles = set(runtime_mutable_input_roles)
    if not mutable_roles.issubset(expected_dependency_roles):
        raise ValueError("Queued ORCA execution snapshot has invalid mutable input roles")
    recovery_block = snapshot.get("recovery")
    recovery_checkpoint_role = (
        str(recovery_block.get("checkpoint_role") or "")
        if isinstance(recovery_block, Mapping)
        else ""
    )
    if recovery_checkpoint_role and (
        recovery_checkpoint_role not in expected_dependency_roles
        or recovery_checkpoint_role in mutable_roles
    ):
        raise ValueError("Queued ORCA recovery checkpoint role is invalid")

    source_selected = str(snapshot.get("source_selected_inp") or "").strip()
    if source_selected != str(expected_source_selected_inp):
        raise ValueError("Queued ORCA source selected input does not match its queue metadata")
    _selected_descriptor, source_selected_path = _verify_source_descriptor(
        raw_inputs["selected_source"],
        role="selected_source",
        expected_source=source_selected,
        job_dir=resolved_job_dir,
    )
    verified_dependencies: list[tuple[str, Mapping[str, Any], Path]] = []
    for index, expected_source in enumerate(dependency_paths):
        role = f"dependency_{index:06d}"
        descriptor, source_path = _verify_source_descriptor(
            raw_inputs[role],
            role=role,
            expected_source=expected_source,
            job_dir=resolved_job_dir,
        )
        verified_dependencies.append((role, descriptor, source_path))
    verified_source_paths = [
        source_path for _role, _descriptor, source_path in verified_dependencies
    ]
    if len(verified_source_paths) != len(set(verified_source_paths)):
        raise ValueError("Queued ORCA execution snapshot has duplicate dependency source paths")
    if source_selected_path in verified_source_paths:
        raise ValueError("Queued ORCA execution snapshot lists its selected input as a dependency")
    _validate_unique_dependency_basenames(
        verified_source_paths,
        job_dir=resolved_job_dir,
    )
    if any(source_path.name == source_selected_path.name for source_path in verified_source_paths):
        raise ValueError(
            "Queued ORCA dependency basename conflicts with the selected input in the flat "
            f"generation: {source_selected_path.name}"
        )

    verified_private_paths: dict[str, Path] = {}
    for role, descriptor, source_path in verified_dependencies:
        materialized_identity = materialized_inputs[role]
        if not isinstance(materialized_identity, Mapping):
            raise ValueError(f"Queued ORCA dependency {role!r} has no materialized identity")
        if materialized_identity.get("sha256") != descriptor.get(
            "sha256"
        ) or materialized_identity.get("size_bytes") != descriptor.get("size_bytes"):
            raise ValueError(f"Queued ORCA dependency {role!r} mismatches its source snapshot")
        if role in mutable_roles and allow_runtime_outputs:
            private_path = require_confined_regular_file(
                execution_dir,
                Path(str(materialized_identity.get("path") or "")),
                label=f"Queued ORCA mutable runtime output {role!r}",
            )
        else:
            private_path = _verify_identity(
                materialized_identity,
                root=execution_dir,
                label=f"private dependency {role!r}",
            )
        if role == recovery_checkpoint_role:
            selected_source_name = Path(source_selected)
            if (
                private_path.parent != execution_dir
                or not _is_recovery_checkpoint_source_name(
                    source_path.name,
                    source_selected=selected_source_name,
                )
                or private_path.name != recovery_checkpoint_private_name(selected_source_name)
            ):
                raise ValueError(
                    f"Queued ORCA recovery checkpoint {role!r} violates its rename contract"
                )
        elif private_path.parent != execution_dir or private_path.name != source_path.name:
            raise ValueError(
                f"Queued ORCA dependency {role!r} does not preserve its source basename"
            )
        if (
            role in mutable_roles
            and private_path.name != Path(source_selected).with_suffix(".xyz").name
        ):
            raise ValueError(f"Queued ORCA mutable dependency {role!r} is not same-stem XYZ")
        verified_private_paths[role] = private_path
    return _VerifiedSnapshotInputs(
        source_selected=source_selected,
        source_selected_path=source_selected_path,
        verified_dependencies=verified_dependencies,
        verified_source_paths=verified_source_paths,
        verified_private_paths=verified_private_paths,
        mutable_roles=mutable_roles,
        recovery_checkpoint_role=recovery_checkpoint_role,
    )


def _verify_bound_snapshot_content(
    snapshot: Mapping[str, Any],
    verified: _VerifiedSnapshotInputs,
    *,
    resolved_job_dir: Path,
    execution_dir: Path,
    expected_selected_inp: str | Path,
    expected_selected_input_xyz: str,
    expected_resource_request: Mapping[str, int],
) -> Path:
    selected = _verify_identity(
        snapshot.get("bound_selected_identity"),
        root=execution_dir,
        label="bound selected input",
    )
    expected_selected = Path(expected_selected_inp).expanduser().resolve()
    if (
        selected != expected_selected
        or str(snapshot.get("selected_inp") or "") != str(expected_selected)
        or not selected.is_relative_to(execution_dir)
        or selected.parent != execution_dir
        or selected.name != Path(verified.source_selected).name
    ):
        raise ValueError("Queued ORCA selected input does not match its private snapshot")
    try:
        selected_text = read_stable_regular_file(
            selected,
            require_single_link=True,
        ).decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("Queued ORCA bound selected input must be UTF-8 text") from exc
    selected_lines = selected_text.splitlines()
    validate_supported_xyz_geometry_syntax(selected_lines, label="Queued ORCA bound input")
    bound_references = input_references.scan_orca_file_references(selected_lines)
    engrad_is_output = _route_writes_engrad(selected_lines)
    hessian_requested = _route_requests_hessian(selected_lines)
    same_stem_xyz_is_output = _route_writes_same_stem_xyz(selected_lines)
    for role, _descriptor, source_path in verified.verified_dependencies:
        if role == verified.recovery_checkpoint_role:
            continue
        _validate_dependency_basename(
            source_path,
            verified.source_selected_path,
            engrad_is_output=engrad_is_output,
            hessian_requested=hessian_requested,
            same_stem_xyz_is_output=same_stem_xyz_is_output,
            inline_same_stem_xyz=role in verified.mutable_roles,
        )
    bound_reference_paths = {
        _reference_source(execution_dir, selected, reference.value)
        for reference in bound_references
    }
    expected_bound_reference_paths = {
        private_path
        for role, private_path in verified.verified_private_paths.items()
        if role not in verified.mutable_roles
    }
    if bound_reference_paths != expected_bound_reference_paths:
        raise ValueError(
            "Queued ORCA bound input references do not match its materialized dependencies"
        )
    if verified.verified_source_paths != sorted(
        verified.verified_source_paths,
        key=lambda path: path.relative_to(resolved_job_dir).as_posix(),
    ):
        raise ValueError("Queued ORCA dependency roles are not in canonical source path order")
    if verified.mutable_roles and (
        _inline_geometry_atom_count(selected_text) is None
        or any(reference.kind == "geometry" for reference in bound_references)
    ):
        raise ValueError("Queued ORCA mutable same-stem XYZ is not bound as inline geometry")
    if str(snapshot.get("selected_input_xyz") or "") != str(expected_selected_input_xyz or ""):
        raise ValueError("Queued ORCA selected geometry does not match its execution snapshot")
    if snapshot.get("resource_request") != dict(expected_resource_request):
        raise ValueError("Queued ORCA resource request does not match its execution snapshot")
    return selected


def verify_orca_execution_snapshot(
    job_dir: str | Path,
    snapshot: Any,
    *,
    expected_selected_inp: str | Path,
    expected_source_selected_inp: str | Path,
    expected_selected_input_xyz: str,
    expected_resource_request: Mapping[str, int],
    allow_runtime_outputs: bool = False,
) -> tuple[Path, str]:
    """Verify a queued private ORCA input tree and its bound executable identity."""

    resolved_job_dir = Path(job_dir).expanduser().resolve()
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("version") != ORCA_EXECUTION_SNAPSHOT_VERSION
        or "max_retries" in snapshot
    ):
        raise ValueError("Queue metadata 'execution_snapshot' has an unsupported version")
    execution_dir = orca_execution_snapshot_generation_dir(resolved_job_dir, snapshot)

    job_identity = snapshot.get("job_dir_identity")
    if not isinstance(job_identity, Mapping):
        raise ValueError("Queued ORCA execution snapshot has no job directory identity")
    job_details = resolved_job_dir.stat()
    if (int(job_details.st_dev), int(job_details.st_ino)) != (
        int(job_identity.get("device", -1)),
        int(job_identity.get("inode", -1)),
    ):
        raise ValueError("Queued ORCA job directory identity changed")

    verified = _verify_snapshot_input_tree(
        snapshot,
        resolved_job_dir=resolved_job_dir,
        execution_dir=execution_dir,
        expected_source_selected_inp=expected_source_selected_inp,
        allow_runtime_outputs=allow_runtime_outputs,
    )
    selected = _verify_bound_snapshot_content(
        snapshot,
        verified,
        resolved_job_dir=resolved_job_dir,
        execution_dir=execution_dir,
        expected_selected_inp=expected_selected_inp,
        expected_selected_input_xyz=expected_selected_input_xyz,
        expected_resource_request=expected_resource_request,
    )

    executable = verify_orca_snapshot_executable(snapshot)
    return selected, executable
