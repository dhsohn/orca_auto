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

from orca_auto.core.engine_process import ensure_confined_directory
from orca_auto.core.statuses import STATUS_COMPLETED, is_stage_terminal_status
from orca_auto.core.utils.coercion import normalize_text, safe_int
from orca_auto.flow._orca_stage_materialization import build_materialized_orca_stage, safe_name
from orca_auto.flow.conformer_selection import (
    OrcaSelectedInputScienceIdentity,
    eligible_minimum_block,
    finite,
    has_required_provenance,
    rmsd_candidate_for_block,
    rmsd_grouping,
    unique_single_point_matches,
)
from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.contracts.workflow import (
    INTERACTION_COMPLEX_SP_ROLE,
    INTERACTION_CONFIG_FINGERPRINT_KEY,
    INTERACTION_FRAGMENT_ROLE,
    is_exact_orca_stage_contract,
    is_interaction_role,
    is_orca_stage_kind,
    is_valid_interaction_stage_contract,
    required_route_line,
    workflow_request_parameters,
    workflow_stage_dicts,
)
from orca_auto.flow.manifest import (
    INTERACTION_ENERGY_MAX_FRAGMENTS_CAP,
    interaction_energy_config_fingerprint,
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    require_int,
    validate_interaction_energy_state_balance,
)
from orca_auto.flow.orca_stage_evidence import collect_verified_orca_stage_evidence
from orca_auto.flow.state import workflow_workspace_internal_engine_paths
from orca_auto.flow.xyz_utils import write_fragment_xyz
from orca_auto.orca.report.interaction_energy import (
    validate_fragment_electronic_states,
    validate_fragment_partition,
)
from orca_auto.orca.report.si import SiBlock

logger = logging.getLogger(__name__)

_INTERACTION_SOURCE_DIRNAME = "_interaction_sources"

_OptimizedEvidence = tuple[
    str,
    dict[str, Any],
    SiBlock,
    OrcaSelectedInputScienceIdentity,
]


def _text(value: Any) -> str:
    return normalize_text(value)


def _stage_metadata(stage: Mapping[str, Any]) -> dict[str, Any]:
    metadata = stage.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _stage_role(stage: Mapping[str, Any]) -> str:
    return _text(_stage_metadata(stage).get("role"))


def _task_kind(stage: Mapping[str, Any]) -> str:
    task = stage.get("task")
    return _text(task.get("task_kind")) if isinstance(task, Mapping) else ""


def _record_interaction_energy_error(
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
) -> None:
    metadata = payload.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return
    metadata["workflow_error"] = {
        "status": "failed",
        "scope": "conformer_screening_interaction_energy",
        "reason": reason,
        "message": message,
    }


def _completed_complex_evidence(
    stage: Mapping[str, Any],
    *,
    expected_charge: int,
    expected_multiplicity: int,
) -> tuple[SiBlock, OrcaSelectedInputScienceIdentity] | None:
    if _task_kind(stage) != "opt":
        return None
    block, _reason, selected_input_identity = collect_verified_orca_stage_evidence(stage)
    # ``eligible_minimum_block`` fails closed on non-finite parsed data so a
    # corrupt optimized geometry can never seed the RMSD grouping.
    if (
        block is None
        or selected_input_identity is None
        or not eligible_minimum_block(
            block,
            expected_charge=expected_charge,
            expected_multiplicity=expected_multiplicity,
        )
    ):
        return None
    return block, selected_input_identity


def _completed_single_point_evidence(
    stage: Mapping[str, Any],
) -> tuple[SiBlock, OrcaSelectedInputScienceIdentity] | None:
    if _task_kind(stage) != "sp":
        return None
    block, _reason, selected_input_identity = collect_verified_orca_stage_evidence(stage)
    if (
        block is None
        or selected_input_identity is None
        or block.analysis is not None
        or not finite(block.result.energy_hartree)
        or not block.result.coordinates
        or not has_required_provenance(block)
    ):
        return None
    if any(
        not math.isfinite(value) for _element, *xyz in block.result.coordinates for value in xyz
    ):
        return None
    return block, selected_input_identity


def _uniform_single_point_energies(
    optimized: list[_OptimizedEvidence],
    single_points: list[tuple[SiBlock, OrcaSelectedInputScienceIdentity]],
) -> dict[str, float]:
    if not optimized:
        return {}
    single_point_blocks = [block for block, _identity in single_points]
    unique = unique_single_point_matches(
        [block for _stage_id, _stage, block, _identity in optimized], single_point_blocks
    )
    # All-or-nothing: every optimized entry must have its own unique match, or
    # the single-point energies are not a uniform substitute basis at all.
    if any(index is None for index in unique):
        return {}
    matched_indices = {index for index in unique if index is not None}
    levels = {
        (
            block.result.method,
            block.result.basis_set,
            block.result.solvation,
            block.result.orca_version,
            selected_input_identity,
        )
        for index, (block, selected_input_identity) in enumerate(single_points)
        if index in matched_indices
    }
    if len(levels) != 1:
        return {}
    energies: dict[str, float] = {}
    for (stage_id, _stage, _block, _identity), index in zip(optimized, unique, strict=True):
        assert index is not None  # guaranteed by the all-or-nothing gate above
        energy = single_point_blocks[index].result.energy_hartree
        if energy is None:
            return {}
        energies[stage_id] = energy
    return energies


def _existing_interaction_keys(
    stages: list[dict[str, Any]],
    *,
    expected_config_fingerprint: str,
) -> set[tuple[str, str, int]]:
    """(role, parent_stage_id, fragment_index) already present, for idempotency."""
    keys: set[tuple[str, str, int]] = set()
    for stage in stages:
        role = _stage_role(stage)
        if not is_valid_interaction_stage_contract(
            stage,
            stages,
            expected_config_fingerprint=expected_config_fingerprint,
        ):
            continue
        meta = _stage_metadata(stage)
        parent = _text(meta.get("parent_stage_id"))
        index = safe_int(meta.get("fragment_index", -1), default=-1)
        keys.add((role, parent, index))
    return keys


def _rmsd_representative_ids(
    parsed: list[_OptimizedEvidence],
    rmsd_cfg: Mapping[str, Any] | None,
    *,
    effective_energies: Mapping[str, float] | None = None,
) -> frozenset[str]:
    grouping = rmsd_grouping(
        [
            rmsd_candidate_for_block(
                stage_id,
                block,
                energy_hartree=(effective_energies or {}).get(
                    stage_id, block.result.energy_hartree
                ),
                selected_input_identity=selected_input_identity,
            )
            for stage_id, _stage, block, selected_input_identity in parsed
        ],
        rmsd_cfg,
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
) -> bool:
    if _text(payload.get("template_name")) != "conformer_screening":
        return False
    params = workflow_request_parameters(payload)
    cfg = params.get("interaction_energy")
    try:
        normalized_cfg = normalize_interaction_energy_block(cfg)
    except ValueError:
        _record_interaction_energy_error(
            payload,
            reason="invalid_interaction_energy_config",
            message="The durable interaction-energy configuration is invalid.",
        )
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
        _record_interaction_energy_error(
            payload,
            reason="invalid_rmsd_config",
            message="The durable RMSD configuration required for interaction energy is invalid.",
        )
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

    try:
        complex_charge = require_int(params.get("charge", 0), field="charge")
        complex_multiplicity = require_int(
            params.get("multiplicity", 1), field="multiplicity", minimum=1
        )
        validate_interaction_energy_state_balance(
            cfg,
            complex_charge=complex_charge,
            complex_multiplicity=complex_multiplicity,
        )
    except ValueError:
        _record_interaction_energy_error(
            payload,
            reason="invalid_electronic_state",
            message=(
                "The complex and fragment electronic states required for interaction energy "
                "are invalid or incompatible."
            ),
        )
        logger.warning(
            "interaction_energy fan-out skipped: electronic states are invalid or incompatible",
            exc_info=True,
        )
        return False

    config_fingerprint = interaction_energy_config_fingerprint(
        cfg,
        complex_charge=complex_charge,
        complex_multiplicity=complex_multiplicity,
        rmsd_dedup=rmsd_cfg,
    )

    stages = workflow_stage_dicts(payload)
    declared_orca_stages = [stage for stage in stages if is_orca_stage_kind(stage)]
    if any(not is_exact_orca_stage_contract(stage) for stage in declared_orca_stages):
        return False
    orca_stages = declared_orca_stages
    complex_stages = [
        stage
        for stage in orca_stages
        if not is_valid_interaction_stage_contract(
            stage,
            stages,
            expected_config_fingerprint=config_fingerprint,
        )
    ]
    if not complex_stages:
        return False
    # Fire only once the primary ORCA set is terminal. Partial-success conformer
    # workflows use their completed subset; restart refuses to reopen failed
    # primary stages after interaction children exist unless the feature is
    # explicitly disabled and those children are retired.
    if not all(is_stage_terminal_status(_text(stage.get("status"))) for stage in complex_stages):
        return False

    parsed: list[_OptimizedEvidence] = []
    single_points: list[tuple[SiBlock, OrcaSelectedInputScienceIdentity]] = []
    for stage in complex_stages:
        if _text(stage.get("status")) != STATUS_COMPLETED:
            continue
        if _task_kind(stage) == "opt":
            opt_evidence = _completed_complex_evidence(
                stage,
                expected_charge=complex_charge,
                expected_multiplicity=complex_multiplicity,
            )
            if opt_evidence is None:
                continue
            block, selected_input_identity = opt_evidence
            parsed.append((_text(stage.get("stage_id")), stage, block, selected_input_identity))
            continue
        if _task_kind(stage) == "sp":
            single_point_evidence = _completed_single_point_evidence(stage)
            if single_point_evidence is not None:
                single_points.append(single_point_evidence)
    optimized_science_identities = {identity for _stage_id, _stage, _block, identity in parsed}
    if not parsed or len(optimized_science_identities) != 1:
        return False

    representative_ids = _rmsd_representative_ids(
        parsed,
        rmsd_cfg,
        effective_energies=_uniform_single_point_energies(parsed, single_points),
    )
    representatives = [item for item in parsed if item[0] in representative_ids]
    # ``role`` is reserved workflow metadata. A corrupt/spoofed interaction
    # role on an otherwise valid optimization must not hide that primary, but
    # generated fan-out children may only point to a non-interaction parent.
    for _stage_id, stage, _block, _identity in representatives:
        if is_interaction_role(_stage_role(stage)):
            _stage_metadata(stage).pop("role", None)
    fragment_index_lists = [fragment.get("atom_indices", []) for fragment in fragments]
    # The normalized durable block always carries a validated sp_route_line;
    # read it fail-closed instead of silently substituting a level of theory.
    sp_route_line = required_route_line(cfg, "sp_route_line")
    priority = safe_int(cfg.get("priority", params.get("priority", 10)), default=10)
    max_cores = safe_int(cfg.get("max_cores", params.get("max_cores", 8)), default=8)
    max_memory_gb = safe_int(cfg.get("max_memory_gb", params.get("max_memory_gb", 32)), default=32)

    existing = _existing_interaction_keys(
        stages,
        expected_config_fingerprint=config_fingerprint,
    )
    orca_paths = workflow_workspace_internal_engine_paths(workspace_dir, engine="orca")
    allowed_root = orca_paths["allowed_root"]
    source_root = ensure_confined_directory(
        allowed_root,
        allowed_root / _INTERACTION_SOURCE_DIRNAME,
        label="interaction-energy source directory",
    )
    created = 0

    for stage_id, _stage, block, _identity in representatives:
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

        if (INTERACTION_COMPLEX_SP_ROLE, stage_id, -1) not in existing:
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
                    "role": INTERACTION_COMPLEX_SP_ROLE,
                    "parent_stage_id": stage_id,
                    INTERACTION_CONFIG_FINGERPRINT_KEY: config_fingerprint,
                },
            )
            created += 1

        for index, fragment in enumerate(fragments):
            if (INTERACTION_FRAGMENT_ROLE, stage_id, index) in existing:
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
                    "role": INTERACTION_FRAGMENT_ROLE,
                    "parent_stage_id": stage_id,
                    "fragment_index": index,
                    "fragment_label": label,
                    "fragment_charge": fragment_charge,
                    "fragment_multiplicity": fragment_multiplicity,
                    "fragment_atom_indices": list(atom_indices),
                    INTERACTION_CONFIG_FINGERPRINT_KEY: config_fingerprint,
                },
            )
            created += 1

    return created > 0


__all__ = ["append_interaction_energy_stages_impl"]
