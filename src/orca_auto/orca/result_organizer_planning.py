from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orca_auto.core.paths import is_subpath, resolve_artifact_path

from .completion_rules import TS_ROUTE_RE
from .molecule_key import resolve_molecule_key
from .result_organizer_models import OrganizePlan, SkipReason
from .state import load_state
from .statuses import RunStatus
from .types import RunState

logger = logging.getLogger(__name__)

OPT_RE = re.compile(r"\bOpt\b", re.IGNORECASE)
SP_RE = re.compile(r"\b(SP|Energy)\b", re.IGNORECASE)
FREQ_RE = re.compile(r"\b(Freq|NumFreq|AnFreq)\b", re.IGNORECASE)


def _resolve_existing_artifact(path_text: str, reaction_dir: Path) -> Path | None:
    return resolve_artifact_path(path_text, reaction_dir)


def check_eligibility(reaction_dir: Path) -> tuple[RunState | None, SkipReason | None]:
    state = load_state(reaction_dir)
    if state is None:
        return None, SkipReason(str(reaction_dir), "state_missing_or_invalid")

    run_id = state.get("run_id")
    status = state.get("status")
    if not isinstance(run_id, str) or not run_id.strip():
        return None, SkipReason(str(reaction_dir), "state_schema_invalid")
    if not isinstance(status, str) or not status.strip():
        return None, SkipReason(str(reaction_dir), "state_schema_invalid")

    if status != RunStatus.COMPLETED.value:
        logger.debug("Skipping non-completed: %s (status=%s)", reaction_dir, status)
        return None, SkipReason(str(reaction_dir), "not_completed")

    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return None, SkipReason(str(reaction_dir), "final_result_missing")

    selected_inp = state.get("selected_inp")
    if isinstance(selected_inp, str) and selected_inp.strip():
        inp_path = _resolve_existing_artifact(selected_inp, reaction_dir)
        if inp_path is None:
            logger.warning("Artifact missing: %s", selected_inp)
            return None, SkipReason(str(reaction_dir), "artifact_missing")
        state["selected_inp"] = str(inp_path)

    last_out = final_result.get("last_out_path")
    if isinstance(last_out, str) and last_out.strip():
        out_path = _resolve_existing_artifact(last_out, reaction_dir)
        if out_path is None:
            logger.warning("Artifact missing: %s", last_out)
            return None, SkipReason(str(reaction_dir), "state_output_mismatch")
        final_result["last_out_path"] = str(out_path)

    return state, None


def detect_job_type(inp_path: Path) -> str:
    route_line = _read_route_line(inp_path)
    if TS_ROUTE_RE.search(route_line):
        return "ts"
    if OPT_RE.search(route_line):
        return "opt"
    if SP_RE.search(route_line):
        return "sp"
    if FREQ_RE.search(route_line):
        return "freq"
    return "other"


def _read_route_line(inp_path: Path) -> str:
    try:
        with inp_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("!"):
                    return stripped
    except OSError:
        pass
    return ""


def _attempt_is_successful(attempt: Mapping[str, Any]) -> bool:
    analyzer_status = attempt.get("analyzer_status")
    if isinstance(analyzer_status, str) and analyzer_status == "completed":
        return True

    return_code = attempt.get("return_code")
    return return_code == 0


def _last_successful_attempt_inp_path(state: Mapping[str, Any], reaction_dir: Path) -> Path | None:
    attempts = state.get("attempts")
    if not isinstance(attempts, list):
        return None

    final_out_path = _final_out_path(state, reaction_dir)

    for attempt in reversed(attempts):
        if not isinstance(attempt, dict):
            continue

        inp_path = _attempt_inp_path(attempt, reaction_dir)
        if inp_path is None:
            continue

        if _attempt_matches_final_out(
            attempt, final_out_path, reaction_dir
        ) or _attempt_is_successful(attempt):
            return inp_path

    return None


def _final_out_path(state: Mapping[str, Any], reaction_dir: Path) -> Path | None:
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return None
    last_out_path = final_result.get("last_out_path")
    if not isinstance(last_out_path, str) or not last_out_path.strip():
        return None
    return _resolve_existing_artifact(last_out_path, reaction_dir)


def _attempt_inp_path(attempt: Mapping[str, Any], reaction_dir: Path) -> Path | None:
    inp_path_text = attempt.get("inp_path")
    if not isinstance(inp_path_text, str) or not inp_path_text.strip():
        return None
    return _resolve_existing_artifact(inp_path_text, reaction_dir)


def _attempt_matches_final_out(
    attempt: Mapping[str, Any],
    final_out_path: Path | None,
    reaction_dir: Path,
) -> bool:
    if final_out_path is None:
        return False
    out_path_text = attempt.get("out_path")
    if not isinstance(out_path_text, str) or not out_path_text.strip():
        return False
    out_path = _resolve_existing_artifact(out_path_text, reaction_dir)
    return out_path is not None and out_path == final_out_path


def select_organize_metadata_inp_path(state: Mapping[str, Any], reaction_dir: Path) -> Path | None:
    selected_inp_value = state.get("selected_inp")
    selected_inp_path = None
    selected_resolution = None

    if isinstance(selected_inp_value, str) and selected_inp_value.strip():
        selected_inp_path = _resolve_existing_artifact(selected_inp_value, reaction_dir)
        if selected_inp_path is not None and selected_inp_path.exists():
            selected_resolution = resolve_molecule_key(selected_inp_path)
            if selected_resolution.source != "directory_fallback":
                return selected_inp_path

    attempt_inp_path = _last_successful_attempt_inp_path(state, reaction_dir)
    if attempt_inp_path is not None and attempt_inp_path.exists():
        attempt_resolution = resolve_molecule_key(attempt_inp_path)
        if (
            selected_resolution is not None
            and selected_resolution.source == "directory_fallback"
            and attempt_resolution.source != "directory_fallback"
        ):
            return attempt_inp_path
        if selected_inp_path is None:
            return attempt_inp_path

    return selected_inp_path


def resolve_organize_metadata(
    state: Mapping[str, Any],
    reaction_dir: Path,
) -> tuple[Path | None, str, str]:
    inp_path = select_organize_metadata_inp_path(state, reaction_dir)
    if inp_path is None or not inp_path.exists():
        return None, "other", "unknown"

    resolution = resolve_molecule_key(inp_path)
    return inp_path, detect_job_type(inp_path), resolution.key


def compute_organize_plan(
    reaction_dir: Path,
    state: Mapping[str, Any],
    organized_root: Path,
) -> OrganizePlan:
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError(f"Completed state missing run_id: {reaction_dir}")

    selected_inp_value = state.get("selected_inp", "")
    selected_inp = selected_inp_value if isinstance(selected_inp_value, str) else ""
    _, job_type, molecule_key = resolve_organize_metadata(state, reaction_dir)

    final_result = state.get("final_result") or {}
    last_out_path = final_result.get("last_out_path", "")
    analyzer_status = final_result.get("analyzer_status", "")
    reason = final_result.get("reason", "")
    completed_at = final_result.get("completed_at", "")

    attempts = state.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0

    target_rel_path = f"{job_type}/{molecule_key}/{run_id}"
    target_abs_path = organized_root / target_rel_path

    return OrganizePlan(
        reaction_dir=reaction_dir,
        run_id=run_id,
        job_type=job_type,
        molecule_key=molecule_key,
        selected_inp=selected_inp,
        last_out_path=last_out_path if isinstance(last_out_path, str) else "",
        attempt_count=attempt_count,
        status=str(state.get("status", "")),
        analyzer_status=analyzer_status if isinstance(analyzer_status, str) else "",
        reason=reason if isinstance(reason, str) else "",
        completed_at=completed_at if isinstance(completed_at, str) else "",
        source_dir=reaction_dir,
        target_rel_path=target_rel_path,
        target_abs_path=target_abs_path,
    )


def plan_single(
    reaction_dir: Path,
    organized_root: Path,
) -> tuple[OrganizePlan | None, SkipReason | None]:
    state, skip = check_eligibility(reaction_dir)
    if skip is not None:
        return None, skip
    assert state is not None
    plan = compute_organize_plan(reaction_dir, state, organized_root)
    return plan, None


def plan_root_scan(
    root: Path,
    organized_root: Path,
) -> tuple[list[OrganizePlan], list[SkipReason]]:
    plans: list[OrganizePlan] = []
    skips: list[SkipReason] = []

    try:
        state_files = sorted(root.rglob("job_state.json"))
    except OSError as exc:
        logger.error("Cannot scan root: %s (%s)", root, exc)
        return plans, skips

    candidate_dirs: set[Path] = set()
    for f in state_files:
        candidate_dirs.add(f.parent)

    for entry in sorted(candidate_dirs):
        if entry.is_symlink():
            continue
        if is_subpath(entry, organized_root):
            continue
        plan, skip = plan_single(entry, organized_root)
        if plan is not None:
            plans.append(plan)
        if skip is not None:
            skips.append(skip)

    return plans, skips
