"""Interaction-energy single-point fan-out for conformer_screening.

Once every conformer optimization stage is terminal, this appends the single
points needed for ΔE_int = E(complex) − Σ E(fragment): one fresh complex single
point plus one per fragment, all at ``interaction_energy.sp_route_line`` on the
complex-optimized geometry, so complex and fragments share the exact level of
theory and geometry.

The fan-out is bounded and fail-closed:

* It only targets the RMSD-deduplication representatives (computed from the
  optimized geometries), so degenerate conformers do not each spawn a fan-out.
* ``max_fragments`` caps how many fragment single points a complex can spawn.
* Materialization is resumable: it re-creates only the missing stages of a
  complex's expected set (complex + every fragment), so a partial failure is
  completed on a later advance rather than stranded.
* A fragment set that does not partition the complex is skipped (no ΔE_int),
  never a partial-atom single point.

Fragment/complex stages carry ``role`` metadata (``interaction_complex_sp`` /
``interaction_fragment``) so the SI layer routes them to the ΔE_int table and
never into the stationary-structure path.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.statuses import STATUS_COMPLETED, is_stage_terminal_status
from orca_auto.core.utils.coercion import normalize_text, safe_int
from orca_auto.flow._orca_stage_materialization import build_materialized_orca_stage, safe_name
from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.manifest import (
    DEFAULT_INTERACTION_SP_ROUTE_LINE,
    DEFAULT_RMSD_ENERGY_WINDOW_KCAL,
    DEFAULT_RMSD_THRESHOLD_ANGSTROM,
    INTERACTION_ENERGY_MAX_FRAGMENTS_CAP,
    interaction_energy_config_fingerprint,
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    validate_interaction_energy_state_balance,
)
from orca_auto.flow.orchestration.dep_types import OrchestrationDeps
from orca_auto.flow.state import workflow_workspace_internal_engine_paths
from orca_auto.flow.xyz_utils import write_fragment_xyz
from orca_auto.orca.input_blocks import file_route_lines, geometry_range
from orca_auto.orca.report.interaction_energy import (
    validate_fragment_electronic_states,
    validate_fragment_partition,
)
from orca_auto.orca.report.rmsd import RmsdCandidate, group_by_rmsd, rmsd_comparison_key
from orca_auto.orca.report.si import SiBlock, SiBlockError, collect_si_block
from orca_auto.orca.state import load_state

logger = logging.getLogger(__name__)

_INTERACTION_ROLE_PREFIX = "interaction_"
_ROLE_COMPLEX = "interaction_complex_sp"
_ROLE_FRAGMENT = "interaction_fragment"
_INTERACTION_SOURCE_DIRNAME = "_interaction_sources"
_INTERACTION_CONFIG_FINGERPRINT_KEY = "interaction_config_fingerprint"
_GEOMETRY_TOL_ANGSTROM = 1e-4


def _text(value: Any) -> str:
    return normalize_text(value)


def _stage_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return []
    return [stage for stage in stages if isinstance(stage, dict)]


def _stage_metadata(stage: Mapping[str, Any]) -> dict[str, Any]:
    metadata = stage.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _stage_role(stage: Mapping[str, Any]) -> str:
    return _text(_stage_metadata(stage).get("role"))


def _task_kind(stage: Mapping[str, Any]) -> str:
    task = stage.get("task")
    return _text(task.get("task_kind")) if isinstance(task, Mapping) else ""


def _request_parameters(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    request = metadata.get("request")
    if not isinstance(request, dict):
        return {}
    parameters = request.get("parameters")
    return parameters if isinstance(parameters, dict) else {}


def _orca_reaction_dir(stage: Mapping[str, Any]) -> Path | None:
    artifacts = stage.get("output_artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and _text(artifact.get("kind")) == "orca_output_dir":
                path_text = _text(artifact.get("path"))
                if path_text:
                    return Path(path_text)
    reaction_dir = _text(_stage_metadata(stage).get("reaction_dir"))
    return Path(reaction_dir) if reaction_dir else None


def _selected_input_state_matches(block: SiBlock, state: Mapping[str, Any]) -> bool:
    selected_raw = _text(state.get("selected_inp"))
    if not selected_raw:
        return False
    try:
        selected_geometry = geometry_range(
            Path(selected_raw).read_text(encoding="utf-8", errors="ignore").splitlines()
        )
    except OSError:
        return False
    selected_route = " ".join(file_route_lines(Path(selected_raw)))
    normalized_selected_route = " ".join(selected_route.replace("!", " ").split()).lower()
    normalized_output_route = " ".join(block.result.input_line.split()).lower()
    return (
        selected_geometry is not None
        and (
            block.result.charge,
            block.result.multiplicity,
        )
        == (selected_geometry[2], selected_geometry[3])
        and bool(normalized_selected_route)
        and normalized_selected_route == normalized_output_route
    )


def _completed_complex_block(
    stage: Mapping[str, Any],
    *,
    expected_charge: int,
    expected_multiplicity: int,
) -> SiBlock | None:
    if _task_kind(stage) != "opt":
        return None
    reaction_dir = _orca_reaction_dir(stage)
    if reaction_dir is None:
        return None
    state = load_state(reaction_dir)
    if state is None:
        return None
    try:
        block = collect_si_block(reaction_dir, state)
    except SiBlockError:
        return None
    if (
        block is None
        or block.kind != "min"
        or block.result.opt_converged is not True
        or block.imaginary_count not in (None, 0)
        or block.result.energy_hartree is None
        or not block.result.coordinates
        or not block.result.electronic_state_verified
        or block.result.charge != expected_charge
        or block.result.multiplicity != expected_multiplicity
        or not block.result.input_line.strip()
        or not block.result.orca_version.strip()
        or not _selected_input_state_matches(block, state)
    ):
        return None
    # Fail closed on non-finite parsed data so a corrupt optimized geometry can
    # never seed the RMSD grouping (the report path guards this too).
    if not math.isfinite(block.result.energy_hartree):
        return None
    if any(
        not math.isfinite(value) for _element, *xyz in block.result.coordinates for value in xyz
    ):
        return None
    return block


def _completed_single_point_block(stage: Mapping[str, Any]) -> SiBlock | None:
    if _task_kind(stage) != "sp":
        return None
    reaction_dir = _orca_reaction_dir(stage)
    if reaction_dir is None:
        return None
    state = load_state(reaction_dir)
    if state is None:
        return None
    try:
        block = collect_si_block(reaction_dir, state)
    except SiBlockError:
        return None
    if (
        block is None
        or block.analysis is not None
        or block.result.energy_hartree is None
        or not block.result.coordinates
        or not block.result.input_line.strip()
        or not block.result.orca_version.strip()
        or not block.result.electronic_state_verified
        or not _selected_input_state_matches(block, state)
        or not math.isfinite(block.result.energy_hartree)
    ):
        return None
    if any(
        not math.isfinite(value) for _element, *xyz in block.result.coordinates for value in xyz
    ):
        return None
    return block


def _blocks_match_geometry_and_state(left: SiBlock, right: SiBlock) -> bool:
    a = left.result.coordinates
    b = right.result.coordinates
    if len(a) != len(b) or not a:
        return False
    if (left.result.charge, left.result.multiplicity) != (
        right.result.charge,
        right.result.multiplicity,
    ):
        return False
    for left_row, right_row in zip(a, b, strict=True):
        if left_row[0] != right_row[0]:
            return False
        if any(
            abs(x - y) > _GEOMETRY_TOL_ANGSTROM
            for x, y in zip(left_row[1:], right_row[1:], strict=True)
        ):
            return False
    return True


def _uniform_single_point_energies(
    optimized: list[tuple[str, dict[str, Any], SiBlock]],
    single_points: list[SiBlock],
) -> dict[str, float]:
    if not optimized:
        return {}
    matches: dict[str, list[int]] = {}
    match_counts = [0] * len(single_points)
    for stage_id, _stage, block in optimized:
        indices = [
            index
            for index, single_point in enumerate(single_points)
            if _blocks_match_geometry_and_state(block, single_point)
        ]
        matches[stage_id] = indices
        for index in indices:
            match_counts[index] += 1
    if any(len(indices) != 1 for indices in matches.values()) or any(
        count > 1 for count in match_counts
    ):
        return {}
    matched_indices = {indices[0] for indices in matches.values()}
    levels = {
        (
            block.result.method,
            block.result.basis_set,
            block.result.solvation,
            block.result.orca_version,
            block.result.input_line,
        )
        for index, block in enumerate(single_points)
        if index in matched_indices
    }
    if len(levels) != 1:
        return {}
    energies: dict[str, float] = {}
    for stage_id, indices in matches.items():
        energy = single_points[indices[0]].result.energy_hartree
        if energy is None:
            return {}
        energies[stage_id] = energy
    return energies


def _existing_interaction_keys(stages: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    """(role, parent_stage_id, fragment_index) already present, for idempotency."""
    keys: set[tuple[str, str, int]] = set()
    for stage in stages:
        role = _stage_role(stage)
        if not role.startswith(_INTERACTION_ROLE_PREFIX):
            continue
        meta = _stage_metadata(stage)
        parent = _text(meta.get("parent_stage_id"))
        index = safe_int(meta.get("fragment_index", -1), default=-1)
        keys.add((role, parent, index))
    return keys


def _rmsd_representative_ids(
    parsed: list[tuple[str, dict[str, Any], SiBlock]],
    rmsd_cfg: Mapping[str, Any] | None,
    *,
    effective_energies: Mapping[str, float] | None = None,
) -> frozenset[str]:
    rmsd_cfg = rmsd_cfg or {}
    grouping = group_by_rmsd(
        [
            RmsdCandidate(
                stage_id=stage_id,
                coordinates=tuple(block.result.coordinates),
                energy_hartree=(effective_energies or {}).get(
                    stage_id, block.result.energy_hartree
                ),
                comparison_key=rmsd_comparison_key(
                    formula=block.result.formula,
                    charge=block.result.charge,
                    multiplicity=block.result.multiplicity,
                    method=block.result.method,
                    basis_set=block.result.basis_set,
                    solvation=block.result.solvation,
                    orca_version=block.result.orca_version,
                    input_line=block.result.input_line,
                    electronic_state_verified=block.result.electronic_state_verified,
                ),
            )
            for stage_id, _stage, block in parsed
        ],
        rmsd_threshold_angstrom=float(
            rmsd_cfg.get("rmsd_threshold_angstrom") or DEFAULT_RMSD_THRESHOLD_ANGSTROM
        ),
        energy_window_kcal=float(
            rmsd_cfg.get("energy_window_kcal") or DEFAULT_RMSD_ENERGY_WINDOW_KCAL
        ),
        heavy_atoms_only=bool(rmsd_cfg.get("heavy_atoms_only", False)),
    )
    return grouping.representative_ids


def _append_interaction_stage(
    *,
    payload: dict[str, Any],
    allowed_root: Path,
    source_xyz: Path,
    stage_id: str,
    stage_key: str,
    kind: str,
    route_line: str,
    charge: int,
    multiplicity: int,
    priority: int,
    max_cores: int,
    max_memory_gb: int,
    metadata: dict[str, Any],
) -> None:
    candidate = WorkflowStageInput(
        source_job_id="",
        source_job_type="interaction_energy",
        reaction_key=_text(payload.get("reaction_key")),
        selected_input_xyz=str(source_xyz),
        rank=1,
        kind=kind,
        artifact_path=str(source_xyz),
        selected=True,
        score=0.0,
        metadata={"source_frame_index": 0},
    )
    stage = build_materialized_orca_stage(
        workflow_id=_text(payload.get("workflow_id")),
        template_name="conformer_screening",
        stage_id=stage_id,
        stage_key=stage_key,
        stage_root_name="",
        workspace_dir=allowed_root,
        input_artifact_kind=kind,
        candidate=candidate,
        task_kind="sp",
        route_line=route_line,
        charge=charge,
        multiplicity=multiplicity,
        max_cores=max_cores,
        max_memory_gb=max_memory_gb,
        priority=priority,
        xyz_filename="sp.xyz",
        inp_filename="sp.inp",
    )
    stage_dict = stage.to_dict()
    stage_metadata = stage_dict.setdefault("metadata", {})
    if isinstance(stage_metadata, dict):
        stage_metadata.update(metadata)
    payload.setdefault("stages", []).append(stage_dict)


def append_interaction_energy_stages_impl(
    payload: dict[str, Any],
    *,
    workspace_dir: Path,
    deps: OrchestrationDeps | None = None,
) -> bool:
    del deps
    if _text(payload.get("template_name")) != "conformer_screening":
        return False
    params = _request_parameters(payload)
    cfg = params.get("interaction_energy")
    try:
        normalized_cfg = normalize_interaction_energy_block(cfg)
    except ValueError:
        logger.warning(
            "interaction_energy fan-out skipped: invalid durable configuration", exc_info=True
        )
        return False
    if normalized_cfg is None:
        return False
    cfg = normalized_cfg
    try:
        rmsd_cfg = normalize_rmsd_dedup_block(params.get("rmsd_dedup"))
    except ValueError:
        logger.warning(
            "interaction_energy fan-out skipped: invalid durable RMSD configuration",
            exc_info=True,
        )
        return False
    fragments = cfg.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        return False
    max_fragments = safe_int(cfg.get("max_fragments"), default=0)
    if (
        max_fragments < 2
        or max_fragments > INTERACTION_ENERGY_MAX_FRAGMENTS_CAP
        or len(fragments) > max_fragments
        or len(fragments) > INTERACTION_ENERGY_MAX_FRAGMENTS_CAP
    ):
        logger.warning(
            "interaction_energy fan-out skipped: %d fragments violate max_fragments %d / hard cap %d",
            len(fragments),
            max_fragments,
            INTERACTION_ENERGY_MAX_FRAGMENTS_CAP,
        )
        return False

    complex_charge = safe_int(params.get("charge", 0), default=0)
    complex_multiplicity = safe_int(params.get("multiplicity", 1), default=1)
    try:
        validate_interaction_energy_state_balance(
            cfg,
            complex_charge=complex_charge,
            complex_multiplicity=complex_multiplicity,
        )
    except ValueError:
        logger.warning(
            "interaction_energy fan-out skipped: fragment electronic states are incompatible",
            exc_info=True,
        )
        return False

    stages = _stage_dicts(payload)
    orca_stages = [stage for stage in stages if _text(stage.get("stage_kind")) == "orca_stage"]
    complex_stages = [
        stage
        for stage in orca_stages
        if not _stage_role(stage).startswith(_INTERACTION_ROLE_PREFIX)
    ]
    if not complex_stages:
        return False
    # Fire only once the primary ORCA set is terminal. Partial-success conformer
    # workflows use their completed subset; restart refuses to reopen failed
    # primary stages after interaction children exist unless the feature is
    # explicitly disabled and those children are retired.
    if not all(is_stage_terminal_status(_text(stage.get("status"))) for stage in complex_stages):
        return False

    parsed: list[tuple[str, dict[str, Any], SiBlock]] = []
    single_points: list[SiBlock] = []
    for stage in complex_stages:
        if _text(stage.get("status")) != STATUS_COMPLETED:
            continue
        if _task_kind(stage) == "opt":
            block = _completed_complex_block(
                stage,
                expected_charge=complex_charge,
                expected_multiplicity=complex_multiplicity,
            )
            if block is None:
                continue
            parsed.append((_text(stage.get("stage_id")), stage, block))
            continue
        if _task_kind(stage) == "sp":
            single_point = _completed_single_point_block(stage)
            if single_point is not None:
                single_points.append(single_point)
    if not parsed:
        return False

    representative_ids = _rmsd_representative_ids(
        parsed,
        rmsd_cfg,
        effective_energies=_uniform_single_point_energies(parsed, single_points),
    )
    representatives = [item for item in parsed if item[0] in representative_ids]
    fragment_index_lists = [fragment.get("atom_indices", []) for fragment in fragments]
    sp_route_line = _text(cfg.get("sp_route_line")) or DEFAULT_INTERACTION_SP_ROUTE_LINE
    config_fingerprint = interaction_energy_config_fingerprint(
        cfg,
        complex_charge=complex_charge,
        complex_multiplicity=complex_multiplicity,
        rmsd_dedup=rmsd_cfg,
    )
    priority = safe_int(cfg.get("priority", params.get("priority", 10)), default=10)
    max_cores = safe_int(cfg.get("max_cores", params.get("max_cores", 8)), default=8)
    max_memory_gb = safe_int(cfg.get("max_memory_gb", params.get("max_memory_gb", 32)), default=32)

    existing_interaction = [
        stage for stage in orca_stages if _stage_role(stage).startswith(_INTERACTION_ROLE_PREFIX)
    ]
    if any(
        _text(_stage_metadata(stage).get(_INTERACTION_CONFIG_FINGERPRINT_KEY)) != config_fingerprint
        for stage in existing_interaction
    ):
        logger.warning(
            "interaction_energy fan-out skipped: persisted stages belong to another config generation"
        )
        return False
    existing = _existing_interaction_keys(orca_stages)
    orca_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="orca")
    allowed_root = orca_paths["allowed_root"]
    source_root = allowed_root / _INTERACTION_SOURCE_DIRNAME
    created = 0

    for stage_id, _stage, block in representatives:
        coordinates = list(block.result.coordinates)
        natoms = len(coordinates)
        reason = validate_fragment_partition(fragment_index_lists, natoms)
        if reason:
            logger.warning("interaction_energy fan-out skipped for %s: %s", stage_id, reason)
            continue
        state_reason = validate_fragment_electronic_states(
            [row[0] for row in coordinates], fragments
        )
        if state_reason:
            logger.warning("interaction_energy fan-out skipped for %s: %s", stage_id, state_reason)
            continue
        safe_parent = safe_name(stage_id, fallback="complex")

        if (_ROLE_COMPLEX, stage_id, -1) not in existing:
            complex_xyz = source_root / f"{safe_parent}_complex.xyz"
            write_fragment_xyz(
                coordinates=coordinates,
                atom_indices=list(range(natoms)),
                target_path=complex_xyz,
                comment=f"interaction complex {stage_id}",
            )
            _append_interaction_stage(
                payload=payload,
                allowed_root=allowed_root,
                source_xyz=complex_xyz,
                stage_id=f"orca_ie_{safe_parent}_complex",
                stage_key=f"ie_{safe_parent}_complex",
                kind="interaction_complex",
                route_line=sp_route_line,
                charge=complex_charge,
                multiplicity=complex_multiplicity,
                priority=priority,
                max_cores=max_cores,
                max_memory_gb=max_memory_gb,
                metadata={
                    "role": _ROLE_COMPLEX,
                    "parent_stage_id": stage_id,
                    _INTERACTION_CONFIG_FINGERPRINT_KEY: config_fingerprint,
                },
            )
            created += 1

        for index, fragment in enumerate(fragments):
            if (_ROLE_FRAGMENT, stage_id, index) in existing:
                continue
            atom_indices = [int(value) for value in fragment.get("atom_indices", [])]
            label = _text(fragment.get("label")) or f"fragment_{index + 1}"
            fragment_charge = safe_int(fragment.get("charge", 0), default=0)
            fragment_multiplicity = safe_int(fragment.get("multiplicity", 1), default=1)
            fragment_xyz = source_root / f"{safe_parent}_f{index:02d}.xyz"
            write_fragment_xyz(
                coordinates=coordinates,
                atom_indices=atom_indices,
                target_path=fragment_xyz,
                comment=f"interaction fragment {label} of {stage_id}",
            )
            _append_interaction_stage(
                payload=payload,
                allowed_root=allowed_root,
                source_xyz=fragment_xyz,
                stage_id=f"orca_ie_{safe_parent}_f{index:02d}",
                stage_key=f"ie_{safe_parent}_f{index:02d}",
                kind="interaction_fragment",
                route_line=sp_route_line,
                charge=fragment_charge,
                multiplicity=fragment_multiplicity,
                priority=priority,
                max_cores=max_cores,
                max_memory_gb=max_memory_gb,
                metadata={
                    "role": _ROLE_FRAGMENT,
                    "parent_stage_id": stage_id,
                    "fragment_index": index,
                    "fragment_label": label,
                    "fragment_charge": fragment_charge,
                    "fragment_multiplicity": fragment_multiplicity,
                    "fragment_atom_indices": list(atom_indices),
                    _INTERACTION_CONFIG_FINGERPRINT_KEY: config_fingerprint,
                },
            )
            created += 1

    return created > 0


__all__ = ["append_interaction_energy_stages_impl"]
