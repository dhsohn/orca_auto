"""Rewriting the selected input so it references its private generation copies."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..input_blocks import geometry_header_match
from ..input_references import OrcaFileReference, set_moinp
from ..input_syntax import ensure_route_keywords, quote_orca_path, unquoted_orca_path
from ..input_validation import validate_unambiguous_orca_directives
from ._confinement import _confined_reference_path, _write_private_input
from ._constants import MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES
from ._models import _MaterializedSnapshotInputs, _SelectedSnapshotInput
from ._snapshot_identity import _file_identity


def _render_bound_reference(reference: OrcaFileReference, relative_path: str) -> str:
    if reference.kind != "geometry":
        return quote_orca_path(relative_path)
    return unquoted_orca_path(relative_path)


def _rewrite_bound_input(
    lines: list[str],
    references: list[OrcaFileReference],
    private_paths: Mapping[Path, Path],
    *,
    job_dir: Path,
    selected_inp: Path,
    execution_dir: Path,
    inline_geometry_atoms: Mapping[Path, list[str]],
) -> bytes:
    replacements: dict[int, list[tuple[int, int, str]]] = {}
    inline_replacements: dict[int, str] = {}
    for reference in references:
        source = _confined_reference_path(job_dir, selected_inp, reference.value)
        if reference.kind == "geometry" and source in inline_geometry_atoms:
            match = geometry_header_match(lines[reference.line_index])
            if match is None or match.group(1).lower() != "xyzfile":
                raise ValueError("ORCA mutable geometry reference cannot be safely inlined")
            if reference.line_index in inline_replacements:
                raise ValueError("ORCA selected input has duplicate mutable geometry references")
            inline_replacements[reference.line_index] = "\n".join(
                [
                    f"* xyz {match.group(2)} {match.group(3)}",
                    *inline_geometry_atoms[source],
                    "*",
                ]
            )
            continue
        private_path = private_paths[source]
        relative = private_path.relative_to(execution_dir).as_posix()
        replacements.setdefault(reference.line_index, []).append(
            (reference.start, reference.end, _render_bound_reference(reference, relative))
        )
    rewritten = list(lines)
    for line_index, replacement in inline_replacements.items():
        if line_index in replacements:
            raise ValueError("ORCA geometry line has conflicting snapshot rewrites")
        rewritten[line_index] = replacement
    for line_index, line_replacements in replacements.items():
        updated = rewritten[line_index]
        for start, end, replacement in sorted(line_replacements, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
        rewritten[line_index] = updated
    return ("\n".join(rewritten).rstrip() + "\n").encode("utf-8")


def _write_bound_selected_snapshot(
    resolved_job_dir: Path,
    execution_dir: Path,
    source_selected: Path,
    selected: _SelectedSnapshotInput,
    materialized: _MaterializedSnapshotInputs,
) -> tuple[Path, dict[str, Any]]:
    bound_payload = _rewrite_bound_input(
        selected.lines,
        selected.references,
        materialized.private_paths,
        job_dir=resolved_job_dir,
        selected_inp=source_selected,
        execution_dir=execution_dir,
        inline_geometry_atoms=materialized.inline_geometry_atoms,
    )
    if materialized.recovery_checkpoint_private is not None:
        bound_lines = bound_payload.decode("utf-8").splitlines()
        ensure_route_keywords(bound_lines, ["MORead"])
        set_moinp(bound_lines, materialized.recovery_checkpoint_private, execution_dir)
        validate_unambiguous_orca_directives(
            bound_lines,
            label="ORCA recovery bound input",
        )
        bound_payload = ("\n".join(bound_lines).rstrip() + "\n").encode("utf-8")
    if materialized.consumed_bytes + len(bound_payload) > MAX_ORCA_AGGREGATE_SNAPSHOT_BYTES:
        raise ValueError("ORCA submission inputs exceed the aggregate snapshot size limit")
    bound_selected = execution_dir / source_selected.name
    _write_private_input(
        execution_dir,
        bound_selected,
        bound_payload,
        label="ORCA bound selected input",
    )
    return bound_selected, _file_identity(bound_selected)
