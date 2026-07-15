from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import atomic_write_confined_bytes
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils import atomic_write_json, normalize_text
from orca_auto.core.utils.persistence import load_json_mapping_file
from orca_auto.flow._orca_stage_materialization import ensure_route_line
from orca_auto.flow.orchestration.stage_views import WorkflowStageView
from orca_auto.orca.input_artifacts import derive_selected_input_xyz
from orca_auto.orca.input_blocks import (
    GEOM_HEADER_RE,
    OrcaFileReference,
    geometry_range,
    is_safe_unquoted_orca_path,
    quote_orca_path,
    route_line_indices,
    scan_orca_file_references,
    set_block_key_value,
    unquoted_orca_path,
)
from orca_auto.orca.resource_directives import maxcore_mb_per_core, read_nprocs, set_maxcore

_RESTART_SUFFIX_RE = re.compile(r"\.restart-(\d+)$")
_RESERVED_GEOMETRY_PATH = Path(".orca_auto_inputs") / "geometry.xyz"


@dataclass(frozen=True)
class _AuxiliaryCopy:
    reference: OrcaFileReference
    source_path: Path
    target_path: Path
    relative_path: Path


def _resolved_file(value: Any, *, base_dir: Path | None = None) -> Path | None:
    text = normalize_text(value)
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def _next_restart_dir(reaction_dir: Path) -> Path:
    base_name = _RESTART_SUFFIX_RE.sub("", reaction_dir.name)
    latest = 0
    for candidate in reaction_dir.parent.glob(f"{base_name}.restart-*"):
        match = _RESTART_SUFFIX_RE.search(candidate.name)
        if match:
            latest = max(latest, int(match.group(1)))
    return reaction_dir.parent / f"{base_name}.restart-{latest + 1:03d}"


def _selected_input_paths(
    payload: dict[str, Any],
    enqueue_payload: dict[str, Any],
    *,
    allowed_root: Path,
) -> tuple[Path, Path, Path | None]:
    reaction_dir_text = normalize_text(
        enqueue_payload.get("reaction_dir") or payload.get("reaction_dir")
    )
    if not reaction_dir_text:
        raise ValueError("Cannot rematerialize ORCA restart input without reaction_dir")
    reaction_dir = Path(reaction_dir_text).expanduser().resolve()
    _restart_relative_path(
        reaction_dir,
        reaction_dir=allowed_root,
        label="reaction directory",
    )
    if not reaction_dir.is_dir():
        raise FileNotFoundError(f"ORCA restart reaction_dir not found: {reaction_dir}")

    selected_inp_text = normalize_text(
        enqueue_payload.get("selected_inp") or payload.get("selected_inp")
    )
    if selected_inp_text:
        selected_inp = _resolved_file(selected_inp_text, base_dir=reaction_dir)
        if selected_inp is None:
            raise FileNotFoundError(f"ORCA restart selected input not found: {selected_inp_text}")
        if selected_inp.suffix.lower() != ".inp":
            raise ValueError(f"ORCA restart selected input must be an .inp file: {selected_inp}")
    else:
        candidates = sorted(reaction_dir.glob("*.inp"), key=lambda path: path.name.lower())
        selected_inp = candidates[0].resolve() if len(candidates) == 1 else None
    if selected_inp is None:
        raise FileNotFoundError(
            f"ORCA restart selected input is missing or ambiguous: {reaction_dir}"
        )
    _restart_relative_path(
        selected_inp,
        reaction_dir=reaction_dir,
        label="selected input",
    )

    selected_xyz = _resolved_file(payload.get("selected_input_xyz"), base_dir=reaction_dir)
    if selected_xyz is not None and selected_xyz.suffix.lower() != ".xyz":
        selected_xyz = None
    if selected_xyz is None:
        selected_xyz = _resolved_file(derive_selected_input_xyz(selected_inp))
    if selected_xyz is not None and selected_xyz.suffix.lower() != ".xyz":
        selected_xyz = None
    if selected_xyz is not None:
        _restart_relative_path(
            selected_xyz,
            reaction_dir=reaction_dir,
            label="selected geometry",
        )
    return reaction_dir, selected_inp, selected_xyz


def _auxiliary_copy_plan(
    lines: list[str],
    *,
    source_dir: Path,
    source_root: Path,
    target_dir: Path,
    reserved_copies: Mapping[Path, Path] | None,
) -> list[_AuxiliaryCopy]:
    source_base = source_dir.resolve()
    resolved_source_root = source_root.resolve()
    target_root = target_dir.resolve()
    target_sources: dict[Path, Path] = {}
    for target, source in (reserved_copies or {}).items():
        _register_copy_target(
            target_sources,
            target_path=target.resolve(),
            source_path=source.resolve(),
        )
    copies: list[_AuxiliaryCopy] = []
    # The geometry line is rewritten separately by ``_rewrite_geometry_header``,
    # so only the auxiliary/NEB references are copied here — but they come from
    # the same scanner execution binding uses, so a reference accepted at
    # submission can never be silently dropped on restart.
    for reference in scan_orca_file_references(lines, include_geometry=False):
        raw_path = Path(reference.value).expanduser()
        source_path = (raw_path if raw_path.is_absolute() else source_base / raw_path).resolve()
        try:
            relative_path = source_path.relative_to(resolved_source_root)
        except ValueError as exc:
            raise ValueError(
                f"ORCA auxiliary input escapes the reaction directory: {reference.value}"
            ) from exc
        if not source_path.is_file():
            raise FileNotFoundError(f"ORCA auxiliary input not found: {source_path}")

        target_path = (target_root / relative_path).resolve()
        _register_copy_target(
            target_sources,
            target_path=target_path,
            source_path=source_path,
        )
        copies.append(
            _AuxiliaryCopy(
                reference=reference,
                source_path=source_path,
                target_path=target_path,
                relative_path=relative_path,
            )
        )
    return copies


def _register_copy_target(
    target_sources: dict[Path, Path],
    *,
    target_path: Path,
    source_path: Path,
) -> None:
    for existing_target, existing_source in target_sources.items():
        if target_path == existing_target:
            if source_path == existing_source:
                return
            raise ValueError(
                "ORCA restart copy target collision: "
                f"{target_path} maps to both {existing_source} and {source_path}"
            )
        if target_path in existing_target.parents or existing_target in target_path.parents:
            raise ValueError(
                "ORCA restart copy target collision: "
                f"{target_path} and {existing_target} cannot both be files"
            )
    target_sources[target_path] = source_path


def _copy_auxiliary_input_files(
    lines: list[str],
    *,
    source_dir: Path,
    source_root: Path,
    target_dir: Path,
    reserved_copies: Mapping[Path, Path] | None = None,
) -> None:
    copies = _auxiliary_copy_plan(
        lines,
        source_dir=source_dir,
        source_root=source_root,
        target_dir=target_dir,
        reserved_copies=reserved_copies,
    )
    copied_targets: set[Path] = set()
    for copy in copies:
        target_path = copy.target_path
        if target_path in copied_targets:
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_confined_bytes(
            target_dir,
            target_path,
            read_stable_regular_file(copy.source_path),
            label="ORCA restart auxiliary input",
        )
        copied_targets.add(target_path)

    replacements: dict[int, list[tuple[int, int, str]]] = {}
    for copy in copies:
        reference = copy.reference
        replacements.setdefault(reference.line_index, []).append(
            (
                reference.start,
                reference.end,
                quote_orca_path(copy.relative_path.as_posix()),
            )
        )
    for line_index, line_replacements in replacements.items():
        updated = lines[line_index]
        for start, end, replacement in sorted(line_replacements, reverse=True):
            updated = updated[:start] + replacement + updated[end:]
        lines[line_index] = updated


def _rewrite_geometry_header(
    lines: list[str],
    *,
    charge: int | None,
    multiplicity: int | None,
    selected_xyz_reference: str,
) -> None:
    geometry = geometry_range(lines)
    if geometry is None:
        raise ValueError("ORCA restart input has no xyz/xyzfile geometry header")
    start, end, old_charge, old_multiplicity = geometry
    resolved_charge = old_charge if charge is None else charge
    resolved_multiplicity = old_multiplicity if multiplicity is None else multiplicity
    match = GEOM_HEADER_RE.match(lines[start].strip())
    if match is None:
        raise ValueError("ORCA restart input has an invalid geometry header")
    geometry_type = match.group(1).lower()
    if selected_xyz_reference:
        selected_xyz_ref = unquoted_orca_path(selected_xyz_reference)
        lines[start:end] = [
            f"* xyzfile {resolved_charge} {resolved_multiplicity} {selected_xyz_ref}"
        ]
        return
    if geometry_type == "xyzfile":
        raise FileNotFoundError("ORCA restart xyzfile geometry is missing")
    lines[start] = f"* xyz {resolved_charge} {resolved_multiplicity}"


def _rewrite_restart_input(
    source_inp: Path,
    target_inp: Path,
    *,
    source_root: Path,
    source_xyz: Path | None,
    target_xyz: Path | None,
    settings: dict[str, Any],
) -> None:
    lines = source_inp.read_text(encoding="utf-8", errors="ignore").splitlines()
    reserved_copies: dict[Path, Path] = {
        target_inp: target_inp,
        target_inp.parent / "source_candidate.json": target_inp.parent / "source_candidate.json",
        target_inp.parent / "enqueue_payload.json": target_inp.parent / "enqueue_payload.json",
    }
    if target_xyz is not None and source_xyz is not None:
        reserved_copies[target_xyz] = source_xyz
    _copy_auxiliary_input_files(
        lines,
        source_dir=source_inp.parent,
        source_root=source_root,
        target_dir=target_inp.parent,
        reserved_copies=reserved_copies,
    )
    if bool(settings.get("orca_route_line_present")):
        route_indices = route_line_indices(lines)
        route_line = ensure_route_line(normalize_text(settings.get("orca_route_line")))
        if not route_indices:
            lines.insert(0, route_line)
        else:
            lines[route_indices[0]] = route_line
            for route_index in reversed(route_indices[1:]):
                del lines[route_index]

    charge_update = settings.get("orca_charge")
    multiplicity_update = settings.get("orca_multiplicity")
    _rewrite_geometry_header(
        lines,
        charge=int(charge_update) if isinstance(charge_update, int) else None,
        multiplicity=(int(multiplicity_update) if isinstance(multiplicity_update, int) else None),
        selected_xyz_reference=(
            target_xyz.relative_to(target_inp.parent).as_posix() if target_xyz is not None else ""
        ),
    )

    resources = settings.get("resources")
    if isinstance(resources, dict) and resources:
        max_cores = int(resources.get("max_cores", 0) or 0)
        max_memory_gb = int(resources.get("max_memory_gb", 0) or 0)
        if max_cores > 0:
            set_block_key_value(lines, "pal", "nprocs", str(max_cores))
        if max_memory_gb > 0:
            effective_cores = max_cores if max_cores > 0 else (read_nprocs(lines) or 1)
            set_maxcore(
                lines,
                maxcore_mb_per_core(
                    max_memory_gb=max_memory_gb,
                    max_cores=effective_cores,
                ),
            )
    atomic_write_confined_bytes(
        target_inp.parent,
        target_inp,
        ("\n".join(lines).rstrip() + "\n").encode("utf-8"),
        label="ORCA restart input",
    )


def _restart_relative_path(path: Path, *, reaction_dir: Path, label: str) -> Path:
    try:
        return path.resolve().relative_to(reaction_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"ORCA restart {label} escapes the reaction directory: {path}") from exc


def _restart_geometry_relative_path(path: Path, *, reaction_dir: Path) -> Path:
    relative_path = _restart_relative_path(
        path,
        reaction_dir=reaction_dir,
        label="selected geometry",
    )
    if is_safe_unquoted_orca_path(relative_path.as_posix()):
        return relative_path
    return _RESERVED_GEOMETRY_PATH


def _source_payload(
    reaction_dir: Path,
    *,
    previous_inp: Path,
    previous_xyz: Path | None,
) -> dict[str, Any]:
    source_path = reaction_dir / "source_candidate.json"
    if source_path.exists() or source_path.is_symlink():
        _restart_relative_path(
            source_path,
            reaction_dir=reaction_dir,
            label="source metadata",
        )
    source = load_json_mapping_file(source_path) or {}
    source["restart_provenance"] = {
        "previous_reaction_dir": str(reaction_dir),
        "previous_selected_inp": str(previous_inp),
        "previous_selected_input_xyz": str(previous_xyz) if previous_xyz is not None else "",
    }
    return source


def _update_command_paths(
    enqueue_payload: dict[str, Any], *, previous_dir: Path, reaction_dir: Path
) -> None:
    argv = enqueue_payload.get("command_argv")
    if isinstance(argv, list) and "run-dir" in argv:
        index = argv.index("run-dir")
        if index + 1 < len(argv):
            argv[index + 1] = str(reaction_dir)
    command = enqueue_payload.get("command")
    if isinstance(command, str):
        enqueue_payload["command"] = command.replace(str(previous_dir), str(reaction_dir))


def rematerialize_orca_restart_input(
    stage: dict[str, Any],
    settings: dict[str, Any],
    *,
    allowed_root: Path,
    created_restart_dirs: list[Path] | None = None,
) -> bool:
    """Create an isolated ORCA input generation and repoint a restart stage."""

    if not bool(settings.get("orca_input_updates")):
        return False
    stage_view = WorkflowStageView(stage)
    task_view = stage_view.ensure_task()
    payload = task_view.payload(None)
    enqueue_payload = task_view.enqueue_payload()
    previous_dir, previous_inp, previous_xyz = _selected_input_paths(
        payload,
        enqueue_payload,
        allowed_root=allowed_root.expanduser().resolve(),
    )
    reaction_dir = _next_restart_dir(previous_dir)
    _restart_relative_path(
        reaction_dir,
        reaction_dir=allowed_root,
        label="target reaction directory",
    )
    reaction_dir.mkdir(parents=True, exist_ok=False)
    try:
        target_xyz = (
            reaction_dir
            / _restart_geometry_relative_path(
                previous_xyz,
                reaction_dir=previous_dir,
            )
            if previous_xyz is not None
            else None
        )
        target_inp = reaction_dir / previous_inp.name
        _rewrite_restart_input(
            previous_inp,
            target_inp,
            source_root=previous_dir,
            source_xyz=previous_xyz,
            target_xyz=target_xyz,
            settings=settings,
        )
        if previous_xyz is not None and target_xyz is not None:
            target_xyz.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_confined_bytes(
                reaction_dir,
                target_xyz,
                read_stable_regular_file(previous_xyz),
                label="ORCA restart geometry",
            )
        provenance = _source_payload(
            previous_dir,
            previous_inp=previous_inp,
            previous_xyz=previous_xyz,
        )
        atomic_write_json(
            reaction_dir / "source_candidate.json",
            provenance,
            ensure_ascii=True,
            indent=2,
        )
        updated_enqueue_payload = dict(enqueue_payload)
        updated_enqueue_payload.update(
            {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(target_inp),
                "force": True,
            }
        )
        _update_command_paths(
            updated_enqueue_payload,
            previous_dir=previous_dir,
            reaction_dir=reaction_dir,
        )
        atomic_write_json(
            reaction_dir / "enqueue_payload.json",
            updated_enqueue_payload,
            ensure_ascii=True,
            indent=2,
        )

        payload.update(
            {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(target_inp),
                "selected_input_xyz": str(target_xyz) if target_xyz is not None else "",
                "suggested_command": f"orca_auto run-dir '{reaction_dir}'",
                "restart_provenance": provenance["restart_provenance"],
            }
        )
        enqueue_payload.clear()
        enqueue_payload.update(updated_enqueue_payload)
        task_view.update_metadata(
            {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(target_inp),
            }
        )
        stage_view.update_metadata(
            {
                "reaction_dir": str(reaction_dir),
                "restart_provenance": provenance["restart_provenance"],
            }
        )
    except Exception:
        shutil.rmtree(reaction_dir, ignore_errors=True)
        raise

    if created_restart_dirs is not None:
        created_restart_dirs.append(reaction_dir)
    return True


__all__ = ["rematerialize_orca_restart_input"]
