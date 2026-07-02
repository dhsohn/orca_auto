"""Utility for discovering files to be indexed for DFT."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orca_auto.core.utils import parse_iso_utc

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
    recent_completed_window_minutes: int | None = None,
) -> list[DiscoveredTarget]:
    """Return a list of ORCA output files to be indexed.

    Rules:
    1) Default (containing `orca_runs`): based on job_state.json
       - Status is trusted only from job_state.status
       - Output file tracks the latest .out in the job_state.json folder
    2) `orca_outputs`:
       - Uses only job_state.json (job_report is ignored)
       - Status is trusted only from job_state.status
       - Output file tracks the latest .out in the job_state.json folder
    """
    parts = {part.lower() for part in kb_path.parts}
    if "orca_outputs" in parts:
        return _discover_orca_outputs_targets(
            kb_path=kb_path,
            max_bytes=max_bytes,
            recent_completed_window_minutes=recent_completed_window_minutes,
        )
    return _discover_orca_runs_targets(
        kb_path=kb_path,
        max_bytes=max_bytes,
    )


def _discover_orca_outputs_targets(
    *,
    kb_path: Path,
    max_bytes: int,
    recent_completed_window_minutes: int | None,
) -> list[DiscoveredTarget]:
    targets: dict[str, DiscoveredTarget] = {}
    now_utc = datetime.now(UTC)

    # job_state-only policy: job_report is completely ignored.
    for state_path in kb_path.rglob("job_state.json"):
        data = _load_report_json(state_path)
        if not isinstance(data, dict):
            continue
        status = _state_status(data)
        if status != "completed":
            continue
        resolved = _find_latest_out_in_dir(state_path.parent)
        if resolved is None:
            continue
        if not _is_recent_completed_output(
            data=data,
            output_path=resolved,
            now_utc=now_utc,
            recent_completed_window_minutes=recent_completed_window_minutes,
        ):
            continue
        _add_if_valid_target(
            resolved=resolved,
            max_bytes=max_bytes,
            targets=targets,
            run_state_status=status,
        )

    return sorted(targets.values(), key=lambda t: str(t.path))


def _discover_orca_runs_targets(
    *,
    kb_path: Path,
    max_bytes: int,
) -> list[DiscoveredTarget]:
    targets: dict[str, DiscoveredTarget] = {}

    for state_path in kb_path.rglob("job_state.json"):
        data = _load_report_json(state_path)
        if not isinstance(data, dict):
            continue

        status = _state_status(data)

        # Path info (reaction_dir, last_out_path) can be corrupted due to
        # runtime environment differences, so we trust only the latest .out
        # in the folder where job_state.json is located.
        resolved = _find_latest_out_in_dir(state_path.parent)
        if resolved is None:
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


def _is_recent_completed_output(
    *,
    data: dict[str, Any],
    output_path: Path,
    now_utc: datetime,
    recent_completed_window_minutes: int | None,
) -> bool:
    if recent_completed_window_minutes is None:
        return True
    if recent_completed_window_minutes < 0:
        return True

    engine_payload = data.get("engine_payload")
    engine_payload_dict = engine_payload if isinstance(engine_payload, dict) else {}
    final_result = engine_payload_dict.get("final_result")
    final_result_dict = final_result if isinstance(final_result, dict) else {}

    top_status = _state_status(data)
    final_status = str(final_result_dict.get("status", "")).strip().lower()
    if "completed" not in {top_status, final_status}:
        return False

    window = timedelta(minutes=recent_completed_window_minutes)
    allowed_future_skew = timedelta(minutes=5)
    completed_at_raw = final_result_dict.get("completed_at")
    completed_at = _parse_iso_datetime_utc(completed_at_raw)
    if completed_at is not None:
        age = now_utc - completed_at
        return (-allowed_future_skew) <= age <= window

    try:
        mtime_dt = datetime.fromtimestamp(output_path.stat().st_mtime, tz=UTC)
    except OSError:
        return False
    age = now_utc - mtime_dt
    return (-allowed_future_skew) <= age <= window


def _parse_iso_datetime_utc(value: Any) -> datetime | None:
    return parse_iso_utc(value)


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
