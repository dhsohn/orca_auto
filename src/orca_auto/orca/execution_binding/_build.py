"""Building one visible, immutable ORCA execution generation."""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.geometry_limits import MAX_ADMISSION_ATOMS, MAX_HESSIAN_ADMISSION_ATOMS
from orca_auto.core.queue.engine.input_snapshot import (
    cleanup_unowned_direct_generation_directory,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
    discard_snapshot_intent_if_generations_absent,
)

from .. import input_references
from ..input_blocks import (
    orca_input_requests_moread,
    orca_moinp_references,
    validate_supported_xyz_geometry_syntax,
)
from ..resource_directives import resource_request_from_lines
from ._confinement import (
    _confined_reference_path,
    _payload_with_budget,
    _private_input_path,
    _reference_source,
    _source_with_budget,
    _validate_dependency_basename,
    _validate_unique_dependency_basenames,
    _write_private_input,
)
from ._constants import ORCA_EXECUTION_SNAPSHOT_VERSION
from ._inputs import (
    _inline_geometry_atom_count,
    _route_requests_hessian,
    _route_writes_engrad,
    _route_writes_same_stem_xyz,
    _validated_xyz_atom_count,
    _xyz_atom_lines,
)
from ._models import _BoundDependency, _MaterializedSnapshotInputs, _SelectedSnapshotInput
from ._recovery import (
    _recovery_seed_plan,
    _validated_recovery_checkpoint,
    _validated_recovery_seed,
    _validated_recovery_source_payload,
    recovery_checkpoint_private_name,
)
from ._reservation import _reserve_execution_generation
from ._rewrite import _write_bound_selected_snapshot
from ._snapshot_identity import _file_identity, verify_orca_snapshot_executable

_XYZ_GEOMETRY_REFERENCE_KINDS = frozenset({"geometry", "neb_geometry"})


def _load_selected_snapshot_input(
    source_selected: Path,
    *,
    normalized_selected_payload: bytes | None,
    source_selected_sha256: str | None,
    recovery_from: Mapping[str, Any] | None,
    recovery_selected_sha256: str,
    resource_request: Mapping[str, int],
) -> _SelectedSnapshotInput:
    source_inputs: dict[str, dict[str, Any]] = {}
    selected_descriptor, selected_payload, consumed_bytes = _source_with_budget(
        source_selected,
        role="selected_source",
        consumed_bytes=0,
    )
    source_inputs["selected_source"] = selected_descriptor
    if (
        source_selected_sha256 is not None
        and str(selected_descriptor.get("sha256") or "") != source_selected_sha256
    ):
        raise ValueError("ORCA selected input changed while submission resources were prepared")
    if recovery_from is not None and (
        str(selected_descriptor.get("sha256") or "").strip().lower() != recovery_selected_sha256
    ):
        raise ValueError("ORCA recovery source input changed since the crashed submission")
    try:
        selected_text = (
            normalized_selected_payload
            if normalized_selected_payload is not None
            else selected_payload
        ).decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("ORCA selected input must be UTF-8 text") from exc
    inline_atom_count = _inline_geometry_atom_count(selected_text)
    lines = selected_text.splitlines()
    if normalized_selected_payload is not None and resource_request_from_lines(lines) != dict(
        resource_request
    ):
        raise ValueError("ORCA normalized selected input does not match its resource request")
    validate_supported_xyz_geometry_syntax(lines, label="ORCA selected input")
    requests_moread = orca_input_requests_moread(lines)
    if requests_moread and not orca_moinp_references(lines):
        raise ValueError(
            "ORCA MORead requires an explicit MOInp file so the checkpoint can be "
            "bound into the execution snapshot"
        )
    engrad_is_output = _route_writes_engrad(lines)
    hessian_requested = _route_requests_hessian(lines)
    same_stem_xyz_is_output = _route_writes_same_stem_xyz(lines)
    if (
        hessian_requested
        and inline_atom_count is not None
        and inline_atom_count > MAX_HESSIAN_ADMISSION_ATOMS
    ):
        raise ValueError(
            "ORCA frequency calculation exceeds the server Hessian atom-count "
            f"limit of {MAX_HESSIAN_ADMISSION_ATOMS}"
        )
    return _SelectedSnapshotInput(
        source_inputs=source_inputs,
        selected_payload=selected_payload,
        lines=lines,
        references=input_references.scan_orca_file_references(lines),
        requests_moread=requests_moread,
        engrad_is_output=engrad_is_output,
        hessian_requested=hessian_requested,
        same_stem_xyz_is_output=same_stem_xyz_is_output,
        consumed_bytes=consumed_bytes,
    )


def _plan_bound_dependencies(
    resolved_job_dir: Path,
    source_selected: Path,
    selected: _SelectedSnapshotInput,
    *,
    recovery_from: Mapping[str, Any] | None,
    recovery_seed_dir: Path | None,
    recovery_seed_basenames: set[str],
    recovery_seed_atom_signature: tuple[str, ...] | None,
    recovery_submitted_dependency_identities: Mapping[str, Mapping[str, Any]],
) -> tuple[list[_BoundDependency], set[Path]]:
    dependency_sources: set[Path] = set()
    geometry_dependencies: set[Path] = set()
    dependency_reference_kinds: dict[Path, set[str]] = {}
    for reference in selected.references:
        prospective_dependency = _confined_reference_path(
            resolved_job_dir, source_selected, reference.value
        )
        may_use_recovery_geometry_seed = (
            recovery_seed_dir is not None
            and selected.same_stem_xyz_is_output
            and reference.kind == "geometry"
            and prospective_dependency.name == source_selected.with_suffix(".xyz").name
            and str(prospective_dependency) in recovery_submitted_dependency_identities
        )
        dependency = (
            prospective_dependency
            if may_use_recovery_geometry_seed
            else _reference_source(
                resolved_job_dir,
                source_selected,
                reference.value,
            )
        )
        if dependency == source_selected:
            raise ValueError("ORCA selected input must not reference itself as an input file")
        if reference.kind in _XYZ_GEOMETRY_REFERENCE_KINDS:
            geometry_dependencies.add(dependency)
        dependency_reference_kinds.setdefault(dependency, set()).add(reference.kind)
        dependency_sources.add(dependency)
    dependencies = sorted(
        dependency_sources,
        key=lambda path: path.relative_to(resolved_job_dir).as_posix(),
    )
    _validate_unique_dependency_basenames(
        dependencies,
        job_dir=resolved_job_dir,
    )

    recovery_checkpoint_source: Path | None = None
    if recovery_seed_dir is not None and not selected.requests_moread:
        # Count dependencies twice: once for materialized copies and once for
        # possible inline-geometry expansion of the bound input.
        estimated_source_bytes = (
            selected.consumed_bytes
            + 2
            * sum(
                int(recovery_submitted_dependency_identities[str(dependency)]["size_bytes"])
                if str(dependency) in recovery_submitted_dependency_identities
                else dependency.stat().st_size
                for dependency in dependencies
            )
            + 2 * len(selected.selected_payload)
        )
        recovery_checkpoint_source = _validated_recovery_checkpoint(
            job_dir=resolved_job_dir,
            seed_dir=recovery_seed_dir,
            source_selected=source_selected,
            scanned_dependencies=dependencies,
            estimated_source_bytes=estimated_source_bytes,
        )

    bound_dependencies: list[_BoundDependency] = []
    for dependency in dependencies:
        inline_same_stem_xyz = (
            selected.same_stem_xyz_is_output
            and dependency.name == source_selected.with_suffix(".xyz").name
            and dependency_reference_kinds.get(dependency) == {"geometry"}
        )
        _validate_dependency_basename(
            dependency,
            source_selected,
            engrad_is_output=selected.engrad_is_output,
            hessian_requested=selected.hessian_requested,
            same_stem_xyz_is_output=selected.same_stem_xyz_is_output,
            inline_same_stem_xyz=inline_same_stem_xyz,
        )
        seed_source: Path | None = None
        dependency_payload: bytes | None = None
        if (
            recovery_seed_dir is not None
            and inline_same_stem_xyz
            and dependency.name in recovery_seed_basenames
        ):
            if recovery_seed_atom_signature is None:
                raise ValueError("ORCA recovery mutable geometry has no bound atom signature")
            validated_seed = _validated_recovery_seed(
                job_dir=resolved_job_dir,
                seed_dir=recovery_seed_dir,
                basename=dependency.name,
                expected_atom_signature=recovery_seed_atom_signature,
                max_atoms=(
                    MAX_HESSIAN_ADMISSION_ATOMS
                    if selected.hessian_requested
                    else MAX_ADMISSION_ATOMS
                ),
            )
            if validated_seed is not None:
                seed_source, dependency_payload = validated_seed
        if seed_source is None and recovery_from is not None:
            dependency_payload = _validated_recovery_source_payload(
                resolved_job_dir,
                dependency,
                recovery_submitted_dependency_identities,
            )
        bound_dependencies.append(
            _BoundDependency(
                source=dependency,
                effective_source=seed_source if seed_source is not None else dependency,
                inline_same_stem_xyz=inline_same_stem_xyz,
                private_override=None,
                source_payload=dependency_payload,
            )
        )
    if recovery_checkpoint_source is not None:
        bound_dependencies.append(
            _BoundDependency(
                source=recovery_checkpoint_source,
                effective_source=recovery_checkpoint_source,
                inline_same_stem_xyz=False,
                private_override=recovery_checkpoint_private_name(source_selected),
                source_payload=None,
            )
        )
    # Roles follow canonical stored source paths, matching claim-time verification.
    bound_dependencies.sort(
        key=lambda item: item.effective_source.relative_to(resolved_job_dir).as_posix(),
    )
    return bound_dependencies, geometry_dependencies


def _materialize_snapshot_inputs(
    resolved_job_dir: Path,
    execution_dir: Path,
    source_selected: Path,
    selected: _SelectedSnapshotInput,
    bound_dependencies: Sequence[_BoundDependency],
    geometry_dependencies: set[Path],
) -> _MaterializedSnapshotInputs:
    source_inputs = dict(selected.source_inputs)
    seeded_roles: dict[str, dict[str, Any]] = {}
    private_paths: dict[Path, Path] = {}
    materialized_inputs: dict[str, dict[str, Any]] = {}
    runtime_mutable_input_roles: list[str] = []
    inline_geometry_atoms: dict[Path, list[str]] = {}
    dependency_paths: list[str] = []
    recovery_checkpoint_role = ""
    recovery_checkpoint_private: Path | None = None
    consumed_bytes = selected.consumed_bytes
    for index, dependency_plan in enumerate(bound_dependencies):
        dependency = dependency_plan.source
        effective_source = dependency_plan.effective_source
        inline_same_stem_xyz = dependency_plan.inline_same_stem_xyz
        private_override = dependency_plan.private_override
        source_payload = dependency_plan.source_payload
        role = f"dependency_{index:06d}"
        if source_payload is None:
            descriptor, dependency_payload, consumed_bytes = _source_with_budget(
                effective_source,
                role=role,
                consumed_bytes=consumed_bytes,
            )
        else:
            descriptor, dependency_payload, consumed_bytes = _payload_with_budget(
                effective_source,
                source_payload,
                role=role,
                consumed_bytes=consumed_bytes,
            )
        source_inputs[role] = descriptor
        if effective_source is not dependency:
            seeded_roles[role] = {
                "path": str(descriptor.get("source_path") or ""),
                "sha256": str(descriptor.get("sha256") or ""),
                "size_bytes": int(descriptor.get("size_bytes") or 0),
            }
        if dependency in geometry_dependencies:
            geometry_limit = (
                MAX_HESSIAN_ADMISSION_ATOMS if selected.hessian_requested else MAX_ADMISSION_ATOMS
            )
            if inline_same_stem_xyz:
                inline_geometry_atoms[dependency] = _xyz_atom_lines(
                    dependency,
                    dependency_payload,
                    max_atoms=geometry_limit,
                )
            else:
                _validated_xyz_atom_count(
                    dependency,
                    dependency_payload,
                    max_atoms=geometry_limit,
                )
        target = (
            execution_dir / private_override
            if private_override is not None
            else _private_input_path(execution_dir, source=dependency)
        )
        if target.name == source_selected.name:
            raise ValueError(
                "ORCA referenced input basename conflicts with the selected input in the "
                f"flat generation: {target.name}"
            )
        _write_private_input(
            execution_dir,
            target,
            dependency_payload,
            label="ORCA dependency snapshot",
        )
        if inline_same_stem_xyz:
            target.chmod(0o600)
        private_paths[dependency] = target.resolve()
        materialized_inputs[role] = _file_identity(target)
        if inline_same_stem_xyz:
            runtime_mutable_input_roles.append(role)
        if private_override is not None:
            recovery_checkpoint_role = role
            recovery_checkpoint_private = target.resolve()
        dependency_paths.append(str(effective_source))
    return _MaterializedSnapshotInputs(
        source_inputs=source_inputs,
        seeded_roles=seeded_roles,
        private_paths=private_paths,
        materialized_inputs=materialized_inputs,
        runtime_mutable_input_roles=runtime_mutable_input_roles,
        inline_geometry_atoms=inline_geometry_atoms,
        dependency_paths=dependency_paths,
        recovery_checkpoint_role=recovery_checkpoint_role,
        recovery_checkpoint_private=recovery_checkpoint_private,
        consumed_bytes=consumed_bytes,
    )


def build_orca_execution_snapshot(
    job_dir: str | Path,
    selected_inp: str | Path,
    *,
    selected_input_xyz: str,
    resource_request: Mapping[str, int],
    orca_executable: str | Path,
    queue_root: str | Path | None = None,
    snapshot_intent_token: str | None = None,
    target_generation_name: str | None = None,
    normalized_selected_payload: bytes | None = None,
    source_selected_sha256: str | None = None,
    recovery_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one visible, immutable ORCA execution generation.

    With ``recovery_from`` (the crashed submission's snapshot), runtime-mutable
    geometry inputs are seeded from that frozen generation so the replacement
    generation resumes from the last written geometry while every integrity
    check stays byte-exact.
    """

    if set(resource_request) != {"max_cores", "max_memory_gb"} or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in resource_request.values()
    ):
        raise ValueError("ORCA execution snapshot resources must be positive integers")
    if (normalized_selected_payload is None) != (source_selected_sha256 is None):
        raise ValueError("ORCA normalized selected input requires its bound source digest")
    raw_job_dir = Path(job_dir).expanduser()
    resolved_job_dir = raw_job_dir.resolve()
    if raw_job_dir.is_symlink() or not resolved_job_dir.is_dir():
        raise ValueError(f"ORCA job directory must be a real directory: {job_dir}")
    job_dir_stat = resolved_job_dir.stat()
    source_selected = require_confined_regular_file(
        resolved_job_dir,
        Path(selected_inp),
        label="ORCA selected input",
    )
    if source_selected.suffix.lower() != ".inp":
        raise ValueError(f"ORCA selected input must be an .inp file: {source_selected}")

    if recovery_from is not None and (
        recovery_from.get("version") != ORCA_EXECUTION_SNAPSHOT_VERSION
        or "max_retries" in recovery_from
    ):
        raise ValueError("ORCA recovery requires a current execution snapshot; resubmit the job")
    recovery_executable: str | None = None
    if recovery_from is not None:
        recovery_executable = verify_orca_snapshot_executable(
            recovery_from,
            expected_executable=orca_executable,
        )

    recovery_seed_dir: Path | None = None
    recovery_selected_sha256 = ""
    recovery_seed_basenames: set[str] = set()
    recovery_seed_atom_signature: tuple[str, ...] | None = None
    recovery_submitted_dependency_identities: dict[str, dict[str, Any]] = {}
    if recovery_from is not None:
        (
            recovery_seed_dir,
            recovery_selected_sha256,
            recovery_seed_basenames,
            recovery_seed_atom_signature,
            recovery_submitted_dependency_identities,
        ) = _recovery_seed_plan(resolved_job_dir, recovery_from)
    selected = _load_selected_snapshot_input(
        source_selected,
        normalized_selected_payload=normalized_selected_payload,
        source_selected_sha256=source_selected_sha256,
        recovery_from=recovery_from,
        recovery_selected_sha256=recovery_selected_sha256,
        resource_request=resource_request,
    )
    resolved_queue_root = Path(queue_root or resolved_job_dir).expanduser().resolve()
    resolved_intent_token = snapshot_intent_token or f"snapshot-{secrets.token_hex(16)}"
    generation_name, execution_dir, generation_identity = _reserve_execution_generation(
        resolved_job_dir,
        queue_root=resolved_queue_root,
        intent_token=resolved_intent_token,
        target_generation_name=target_generation_name,
    )
    try:
        bound_dependencies, geometry_dependencies = _plan_bound_dependencies(
            resolved_job_dir,
            source_selected,
            selected,
            recovery_from=recovery_from,
            recovery_seed_dir=recovery_seed_dir,
            recovery_seed_basenames=recovery_seed_basenames,
            recovery_seed_atom_signature=recovery_seed_atom_signature,
            recovery_submitted_dependency_identities=(recovery_submitted_dependency_identities),
        )

        materialized = _materialize_snapshot_inputs(
            resolved_job_dir,
            execution_dir,
            source_selected,
            selected,
            bound_dependencies,
            geometry_dependencies,
        )
        bound_selected, bound_identity = _write_bound_selected_snapshot(
            resolved_job_dir,
            execution_dir,
            source_selected,
            selected,
            materialized,
        )
        executable = _engine_runner.executable_identity(
            recovery_executable if recovery_executable is not None else orca_executable
        )
        if recovery_from is not None:
            executable_identities = recovery_from.get("executable_identities")
            previous_executable = (
                executable_identities.get("orca")
                if isinstance(executable_identities, Mapping)
                else None
            )
            if executable != previous_executable:
                raise ValueError(
                    "ORCA crash recovery executable changed while the replacement snapshot "
                    "was built"
                )
        snapshot: dict[str, Any] = {
            "version": ORCA_EXECUTION_SNAPSHOT_VERSION,
            "job_dir_identity": {
                "device": int(job_dir_stat.st_dev),
                "inode": int(job_dir_stat.st_ino),
            },
            "generation_name": generation_name,
            "execution_dir_identity": {
                "device": generation_identity[0],
                "inode": generation_identity[1],
            },
            "execution_dir": str(execution_dir),
            SNAPSHOT_INTENT_TOKEN_KEY: resolved_intent_token,
            SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(resolved_queue_root),
            "source_selected_inp": str(source_selected),
            "selected_inp": str(bound_selected.resolve()),
            "selected_input_xyz": str(selected_input_xyz or ""),
            "dependency_paths": materialized.dependency_paths,
            "source_inputs": materialized.source_inputs,
            "materialized_inputs": materialized.materialized_inputs,
            "runtime_mutable_input_roles": materialized.runtime_mutable_input_roles,
            "bound_selected_identity": bound_identity,
            "resource_request": dict(resource_request),
            "executable_identities": {"orca": executable},
        }
        if recovery_from is not None:
            snapshot["recovery"] = {
                "previous_generation_name": str(recovery_from.get("generation_name") or ""),
                "previous_execution_dir": str(recovery_seed_dir),
                "seeded_roles": materialized.seeded_roles,
                "checkpoint_role": materialized.recovery_checkpoint_role,
                "submitted_dependency_identities": (recovery_submitted_dependency_identities),
            }
        return snapshot
    except BaseException:
        try:
            cleanup_unowned_direct_generation_directory(
                resolved_job_dir,
                namespace=generation_name,
                label="ORCA execution snapshot",
                expected_job_identity=(job_dir_stat.st_dev, job_dir_stat.st_ino),
                expected_generation_identity=generation_identity,
                expected_owner_token=resolved_intent_token,
            )
        finally:
            discard_snapshot_intent_if_generations_absent(
                resolved_queue_root,
                resolved_intent_token,
            )
        raise
