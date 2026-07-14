"""Utility for discovering files to be indexed for DFT."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    path_is_inside_workflow_workspace,
    should_exclude_from_production_runs_scan,
)

logger = logging.getLogger(__name__)

_ORCA_EXTENSIONS = {".out"}


@dataclass
class DiscoveredTarget:
    """Discovered ORCA output file with job_state metadata."""

    path: Path
    run_state_status: str = ""


def discover_orca_targets(
    kb_path: Path,
    *,
    max_bytes: int,
) -> list[DiscoveredTarget]:
    """Return a list of ORCA output files to be indexed.

    Discovery is based on job_state.json:
    - Status is trusted only from job_state.status
    - Output file tracks the latest .out in the job_state.json folder
    - Workflow workspaces under the same runs root are skipped; their jobs
      are reported through the workflow views instead
    """
    targets: dict[str, DiscoveredTarget] = {}

    for state_path in iter_production_runs_artifacts(kb_path, "job_state.json"):
        if should_exclude_from_production_runs_scan(state_path, kb_path):
            continue
        if path_is_inside_workflow_workspace(state_path.parent, kb_path):
            continue
        data = _load_report_json(state_path)
        if not isinstance(data, dict):
            continue

        status = _state_status(data)

        # Path info (reaction_dir, last_out_path) can be corrupted due to
        # runtime environment differences, so we trust only the latest .out
        # in the folder where job_state.json is located.
        resolved = _find_latest_out_in_dir(state_path.parent)
        if resolved is None or should_exclude_from_production_runs_scan(resolved, kb_path):
            continue
        _add_if_valid_target(
            resolved=resolved,
            max_bytes=max_bytes,
            targets=targets,
            run_state_status=status,
        )

    return sorted(targets.values(), key=lambda t: str(t.path))


def _find_latest_out_in_dir(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    latest: tuple[float, Path] | None = None
    for candidate in directory.glob("*.out"):
        if not candidate.is_file():
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest[0]:
            latest = (mtime, candidate)
    return latest[1] if latest is not None else None


def _state_status(data: dict[str, Any]) -> str:
    status = data.get("status")
    if isinstance(status, dict):
        return str(status.get("state", "")).strip().lower()
    return str(status or "").strip().lower()


def _add_if_valid_target(
    *,
    resolved: Path,
    max_bytes: int,
    targets: dict[str, DiscoveredTarget],
    run_state_status: str = "",
) -> None:
    if resolved.suffix.lower() not in _ORCA_EXTENSIONS:
        return
    try:
        if resolved.stat().st_size > max_bytes:
            return
    except OSError:
        return
    targets[str(resolved)] = DiscoveredTarget(
        path=resolved,
        run_state_status=run_state_status,
    )


def _load_report_json(report_path: Path) -> dict[str, Any] | None:
    try:
        with open(report_path, encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "dft_job_report_parse_failed: path=%s error=%s",
            report_path,
            exc,
        )
        return None
