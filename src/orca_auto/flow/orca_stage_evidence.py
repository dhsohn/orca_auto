"""Authoritative, provenance-bound evidence reader for completed ORCA stages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_JSON_FILE
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.engine_runner import executable_identity, verify_confined_output_identity
from orca_auto.core.queue.engine.input_snapshot import require_direct_generation_owner
from orca_auto.core.queue.generation import (
    is_visible_generation_name,
    visible_generation_children,
)
from orca_auto.core.utils import mapping_or_empty as _mapping
from orca_auto.flow.conformer_selection import (
    OrcaSelectedInputScienceIdentity,
    bound_orca_selected_input_science_identity,
    selected_input_state_matches,
)
from orca_auto.flow.contracts.workflow import is_supported_orca_stage_contract
from orca_auto.flow.orca_stage_validation import validate_workflow_orca_input
from orca_auto.orca.report.si import SiBlock, SiBlockError, collect_si_block
from orca_auto.orca.state_reading import (
    load_generation_state,
    load_report_json_with_output_receipt,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def stage_metadata(stage: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(stage.get("metadata"))


def stage_task(stage: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(stage.get("task"))


def _stage_task_payload(stage: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(stage_task(stage).get("payload"))


def _stage_artifact_path(stage: Mapping[str, Any], kind: str) -> Path | None:
    artifacts = stage.get("output_artifacts")
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or _text(artifact.get("kind")) != kind:
            continue
        if path_text := _text(artifact.get("path")):
            return Path(path_text)
    return None


def stage_job_dirs(stage: Mapping[str, Any]) -> tuple[Path, ...]:
    metadata = stage_metadata(stage)
    task_payload = _stage_task_payload(stage)
    paths: list[Path] = []
    seen: set[str] = set()
    for value in (
        metadata.get("latest_known_path"),
        task_payload.get("job_dir"),
        task_payload.get("reaction_dir"),
    ):
        path_text = _text(value)
        if path_text and path_text not in seen:
            paths.append(Path(path_text))
            seen.add(path_text)
    return tuple(paths)


def _identity_values(*values: Any) -> frozenset[str]:
    return frozenset(text for value in values if (text := _text(value)))


def stage_report_identity_matches(
    stage: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    require_job_and_run: bool = True,
) -> bool:
    """Whether one durable stage unambiguously names the report job/run/queue."""

    metadata = stage_metadata(stage)
    task = stage_task(stage)
    task_payload = _stage_task_payload(stage)
    submission = _mapping(task.get("submission_result"))
    job = _mapping(report.get("job"))
    engine_payload = _mapping(report.get("engine_payload"))
    stage_job_ids = _identity_values(metadata.get("child_job_id"), submission.get("job_id"))
    report_job_ids = _identity_values(job.get("id"), job.get("task_id"))
    stage_run_ids = _identity_values(metadata.get("run_id"), task_payload.get("run_id"))
    report_run_ids = _identity_values(engine_payload.get("run_id"))
    stage_queue_ids = _identity_values(metadata.get("queue_id"), submission.get("queue_id"))
    report_queue_ids = _identity_values(job.get("queue_id"))
    identity_pairs = (
        (stage_job_ids, report_job_ids),
        (stage_run_ids, report_run_ids),
        (stage_queue_ids, report_queue_ids),
    )
    if any(
        len(stage_values) > 1 or len(report_values) > 1
        for stage_values, report_values in identity_pairs
    ):
        return False
    if require_job_and_run:
        if any(
            not stage_values or report_values != stage_values
            for stage_values, report_values in identity_pairs[:2]
        ):
            return False
        return not (stage_queue_ids and report_queue_ids and report_queue_ids != stage_queue_ids)
    declared_pairs = tuple(
        (stage_values, report_values)
        for stage_values, report_values in identity_pairs
        if stage_values
    )
    return bool(declared_pairs) and all(
        report_values == stage_values for stage_values, report_values in declared_pairs
    )


def verified_orca_stage_report(
    stage: Mapping[str, Any],
    report_path: Path | None,
) -> dict[str, Any] | None:
    """Load one canonical successful ORCA report bound to the durable stage."""

    loaded = _verified_stage_report_with_output_receipt(stage, report_path)
    return None if loaded is None else loaded[0]


def _verified_stage_report_with_output_receipt(
    stage: Mapping[str, Any],
    report_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """The stage's verified report and the ``orca-output`` receipt that load accepted."""

    if (
        report_path is None
        or not report_path.is_absolute()
        or report_path.name != RUN_REPORT_JSON_FILE
        or report_path != report_path.parent / RUN_REPORT_JSON_FILE
        or not is_visible_generation_name(report_path.parent.name)
        or report_path.is_symlink()
        or not report_path.is_file()
    ):
        return None
    try:
        if report_path.resolve(strict=True) != report_path:
            return None
    except OSError:
        return None
    loaded = load_report_json_with_output_receipt(
        report_path.parent,
        require_consumable_success=True,
    )
    if loaded is None or not stage_report_identity_matches(stage, loaded[0]):
        return None
    return loaded


def resolve_verified_orca_stage_report(
    stage: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the canonical verified report for one ORCA workflow stage.

    The third element is the ``orca-output`` receipt the same load accepted, so
    a caller that reads that output afterwards can bind what it reads to the
    observed bytes rather than to a path it re-opens.
    """

    report_path = _stage_artifact_path(stage, "orca_report_json")
    loaded = _verified_stage_report_with_output_receipt(stage, report_path)
    if loaded is not None:
        return report_path, loaded[0], loaded[1]
    for job_dir in stage_job_dirs(stage):
        candidate_dirs = (
            (job_dir,)
            if is_visible_generation_name(job_dir.name)
            else visible_generation_children(job_dir)
        )
        for candidate_dir in candidate_dirs:
            candidate_path = candidate_dir / RUN_REPORT_JSON_FILE
            loaded = _verified_stage_report_with_output_receipt(stage, candidate_path)
            if loaded is not None:
                return candidate_path, loaded[0], loaded[1]
    return None, None, None


def _report_state_identity_matches(
    report: Mapping[str, Any],
    state_payload: Mapping[str, Any],
) -> bool:
    report_job = _identity_values(
        _mapping(report.get("job")).get("id"),
        _mapping(report.get("job")).get("task_id"),
    )
    state_job = _identity_values(
        _mapping(state_payload.get("job")).get("id"),
        _mapping(state_payload.get("job")).get("task_id"),
    )
    report_run = _identity_values(_mapping(report.get("engine_payload")).get("run_id"))
    state_run = _identity_values(_mapping(state_payload.get("engine_payload")).get("run_id"))
    report_queue = _identity_values(_mapping(report.get("job")).get("queue_id"))
    state_queue = _identity_values(_mapping(state_payload.get("job")).get("queue_id"))
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
    return next(iter(values)) if len(values) == 1 else None


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
    final_result = _mapping(_mapping(payload.get("engine_payload")).get("final_result"))
    return _text(final_result.get("last_out_path"))


def _terminal_output_identity(
    payload: Mapping[str, Any],
    output_path: str,
) -> dict[str, Any] | None:
    attempts = _mapping(payload.get("engine_payload")).get("attempts")
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
        label="workflow ORCA selected input",
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


def _selected_input_science_identity(
    generation_dir: Path,
    report: Mapping[str, Any],
) -> OrcaSelectedInputScienceIdentity | None:
    selected_text = _selected_input(report)
    provenance = _execution_provenance(report)
    if selected_text is None or provenance is None:
        return None
    bound_identity = provenance.get("bound_selected_identity")
    if not isinstance(bound_identity, Mapping):
        return None
    materialized_identities = provenance.get("materialized_inputs")
    if not isinstance(materialized_identities, Mapping):
        materialized_identities = {}
    return bound_orca_selected_input_science_identity(
        generation_dir,
        Path(selected_text),
        bound_selected_identity=bound_identity,
        materialized_input_identities=materialized_identities,
    )


def _selected_input_role_matches(
    generation_dir: Path,
    report: Mapping[str, Any],
    stage: Mapping[str, Any],
) -> bool:
    selected_text = _selected_input(report)
    if selected_text is None:
        return False
    selected_path = _direct_regular_file(
        generation_dir,
        selected_text,
        label="workflow ORCA selected input role",
    )
    try:
        validate_workflow_orca_input(
            task_kind=_text(stage_task(stage).get("task_kind")),
            inp_path=selected_path,
        )
    except ValueError:
        return False
    return True


def collect_verified_orca_stage_evidence(
    stage: Mapping[str, Any],
) -> tuple[SiBlock | None, str, OrcaSelectedInputScienceIdentity | None]:
    """Parse one stage only through its verified report generation and bindings."""

    if not is_supported_orca_stage_contract(stage):
        return None, "unsupported or contradictory ORCA stage contract", None
    stage_status = _text(stage.get("status")).lower()
    task_status = _text(stage_task(stage).get("status")).lower()
    if stage_status != "completed" or task_status not in {"", "completed"}:
        return None, "stage/task is not durably completed", None
    report_path, report, _output_receipt = resolve_verified_orca_stage_report(stage)
    if report is None or report_path is None:
        return None, "no verified report generation recorded", None
    generation_dir = report_path.parent
    loaded_state = load_generation_state(generation_dir)
    if loaded_state is None:
        return None, "no job state found", None
    state_payload, state = loaded_state
    if issue := _state_evidence_issue(generation_dir, report, state_payload):
        return None, issue, None
    if not _selected_input_role_matches(generation_dir, report, stage):
        return None, "selected input route does not match durable ORCA task kind", None
    selected_input_identity = _selected_input_science_identity(generation_dir, report)
    try:
        block = collect_si_block(generation_dir, state)
    except (OSError, SiBlockError):
        return None, "job evidence could not be parsed into a complete SI block", None
    if issue := _state_evidence_issue(generation_dir, report, state_payload):
        return None, issue, None
    report_after = verified_orca_stage_report(stage, report_path)
    loaded_after = load_generation_state(generation_dir)
    if report_after != report or loaded_after is None or loaded_after[0] != state_payload:
        return None, "job evidence changed while it was parsed", None
    if issue := _state_evidence_issue(generation_dir, report_after, loaded_after[0]):
        return None, issue, None
    if _selected_input_science_identity(generation_dir, report_after) != selected_input_identity:
        return None, "selected input science evidence changed while it was parsed", None
    if block is None:
        return None, "job type has no SI block", None
    result = block.result
    state_verified = result.electronic_state_verified and selected_input_state_matches(block, state)
    if not state_verified:
        warning = "route/electronic-state provenance missing or inconsistent with selected input"
        block = replace(
            block,
            result=replace(result, electronic_state_verified=False),
            warnings=(*block.warnings, warning),
        )
    return block, "", selected_input_identity


__all__ = [
    "collect_verified_orca_stage_evidence",
    "resolve_verified_orca_stage_report",
    "stage_job_dirs",
    "stage_metadata",
    "stage_report_identity_matches",
    "stage_task",
    "verified_orca_stage_report",
]
