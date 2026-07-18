"""Assemble durable workflow evidence into one scientifically interpreted SI dataset."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from orca_auto.orca.report.interaction_energy import InteractionEnergyResult
from orca_auto.orca.report.rmsd import RmsdGroup

from ...manifest import (
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    validate_conformer_postprocessing_template,
)
from ..report import (
    _crest_stage_detail,
    _stage_dicts,
    _stage_metadata,
    _task_kind,
    _text,
    _xtb_stage_detail,
)
from . import science as _science
from .evidence import _collect_stage_block, _request_parameters, _stage_label
from .models import ExcludedStage, WorkflowSiData, WorkflowSiEntry

logger = logging.getLogger(__name__)

_INTERACTION_ROLE_PREFIX = "interaction_"


def collect_workflow_si_data(
    payload: Mapping[str, Any],
    *,
    boltzmann_temperature_k: float | None = None,
    population_blocker: str = "",
    raise_feature_errors: bool = False,
) -> WorkflowSiData:
    template_name = _text(payload.get("template_name"))
    workflow_status = _text(payload.get("status"))
    parameters = _request_parameters(payload)
    try:
        interaction_cfg = normalize_interaction_energy_block(parameters.get("interaction_energy"))
    except ValueError:
        logger.warning("Invalid durable interaction_energy configuration", exc_info=True)
        interaction_cfg = None
    try:
        rmsd_cfg = normalize_rmsd_dedup_block(parameters.get("rmsd_dedup"))
    except ValueError:
        logger.warning("Invalid durable rmsd_dedup configuration", exc_info=True)
        rmsd_cfg = None
    try:
        validate_conformer_postprocessing_template(
            template_name,
            interaction_energy=interaction_cfg,
            rmsd_dedup=rmsd_cfg,
        )
    except ValueError:
        logger.warning("Conformer post-processing is disabled for this template", exc_info=True)
        interaction_cfg = None
        rmsd_cfg = None
    crest_total: int | None = None
    xtb_total: int | None = None
    stationary: list[WorkflowSiEntry] = []
    single_points: list[WorkflowSiEntry] = []
    extra: list[WorkflowSiEntry] = []
    excluded: list[ExcludedStage] = []
    incomplete_population_stages: list[str] = []
    # Interaction-energy fragment/complex single points are internal inputs, not
    # SI structures: they carry a ``role`` starting ``interaction_`` and must be
    # pulled out BEFORE any min/ts/sp classification so they can never leak into
    # the relative-energy table, the structures list, or si_data.csv, nor be
    # folded into a stationary structure by ``_science._pair_single_points``.
    interaction_raw_stages: list[Mapping[str, Any]] = []

    for stage in _stage_dicts(payload):
        stage_kind = _text(stage.get("stage_kind"))
        if stage_kind == "crest_stage":
            _, frames = _crest_stage_detail(stage)
            if frames is not None:
                crest_total = (crest_total or 0) + frames
            continue
        if stage_kind == "xtb_stage":
            _, candidates = _xtb_stage_detail(stage)
            xtb_total = (xtb_total or 0) + candidates
            continue
        if stage_kind != "orca_stage":
            continue
        if _text(_stage_metadata(stage).get("role")).startswith(_INTERACTION_ROLE_PREFIX):
            interaction_raw_stages.append(stage)
            continue

        stage_id = _text(stage.get("stage_id"))
        label = _stage_label(stage)
        status = _text(stage.get("status"))
        if _task_kind(stage) == "relaxed_scan":
            excluded.append(
                ExcludedStage(
                    stage_id, label, "relaxed scan (prerequisite, not a stationary point)"
                )
            )
            continue
        if status != "completed":
            excluded.append(ExcludedStage(stage_id, label, f"stage status: {status or 'unknown'}"))
            if template_name == "conformer_screening":
                incomplete_population_stages.append(label or stage_id or "unknown")
            continue
        block, reason = _collect_stage_block(stage)
        if block is None:
            excluded.append(ExcludedStage(stage_id, label, reason))
            if template_name == "conformer_screening":
                incomplete_population_stages.append(label or stage_id or "unknown")
            continue
        if (
            template_name == "conformer_screening"
            and block.kind != "min"
            and (_task_kind(stage) == "opt" or block.kind == "ts")
        ):
            incomplete_population_stages.append(label or stage_id or "unknown")
        entry = WorkflowSiEntry(stage_id=stage_id, block=block)
        if block.kind in ("min", "ts"):
            stationary.append(entry)
        elif block.analysis is None:
            single_points.append(entry)
        else:
            extra.append(entry)

    stationary.sort(
        key=lambda entry: (
            entry.block.result.energy_hartree is None,
            entry.block.result.energy_hartree or 0.0,
        )
    )
    # Give scientifically eligible minima first claim on optional SP refinements.
    # A known saddle/unconverged structure at the same geometry must not make an
    # otherwise unique minimum refinement ambiguous. Remaining stationary
    # structures may pair only with SPs left after that canonical pass.
    eligible_stationary = [entry for entry in stationary if _science._rmsd_eligible_minimum(entry)]
    eligible_paired, remaining_single_points = _science._pair_single_points(
        eligible_stationary, single_points
    )
    eligible_blocks = {id(entry.block) for entry in eligible_stationary}
    ineligible_paired, pre_dedup_unpaired = _science._pair_single_points(
        [entry for entry in stationary if id(entry.block) not in eligible_blocks],
        remaining_single_points,
    )
    # Stage IDs in corrupt/legacy payloads are not guaranteed unique. The SiBlock
    # identity is stable through dataclass replacement and keeps this merge 1:1.
    paired_by_block = {id(entry.block): entry for entry in (*eligible_paired, *ineligible_paired)}
    pre_dedup_ranked = [paired_by_block.get(id(entry.block), entry) for entry in stationary]

    # Validate population completeness against the full pre-dedup ensemble.
    # Dropping an unusable duplicate must never turn an incomplete ensemble into
    # a fabricated 100% population for the remaining representative.
    try:
        if not population_blocker and template_name == "conformer_screening":
            if workflow_status != "completed":
                population_blocker = (
                    "(populations omitted: the conformer ensemble is not terminal; "
                    f"workflow status is {workflow_status or 'unknown'})"
                )
            elif incomplete_population_stages:
                population_blocker = (
                    "(populations omitted: the conformer ensemble is incomplete; "
                    f"{len(incomplete_population_stages)} ORCA stage(s) are not usable)"
                )
        if not population_blocker and template_name == "conformer_screening":
            pre_rows, _pre_t, _pre_source, pre_note = _science._compute_populations(
                tuple(pre_dedup_ranked), boltzmann_temperature_k
            )
            pre_min_indices = [
                index for index, entry in enumerate(pre_dedup_ranked) if entry.block.kind == "min"
            ]
            if pre_min_indices and any(pre_rows[index] is None for index in pre_min_indices):
                population_blocker = pre_note or "(populations omitted: pre-dedup ensemble invalid)"
    except Exception:  # noqa: BLE001
        logger.warning("Pre-dedup population validation failed", exc_info=True)
        population_blocker = "(populations omitted: population validation failed)"

    # RMSD re-dedup and interaction-energy assembly are additive report-time
    # features isolated behind their own guards: a failure in either omits only
    # that feature and still renders the base SI (methods, table, structures).
    rmsd_groups: tuple[RmsdGroup, ...] = ()
    ranked = pre_dedup_ranked
    if rmsd_cfg is not None:
        try:
            ranked, rmsd_groups = _science._dedup_minima(pre_dedup_ranked, rmsd_cfg)
        except Exception:  # noqa: BLE001
            if raise_feature_errors:
                raise
            logger.warning("Workflow SI RMSD dedup failed", exc_info=True)
            rmsd_groups = ()
            ranked = pre_dedup_ranked
    unpaired = pre_dedup_unpaired

    interaction_energies: tuple[InteractionEnergyResult, ...] = ()
    if interaction_cfg is not None:
        try:
            interaction_energies = _science._interaction_energy_results(
                interaction_raw_stages,
                stationary,
                single_points,
                interaction_cfg,
                parameters,
                rmsd_cfg,
            )
        except Exception:  # noqa: BLE001
            if raise_feature_errors:
                raise
            logger.warning("Workflow SI interaction-energy assembly failed", exc_info=True)
            interaction_energies = ()

    # A population bug must never replace a valid SI with stale files: isolate the
    # computation so the base document (methods, relative-energy table, structures)
    # still renders even if this raises.
    try:
        populations, temperature, temperature_source, population_note = (
            _science._compute_populations(
                tuple(ranked), boltzmann_temperature_k, blocker=population_blocker
            )
        )
    except Exception:  # noqa: BLE001
        logger.warning("Boltzmann population computation failed", exc_info=True)
        populations, temperature, temperature_source, population_note = (
            tuple(None for _ in ranked),
            None,
            "",
            "(populations omitted: population computation failed; inspect the application log)",
        )

    return WorkflowSiData(
        workflow_id=_text(payload.get("workflow_id")),
        template_name=template_name,
        status=workflow_status,
        reaction_key=_text(payload.get("reaction_key")),
        crest_conformer_total=crest_total,
        xtb_candidate_total=xtb_total,
        entries=tuple(ranked),
        extra_blocks=tuple(unpaired + extra),
        excluded=tuple(excluded),
        boltzmann_temperature_k=temperature,
        boltzmann_temperature_source=temperature_source,
        population_note=population_note,
        populations=populations,
        interaction_energies=interaction_energies,
        interaction_energy_enabled=interaction_cfg is not None,
        rmsd_dedup_enabled=rmsd_cfg is not None,
        rmsd_groups=rmsd_groups,
    )
