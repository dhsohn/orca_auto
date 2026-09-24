"""Rebuild ``job_locations.json`` from every ORCA state on disk.

The walk collects each directory's ``job_state.json`` by the job id it
names; the rows are then decided one job id at a time under the index lock
by ``_decide_rebuild_row``, whose freshness and conflict rule (pinned path
first, then terminal, then the row's own directory, then newest state; a
terminal row never turns non-terminal) is the whole of the merge policy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord, merge_job_locations
from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    should_exclude_from_production_runs_scan,
)
from orca_auto.core.statuses import TERMINAL_STATUSES
from orca_auto.core.utils.persistence import load_json_mapping_file

from ..state_reading import STATE_FILE_NAME, report_json_path, state_from_normalized_payload
from . import _artifact_records
from ._artifacts_to_records import record_from_artifacts
from ._records import build_job_location_record
from ._utils import normalize_text


@dataclass(frozen=True)
class JobLocationRebuildConflict:
    """One job id claimed by several directories: the kept one and the ignored ones."""

    job_id: str
    kept_path: str
    ignored_paths: tuple[str, ...]


@dataclass(frozen=True)
class JobLocationRebuildResult:
    index_path: str
    scanned: int
    skipped: tuple[str, ...]
    added: tuple[JobLocationRecord, ...]
    updated: tuple[JobLocationRecord, ...]
    unchanged: int
    total: int
    applied: bool
    conflicts: tuple[JobLocationRebuildConflict, ...] = ()


def _artifact_job_id(state: dict[str, Any], report: dict[str, Any]) -> str:
    """The identity ``record_from_artifacts`` would settle on without a fallback."""
    sources = (report, state)
    return _artifact_records.first_artifact_text(
        sources, "job_id"
    ) or _artifact_records.first_artifact_text(sources, "run_id")


def _artifact_status(state: dict[str, Any], report: dict[str, Any]) -> str:
    return _artifact_records.first_artifact_text((report, state), "status").lower()


def _record_found_at(
    job_dir: Path,
    *,
    state: dict[str, Any],
    report: dict[str, Any] | None,
    existing: JobLocationRecord | None,
) -> JobLocationRecord | None:
    """The artifact-derived row, with ``latest_known_path`` pinned to ``job_dir``.

    Every field comes from the artifacts (or the existing row they refine);
    only the location is what disk says now: the directory the state was
    found in, which is exactly what ``upsert_job_record`` records at
    submit/running/terminal time for the directory it is handed.
    """
    derived = record_from_artifacts(job_dir=job_dir, state=state, report=report, existing=existing)
    if derived is None:
        return None
    return build_job_location_record(
        existing=derived,
        job_id=derived.job_id,
        status=derived.status,
        job_dir=job_dir,
        job_type=derived.job_type,
        selected_input_xyz=derived.selected_input_xyz,
        molecule_key=derived.molecule_key,
        resource_request=dict(derived.resource_request),
        resource_actual=dict(derived.resource_actual),
    )


def _iter_state_dirs(root: Path) -> list[Path]:
    # Sorted so the walk, and the path-order tie break of ``_rank_discovery``,
    # are the same on every rebuild.
    directories: list[Path] = []
    for state_path in sorted(iter_production_runs_artifacts(root, STATE_FILE_NAME)):
        if should_exclude_from_production_runs_scan(state_path, root):
            continue
        directories.append(state_path.parent)
    return directories


@dataclass(frozen=True)
class _DiscoveredState:
    """One directory's ``job_state.json`` (plus ``report.json``) as read from disk."""

    job_dir: Path
    state: dict[str, Any]
    report: dict[str, Any] | None
    mtime_ns: int

    @property
    def job_id(self) -> str:
        return _artifact_job_id(self.state, self.report or {})

    @property
    def is_terminal(self) -> bool:
        return _artifact_status(self.state, self.report or {}) in TERMINAL_STATUSES


def _discover_state(job_dir: Path) -> _DiscoveredState | None:
    """Read ``job_dir``'s artifacts; ``None`` when there is no usable ORCA state."""
    state_file = job_dir / STATE_FILE_NAME
    state = load_json_mapping_file(state_file)
    if state is None or state_from_normalized_payload(state) is None:
        return None
    try:
        mtime_ns = state_file.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _DiscoveredState(
        job_dir=job_dir,
        state=state,
        report=load_json_mapping_file(report_json_path(job_dir)),
        mtime_ns=mtime_ns,
    )


def _existing_path(existing: JobLocationRecord | None) -> Path | None:
    raw = normalize_text(existing.latest_known_path) if existing is not None else ""
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _pinned_state(existing: JobLocationRecord | None) -> _DiscoveredState | None:
    """The existing row's ``latest_known_path`` re-read now, if it still holds this job.

    Read under the index lock, so the decision is made against what that
    directory says at merge time, not what the plan phase saw.
    """
    path = _existing_path(existing)
    if existing is None or path is None:
        return None
    found = _discover_state(path)
    if found is None or found.job_id != existing.job_id:
        return None
    return found


def _same_dir(job_dir: Path, other: Path | None) -> bool:
    if other is None:
        return False
    try:
        return job_dir.resolve() == other
    except OSError:
        return False


def _rank_discovery(
    found: _DiscoveredState, *, existing_path: Path | None
) -> tuple[int, int, int, str]:
    # Terminal first, then the existing row's own directory, then the most
    # recently written state, then path order.
    return (
        0 if found.is_terminal else 1,
        0 if _same_dir(found.job_dir, existing_path) else 1,
        -found.mtime_ns,
        str(found.job_dir),
    )


def _refined_existing(existing: JobLocationRecord, derived: JobLocationRecord) -> JobLocationRecord:
    """The existing row with only its empty fields filled from ``derived``.

    Status and ``latest_known_path`` are the existing row's: the candidate lost
    the freshness comparison, so it may complete the row but not move it.
    """
    return replace(
        existing,
        job_type=existing.job_type or derived.job_type,
        original_run_dir=existing.original_run_dir or derived.original_run_dir,
        molecule_key=existing.molecule_key or derived.molecule_key,
        selected_input_xyz=existing.selected_input_xyz or derived.selected_input_xyz,
        resource_request=dict(existing.resource_request or derived.resource_request),
        resource_actual=dict(existing.resource_actual or derived.resource_actual),
    )


@dataclass(frozen=True)
class _RebuildDecision:
    record: JobLocationRecord | None
    kept_path: str
    ignored_paths: tuple[str, ...]


def _decide_rebuild_row(
    existing: JobLocationRecord | None, discovered: list[_DiscoveredState]
) -> _RebuildDecision:
    """Pick the directory a job id's row follows; monotonic against ``existing``.

    Called inside the index lock with the row as it is now. The rules:

    * a directory that still holds this job's state at the row's
      ``latest_known_path`` keeps the row there; its state is re-read now, so
      a worker's terminal write between the plan and this merge is seen;
    * otherwise the discovered directories are ranked terminal first, then the
      row's own directory, then newest ``job_state.json``, then path order;
    * a terminal row is never turned non-terminal: such a candidate may only
      fill fields the row lacks.

    Every other discovered directory is reported as ignored so the caller can
    surface the conflict instead of choosing silently.
    """
    existing_path = _existing_path(existing)
    pinned = _pinned_state(existing)
    if pinned is not None:
        winner = pinned
        ignored = [found for found in discovered if not _same_dir(found.job_dir, pinned.job_dir)]
    else:
        ranked = sorted(
            discovered, key=lambda found: _rank_discovery(found, existing_path=existing_path)
        )
        winner, ignored = ranked[0], ranked[1:]
    record = _record_found_at(
        winner.job_dir, state=winner.state, report=winner.report, existing=existing
    )
    kept_path = str(winner.job_dir)
    if (
        existing is not None
        and record is not None
        and existing.status in TERMINAL_STATUSES
        and record.status not in TERMINAL_STATUSES
    ):
        record = _refined_existing(existing, record)
        kept_path = existing.latest_known_path
        ignored = [found for found in discovered if not _same_dir(found.job_dir, existing_path)]
    return _RebuildDecision(
        record=record,
        kept_path=kept_path,
        ignored_paths=tuple(str(found.job_dir) for found in ignored),
    )


def rebuild_job_location_records(
    index_root: str | Path, *, apply: bool = True
) -> JobLocationRebuildResult:
    """Re-derive ``job_locations.json`` rows from every ORCA state on disk.

    The runs root is walked with the production artifact iterator and each
    ``job_state.json`` (plus its ``report.json`` when present) is collected by
    the job id it names, without a lock. The rows are then decided one job id
    at a time under the index lock (``merge_job_locations`` with
    ``_decide_rebuild_row``), against the row as it is at that moment, by the
    same field rules the worker applies at terminal time. Rows for directories
    that no longer exist are left alone (``index prune`` owns removal); a state
    that names neither a job id nor a run id cannot be indexed and is reported
    as skipped; a job id found in several directories is reported as a
    conflict naming the directory kept. Without ``apply`` nothing is written.
    """
    root = Path(index_root).expanduser().resolve()
    discovered: dict[str, list[_DiscoveredState]] = {}
    skipped: list[str] = []
    directories = _iter_state_dirs(root)
    for job_dir in directories:
        found = _discover_state(job_dir)
        if found is None or not found.job_id:
            skipped.append(str(job_dir))
            continue
        discovered.setdefault(found.job_id, []).append(found)

    conflicts: list[JobLocationRebuildConflict] = []

    def decide(
        existing: JobLocationRecord | None, group: list[_DiscoveredState]
    ) -> JobLocationRecord | None:
        decision = _decide_rebuild_row(existing, group)
        if decision.ignored_paths:
            conflicts.append(
                JobLocationRebuildConflict(
                    job_id=group[0].job_id,
                    kept_path=decision.kept_path,
                    ignored_paths=decision.ignored_paths,
                )
            )
        return decision.record

    merged = merge_job_locations(root, discovered, decide=decide, apply=apply)
    return JobLocationRebuildResult(
        index_path=merged.index_path,
        scanned=len(directories),
        skipped=tuple(skipped),
        added=merged.added,
        updated=merged.updated,
        unchanged=merged.unchanged,
        total=merged.total,
        applied=merged.applied,
        conflicts=tuple(conflicts),
    )
