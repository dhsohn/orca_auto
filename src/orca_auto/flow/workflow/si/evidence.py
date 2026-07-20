"""Durable workflow-stage evidence readers for Supporting Information assembly."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.engine_runner import executable_identity, verify_confined_output_identity
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.orca.report.si import SiBlock, SiBlockError, collect_si_block
from orca_auto.orca.state import load_generation_state

from ...conformer_selection import selected_input_state_matches
from ..report import (
    _resolve_orca_stage_report,
    _stage_metadata,
    _text,
)


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identity_values(payload: Mapping[str, Any], dimension: str) -> frozenset[str]:
    job = _mapping(payload.get("job"))
    engine_payload = _mapping(payload.get("engine_payload"))
    values: tuple[Any, ...]
    if dimension == "job":
        values = (
            job.get("id"),
            job.get("task_id"),
            payload.get("job_id"),
            engine_payload.get("job_id"),
        )
    elif dimension == "run":
        values = (payload.get("run_id"), engine_payload.get("run_id"))
    else:
        values = (
            job.get("queue_id"),
            payload.get("queue_id"),
            engine_payload.get("queue_id"),
        )
    return frozenset(text for value in values if (text := _text(value)))


def _report_state_identity_matches(
    report: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> bool:
    report_job = _identity_values(report, "job")
    state_job = _identity_values(state_payload, "job")
    report_run = _identity_values(report, "run")
    state_run = _identity_values(state_payload, "run")
    report_queue = _identity_values(report, "queue")
    state_queue = _identity_values(state_payload, "queue")
    dimensions = (
        (report_job, state_job),
        (report_run, state_run),
        (report_queue, state_queue),
    )
    return (
        bool(report_job)
        and bool(report_run)
        and all(len(left) <= 1 and len(right) <= 1 for left, right in dimensions)
        and all(left == right for left, right in dimensions)
    )


def _selected_input(payload: Mapping[str, Any]) -> str | None:
    input_payload = _mapping(payload.get("input"))
    values = frozenset(
        text
        for value in (payload.get("selected_inp"), input_payload.get("primary_path"))
        if (text := _text(value))
    )
    if len(values) != 1:
        return None
    return next(iter(values))


def _execution_provenance(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    if isinstance(payload.get("execution_provenance"), Mapping):
        candidates.append(dict(payload["execution_provenance"]))
    engine_payload = _mapping(payload.get("engine_payload"))
    if isinstance(engine_payload.get("execution_provenance"), Mapping):
        candidates.append(dict(engine_payload["execution_provenance"]))
    if not candidates or any(candidate != candidates[0] for candidate in candidates[1:]):
        return None
    return candidates[0]


def _generation_matches_provenance(
    generation_dir: Path,
    provenance: Mapping[str, Any],
) -> bool:
    execution_dir = Path(_text(provenance.get("execution_dir")))
    identity = _mapping(provenance.get("execution_dir_identity"))
    try:
        status = generation_dir.stat()
        job_status = generation_dir.parent.stat()
        expected_identity = (int(identity.get("device", -1)), int(identity.get("inode", -1)))
    except (OSError, TypeError, ValueError):
        return False
    if not (
        execution_dir.is_absolute()
        and execution_dir == generation_dir
        and not generation_dir.is_symlink()
        and expected_identity == (int(status.st_dev), int(status.st_ino))
    ):
        return False
    try:
        require_direct_generation_owner(
            generation_dir.parent,
            namespace=generation_dir.name,
            expected_job_identity=(int(job_status.st_dev), int(job_status.st_ino)),
            expected_generation_identity=expected_identity,
            owner_token=_text(provenance.get("generation_owner_token")),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


def _direct_regular_file(generation_dir: Path, raw_path: str, *, label: str) -> Path | None:
    path = Path(raw_path)
    if not path.is_absolute() or path.parent != generation_dir or path.is_symlink():
        return None
    try:
        resolved = require_confined_regular_file(generation_dir, path, label=label)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved if path == resolved else None


def _terminal_output_path(payload: Mapping[str, Any]) -> str:
    engine_payload = _mapping(payload.get("engine_payload"))
    final_result = _mapping(engine_payload.get("final_result"))
    return _text(final_result.get("last_out_path"))


def _terminal_output_identity(
    payload: Mapping[str, Any],
    output_path: str,
) -> dict[str, Any] | None:
    engine_payload = _mapping(payload.get("engine_payload"))
    attempts = engine_payload.get("attempts")
    if not isinstance(attempts, list):
        return None
    matches = [
        attempt
        for attempt in attempts
        if isinstance(attempt, Mapping) and _text(attempt.get("out_path")) == output_path
    ]
    if len(matches) != 1:
        return None
    identity = matches[0].get("output_identity")
    return dict(identity) if isinstance(identity, Mapping) else None


def _terminal_output_issue(
    generation_dir: Path,
    report: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> str:
    report_path = _terminal_output_path(report)
    state_path = _terminal_output_path(state_payload)
    if not report_path or state_path != report_path:
        return "job state terminal output differs from verified report"
    report_identity = _terminal_output_identity(report, report_path)
    state_identity = _terminal_output_identity(state_payload, state_path)
    if (
        report_identity is None
        or state_identity != report_identity
        or _text(report_identity.get("path")) != report_path
    ):
        return "job output content identity differs from verified report"
    try:
        verified = verify_confined_output_identity(generation_dir, report_identity)
    except (OSError, RuntimeError, TypeError, ValueError):
        return "job output no longer matches its terminal content identity"
    if verified != Path(report_path) or verified.parent != generation_dir:
        return "job output is not a regular file confined to the report generation"
    return ""


def _state_evidence_issue(
    generation_dir: Path,
    report: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> str:
    if not _report_state_identity_matches(report, state_payload):
        return "job state identity differs from verified report"
    report_selected = _selected_input(report)
    state_selected = _selected_input(state_payload)
    if report_selected is None or state_selected != report_selected:
        return "job state selected input differs from verified report"
    report_provenance = _execution_provenance(report)
    state_provenance = _execution_provenance(state_payload)
    if report_provenance is None or state_provenance != report_provenance:
        return "job state execution provenance differs from verified report"
    if not _generation_matches_provenance(generation_dir, report_provenance):
        return "verified report provenance does not identify its generation"
    selected_path = _direct_regular_file(
        generation_dir,
        report_selected,
        label="workflow SI selected input",
    )
    bound_identity = _mapping(report_provenance.get("bound_selected_identity"))
    try:
        selected_identity_matches = selected_path is not None and executable_identity(
            selected_path
        ) == dict(bound_identity)
    except (OSError, RuntimeError, TypeError, ValueError):
        selected_identity_matches = False
    if not selected_identity_matches:
        return "selected input is not a provenance-bound generation file"
    return _terminal_output_issue(generation_dir, report, state_payload)


def _collect_stage_block(
    stage: Mapping[str, Any],
) -> tuple[SiBlock | None, str]:
    """(block, exclusion_reason) — exactly one side is meaningful."""
    report_path, report = _resolve_orca_stage_report(stage)
    if report is None or report_path is None:
        return None, "no verified report generation recorded"
    generation_dir = report_path.parent
    loaded_state = load_generation_state(generation_dir)
    if loaded_state is None:
        return None, "no job state found"
    state_payload, state = loaded_state
    if issue := _state_evidence_issue(generation_dir, report, state_payload):
        return None, issue
    try:
        block = collect_si_block(generation_dir, state)
    except (OSError, SiBlockError):
        return None, "job evidence could not be parsed into a complete SI block"
    # Recheck the complete input/output/provenance binding after parsers have
    # reopened the files so a normal overwrite or rename cannot be published.
    if issue := _state_evidence_issue(generation_dir, report, state_payload):
        return None, issue
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
