"""Publication of the canonical workflow SI document."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import WORKFLOW_SI_MD_FILE
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.flow.contracts.workflow import workflow_request_parameters

from ...manifest import (
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    optional_positive_float,
    require_int,
    validate_conformer_postprocessing_template,
    validate_interaction_energy_state_balance,
)
from .collection import collect_workflow_si_data
from .rendering import render_workflow_si_md

logger = logging.getLogger(__name__)


def _boltzmann_temperature_override(
    payload: Mapping[str, Any],
) -> tuple[float | None, str]:
    """Read the admission-validated override from durable workflow state only."""
    parameters = workflow_request_parameters(payload)
    if "boltzmann_temperature_k" not in parameters:
        return None, ""
    try:
        return optional_positive_float(parameters, "boltzmann_temperature_k"), ""
    except ValueError:
        logger.warning("Invalid durable boltzmann_temperature_k", exc_info=True)
        return None, "(populations omitted: durable boltzmann_temperature_k is invalid)"


def _remove_si_artifacts(*paths: Path) -> None:
    first_error: OSError | None = None
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            first_error = first_error or exc
            logger.warning("Failed to remove inconsistent SI artifact %s", path, exc_info=True)
    if first_error is not None:
        raise first_error


def write_workflow_si(
    workspace_dir: Path,
    payload: Mapping[str, Any],
) -> Path | None:
    """Write ``workflow_si.md``.

    A workflow without ORCA stages has no SI: a stale file from an earlier
    template is removed so nothing obsolete can be pasted into a paper.
    Errors are logged and re-raised for the durable publication retry state
    machine; ``None`` means the workflow has no SI, never that writing it
    failed.
    """
    md_path = workspace_dir / WORKFLOW_SI_MD_FILE
    try:
        # Durable corruption is not equivalent to an explicit feature disable.
        # Validate before touching the last known-good publication.
        parameters = workflow_request_parameters(payload)
        normalized_interaction = normalize_interaction_energy_block(
            parameters.get("interaction_energy")
        )
        # Evaluate the strict charge/multiplicity reads only when the feature is
        # configured: corrupt request parameters must fail the interaction
        # feature closed, not block the whole SI of an unrelated workflow.
        if normalized_interaction is not None:
            validate_interaction_energy_state_balance(
                normalized_interaction,
                complex_charge=require_int(parameters.get("charge", 0), field="charge"),
                complex_multiplicity=require_int(
                    parameters.get("multiplicity", 1), field="multiplicity", minimum=1
                ),
            )
        normalized_rmsd = normalize_rmsd_dedup_block(parameters.get("rmsd_dedup"))
        validate_conformer_postprocessing_template(
            payload.get("template_name"),
            interaction_energy=normalized_interaction,
            rmsd_dedup=normalized_rmsd,
        )
        override, population_blocker = _boltzmann_temperature_override(payload)
        data = collect_workflow_si_data(
            payload,
            boltzmann_temperature_k=override,
            population_blocker=population_blocker,
        )
        if not data.has_orca_stages():
            _remove_si_artifacts(md_path)
            return None
        atomic_write_text(md_path, render_workflow_si_md(data))
        return md_path
    except Exception:
        # advance.py persists only a truncated one-line summary, so this is the
        # only place the traceback is recorded.
        logger.warning("Workflow SI generation failed for %s", workspace_dir, exc_info=True)
        raise
