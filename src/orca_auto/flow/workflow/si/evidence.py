"""Durable workflow-stage evidence readers for Supporting Information assembly."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from orca_auto.orca.report.si import SiBlock, SiBlockError, collect_si_block
from orca_auto.orca.state import load_state

from ...conformer_selection import selected_input_state_matches
from ..report import _orca_stage_output_dir, _stage_metadata, _text


def _stage_label(stage: Mapping[str, Any]) -> str:
    return _text(_stage_metadata(stage).get("selected_input_label")) or _text(stage.get("stage_id"))


def _block_has_only_finite_numbers(block: SiBlock) -> bool:
    result = block.result
    optional_values = (
        result.energy_hartree,
        result.energy_ev,
        result.energy_kcalmol,
        result.lowest_freq_cm1,
        result.enthalpy,
        result.gibbs_energy,
        result.zpe_correction,
        result.gibbs_correction,
        result.thermo_temperature_k,
    )
    if any(value is not None and not math.isfinite(value) for value in optional_values):
        return False
    if any(not math.isfinite(value) for _, *coords in result.coordinates for value in coords):
        return False
    analysis = block.analysis
    if analysis is None:
        return True
    analysis_values = (
        *analysis.frequencies,
        *(value for columns in analysis.mode_matrix.values() for value in columns.values()),
        *(value for _, *coords in analysis.atoms for value in coords),
    )
    return all(math.isfinite(value) for value in analysis_values)


def _collect_stage_block(
    stage: Mapping[str, Any],
) -> tuple[SiBlock | None, str]:
    """(block, exclusion_reason) — exactly one side is meaningful."""
    reaction_dir = _orca_stage_output_dir(stage)
    if reaction_dir is None:
        return None, "no output directory recorded"
    state = load_state(reaction_dir)
    if state is None:
        return None, "no job state found"
    try:
        block = collect_si_block(reaction_dir, state)
    except SiBlockError as exc:
        return None, str(exc)
    if block is None:
        return None, "job type has no SI block"
    if not _block_has_only_finite_numbers(block):
        return None, "output contains a non-finite numeric result"
    result = block.result
    state_verified = result.electronic_state_verified and selected_input_state_matches(block, state)
    if not state_verified:
        warning = "route/electronic-state provenance missing or inconsistent with selected input"
        block = replace(
            block,
            result=replace(result, electronic_state_verified=False),
            warnings=(*block.warnings, warning),
        )
    return replace(block, name=_stage_label(stage)), ""


def _request_parameters(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    request = metadata.get("request")
    if not isinstance(request, dict):
        return {}
    parameters = request.get("parameters")
    return parameters if isinstance(parameters, dict) else {}
