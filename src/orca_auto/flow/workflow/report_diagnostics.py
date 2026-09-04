"""Read verified stage reports and assemble workflow failure diagnostics."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE, RUN_REPORT_JSON_FILE
from orca_auto.core.statuses import FAILED_STATUSES, STATUS_CANCELLED
from orca_auto.core.utils import mapping_or_empty as _mapping
from orca_auto.flow.orca_stage_evidence import (
    resolve_verified_orca_stage_report,
    stage_job_dirs,
    stage_metadata,
    stage_report_identity_matches,
    stage_task,
)

_DIAGNOSTIC_STAGE_STATUSES = frozenset({*FAILED_STATUSES, STATUS_CANCELLED})
_INTERNAL_STAGE_ENGINES = {
    "crest_stage": "crest",
    "xtb_stage": "xtb",
}
_LOG_TAIL_LIMIT_BYTES = 256 * 1024


def normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _stage_has_diagnostic_status(stage: Mapping[str, Any]) -> bool:
    return (
        normalized_text(stage.get("status")).lower() in _DIAGNOSTIC_STAGE_STATUSES
        or normalized_text(stage_task(stage).get("status")).lower() in _DIAGNOSTIC_STAGE_STATUSES
    )


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def resolve_stage_job_report(
    stage: Mapping[str, Any],
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve the canonical verified report for one workflow stage.

    The third element is the ``orca-output`` receipt the ORCA load accepted for
    this report, and ``None`` for an internal engine: CREST and xTB states
    carry no artifact receipts, so nothing they name can be bound to observed
    bytes.
    """
    task_engine = normalized_text(stage_task(stage).get("engine")).lower()
    internal_engine = _INTERNAL_STAGE_ENGINES.get(normalized_text(stage.get("stage_kind")).lower())
    if internal_engine is not None:
        if task_engine != internal_engine:
            return None, None, None
        for job_dir in stage_job_dirs(stage):
            state_path = job_dir / "job_state.json"
            state = _load_json(state_path)
            if (
                state is not None
                and normalized_text(state.get("engine")).lower() == internal_engine
                and stage_report_identity_matches(
                    stage,
                    state,
                    require_job_and_run=False,
                )
            ):
                return state_path, state, None
        return None, None, None
    if task_engine in _INTERNAL_STAGE_ENGINES.values():
        return None, None, None
    return resolve_verified_orca_stage_report(stage)


def _stage_status_reason(stage: Mapping[str, Any], report: Mapping[str, Any] | None) -> str:
    metadata = stage_metadata(stage)
    task = stage_task(stage)
    report_status = _mapping(report.get("status")) if report is not None else {}
    for value in (
        metadata.get("reaction_handoff_reason"),
        metadata.get("reason"),
        report_status.get("reason"),
        _mapping(task.get("cancel_result")).get("reason"),
        _mapping(task.get("submission_result")).get("reason"),
        metadata.get("submission_deferred_reason"),
    ):
        reason = normalized_text(value)
        if reason:
            return reason
    return ""


def _read_log_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL_LIMIT_BYTES))
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _crest_topology_change_explanation(stdout_text: str) -> str:
    lowered = stdout_text.lower()
    if (
        "change in topology detected" not in lowered
        and "a topology change was seen in the initial geometry optimization" not in lowered
    ):
        return ""
    affected_atoms = ""
    lines = stdout_text.splitlines()
    for index, line in enumerate(lines):
        if "topology change compared to the input affects atoms:" not in line.lower():
            continue
        for candidate in lines[index + 1 :]:
            candidate = candidate.strip()
            if candidate:
                affected_atoms = candidate
                break
        break
    explanation = (
        "CREST stopped because the initial geometry optimization changed molecular topology."
    )
    if affected_atoms:
        explanation += f" Affected atoms: {affected_atoms}."
    return (
        f"{explanation} Check the input geometry; if the change is intentional, "
        "set crest.noreftopo: true before restarting, noting that this can retain artifacts."
    )


def _relative_href(path: Path, workspace_dir: Path) -> str:
    try:
        return os.path.relpath(path, workspace_dir)
    except ValueError:
        return str(path)


def _direct_single_link_file(path: Path, generation_dir: Path) -> Path | None:
    try:
        details = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if (
        path.parent != generation_dir
        or resolved != path
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
    ):
        return None
    return path


def report_html_href(report_json_path: Path, workspace_dir: Path) -> str | None:
    """Return the safe workspace-relative HTML report link for one stage report."""
    job_report_html = report_json_path.parent / RUN_REPORT_HTML_FILE
    if _direct_single_link_file(job_report_html, report_json_path.parent) is None:
        return None
    return _relative_href(job_report_html, workspace_dir)


def collect_stage_diagnostic(
    stage: Mapping[str, Any], workspace_dir: Path
) -> tuple[str, str, str | None]:
    """Collect failure reason, explanation, and details link for one stage."""
    include_job_artifacts = _stage_has_diagnostic_status(stage)
    if not include_job_artifacts:
        report_path, report = None, None
    else:
        report_path, report, _output_receipt = resolve_stage_job_report(stage)
    reason = _stage_status_reason(stage, report)
    if reason == "completed":
        reason = ""
    explanation = normalized_text(stage_metadata(stage).get("reaction_handoff_message"))
    if not include_job_artifacts:
        return reason, explanation, None
    job_dir = report_path.parent if report_path is not None else None
    details_path: Path | None = None
    engine = normalized_text(stage_task(stage).get("engine")).lower()
    if engine == "crest" and job_dir is not None:
        stdout_path = job_dir / "crest.stdout.log"
        explanation = _crest_topology_change_explanation(_read_log_tail(stdout_path))
        if explanation:
            details_path = stdout_path
    if engine == "orca" and job_dir is not None and report is not None:
        details_path = _direct_single_link_file(job_dir / RUN_REPORT_HTML_FILE, job_dir)
        if details_path is None:
            details_path = report_path
    if details_path is None and job_dir is not None:
        artifact_names = (
            ("job_state.json",) if engine in {"xtb", "crest"} else (RUN_REPORT_JSON_FILE,)
        )
        for name in artifact_names:
            candidate = job_dir / name
            if candidate.exists():
                details_path = candidate
                break
    details_href = _relative_href(details_path, workspace_dir) if details_path is not None else None
    return reason, explanation, details_href


__all__ = [
    "collect_stage_diagnostic",
    "normalized_text",
    "report_html_href",
    "resolve_stage_job_report",
]
