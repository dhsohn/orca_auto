from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orca_auto.core.activity_icons import activity_status_icon
from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.paths import resolve_artifact_path
from orca_auto.core.utils import parse_iso_utc as _parse_iso_utc

from .dft.discovery import _find_latest_out_in_dir
from .job_locations import list_job_location_records, resolve_record_job_dir
from .state import STATE_FILE_NAME, load_state


@dataclass(frozen=True)
class RunSnapshot:
    key: str
    name: str
    reaction_dir: Path
    run_id: str
    status: str
    started_at: str
    updated_at: str
    completed_at: str
    selected_inp_name: str
    attempts: int
    latest_out_path: Path | None
    final_reason: str
    elapsed: float
    elapsed_text: str


def status_icon(status: str) -> str:
    return activity_status_icon(status)


def parse_iso_utc(value: Any) -> datetime | None:
    return _parse_iso_utc(value)


def elapsed_text(seconds: float) -> str:
    if seconds < 0:
        return "-"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    secs = int(seconds % 60)
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _compute_elapsed(state: Mapping[str, Any]) -> float:
    started = parse_iso_utc(state.get("started_at"))
    if started is None:
        return -1.0

    status = str(state.get("status", "")).lower()
    if status in ("completed", "failed"):
        ended = parse_iso_utc(state.get("updated_at"))
        if ended is not None:
            return (ended - started).total_seconds()

    return (datetime.now(UTC) - started).total_seconds()


def _latest_out_path(reaction_dir: Path, state: Mapping[str, Any]) -> Path | None:
    final_result = state.get("final_result")
    if isinstance(final_result, dict):
        last_out_path = final_result.get("last_out_path")
        if isinstance(last_out_path, str) and last_out_path.strip():
            resolved = resolve_artifact_path(last_out_path, reaction_dir)
            if resolved is not None:
                return resolved

    attempts = state.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            if not isinstance(attempt, dict):
                continue
            out_path = attempt.get("out_path")
            if isinstance(out_path, str) and out_path.strip():
                resolved = resolve_artifact_path(out_path, reaction_dir)
                if resolved is not None:
                    return resolved

    return _find_latest_out_in_dir(reaction_dir)


def _dir_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path)


def _original_run_dir(record: JobLocationRecord) -> Path | None:
    raw = record.original_run_dir
    if not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _snapshot_name(
    allowed_root: Path, reaction_dir: Path, *, original_run_dir: Path | None = None
) -> str:
    for candidate in (original_run_dir, reaction_dir):
        if candidate is None:
            continue
        try:
            return str(candidate.relative_to(allowed_root))
        except ValueError:
            continue
    return reaction_dir.name


def _candidate_snapshot_dirs(allowed_root: Path) -> list[tuple[Path, Path | None]]:
    candidates: list[tuple[Path, Path | None]] = []
    seen: set[str] = set()

    for record in list_job_location_records(allowed_root):
        reaction_dir = resolve_record_job_dir(record)
        if reaction_dir is None:
            continue
        key = _dir_key(reaction_dir)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((reaction_dir, _original_run_dir(record)))

    for state_path in allowed_root.rglob(STATE_FILE_NAME):
        reaction_dir = state_path.parent
        key = _dir_key(reaction_dir)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((reaction_dir, None))

    return candidates


def collect_run_snapshots(allowed_root: Path) -> list[RunSnapshot]:
    snapshots: list[RunSnapshot] = []
    if not allowed_root.is_dir():
        return snapshots

    for reaction_dir, original_run_dir in _candidate_snapshot_dirs(allowed_root):
        state = load_state(reaction_dir)
        if state is None:
            continue

        final_result = state.get("final_result")
        completed_at = ""
        final_reason = ""
        if isinstance(final_result, dict):
            completed_at = str(final_result.get("completed_at", "")).strip()
            final_reason = str(final_result.get("reason", "")).strip()

        selected_inp = state.get("selected_inp", "")
        selected_inp_name = "-"
        if isinstance(selected_inp, str) and selected_inp.strip():
            selected_inp_name = Path(selected_inp).name

        run_id = str(state.get("run_id", "")).strip()
        reaction_name = _snapshot_name(
            allowed_root,
            reaction_dir,
            original_run_dir=original_run_dir,
        )
        elapsed = _compute_elapsed(state)

        snapshots.append(
            RunSnapshot(
                key=run_id or str(reaction_dir),
                name=reaction_name,
                reaction_dir=reaction_dir,
                run_id=run_id,
                status=str(state.get("status", "unknown")).strip().lower(),
                started_at=str(state.get("started_at", "")),
                updated_at=str(state.get("updated_at", "")),
                completed_at=completed_at,
                selected_inp_name=selected_inp_name,
                attempts=len(state.get("attempts", [])),
                latest_out_path=_latest_out_path(reaction_dir, state),
                final_reason=final_reason,
                elapsed=elapsed,
                elapsed_text=elapsed_text(elapsed),
            )
        )

    snapshots.sort(key=lambda snapshot: snapshot.started_at, reverse=True)
    return snapshots


def sort_snapshots_by_started(snapshots: Iterable[RunSnapshot]) -> list[RunSnapshot]:
    def _key(snapshot: RunSnapshot) -> tuple[int, datetime]:
        parsed = parse_iso_utc(snapshot.started_at)
        if parsed is None:
            return (1, datetime.min.replace(tzinfo=UTC))
        return (0, parsed)

    return sorted(snapshots, key=_key)


def sort_snapshots_by_completed(snapshots: Iterable[RunSnapshot]) -> list[RunSnapshot]:
    def _key(snapshot: RunSnapshot) -> datetime:
        parsed = parse_iso_utc(snapshot.completed_at) or parse_iso_utc(snapshot.updated_at)
        return parsed or datetime.min.replace(tzinfo=UTC)

    return sorted(snapshots, key=_key, reverse=True)
