from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.indexing import (
    JobLocationRecord,
    get_job_location,
    list_job_locations,
    merge_job_locations,
    upsert_job_location,
)
from orca_auto.core.indexing import engine_artifacts as _engine_artifacts
from orca_auto.core.indexing import engine_records as _engine_locations
from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    should_exclude_from_production_runs_scan,
)
from orca_auto.core.statuses import TERMINAL_STATUSES
from orca_auto.core.utils.persistence import load_json_mapping_file

from ..config import AppConfig
from ..job_type import detect_job_type
from ..molecule_key import resolve_molecule_key
from ..state_reading import STATE_FILE_NAME, report_json_path, state_from_normalized_payload
from ._utils import (
    derive_selected_input_xyz,
    normalize_path_text,
    normalize_text,
    resource_dict_from_any,
)

_MOLECULE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")


def index_root_for_cfg(cfg: AppConfig) -> Path:
    return Path(cfg.runtime.allowed_root).expanduser().resolve()


def job_type_identifier(job_type: str) -> str:
    normalized = normalize_text(job_type).lower()
    if normalized.startswith("orca_"):
        return normalized
    return f"orca_{normalized or 'other'}"


def normalize_molecule_key(value: str) -> str:
    collapsed = _MOLECULE_KEY_RE.sub("_", normalize_text(value)).strip("._-")
    return collapsed or "unknown"


def molecule_key_from_selected_inp(selected_inp: str, job_dir: Path) -> str:
    raw = normalize_text(selected_inp)
    if raw:
        try:
            candidate = Path(raw).expanduser()
            resolved = candidate.resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved.exists():
            return resolve_molecule_key(resolved).key
        stem = Path(raw).stem.strip()
        if stem:
            return normalize_molecule_key(stem)
    return normalize_molecule_key(job_dir.name)


def resolve_job_metadata(selected_inp: str, job_dir: Path) -> tuple[str, str]:
    job_type = "other"
    raw = normalize_text(selected_inp)
    if raw:
        try:
            candidate = Path(raw).expanduser()
            resolved = candidate.resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved.exists():
            job_type = detect_job_type(resolved)
    molecule_key = molecule_key_from_selected_inp(raw, job_dir)
    return job_type, molecule_key


def resource_dict(max_cores: int, max_memory_gb: int) -> dict[str, int]:
    return _engine_locations.resource_dict(max_cores, max_memory_gb)


def build_job_location_record(
    *,
    existing: JobLocationRecord | None = None,
    job_id: str,
    status: str,
    job_dir: Path,
    job_type: str,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
) -> JobLocationRecord:
    selected_input_text = normalize_path_text(selected_input_xyz)
    return _engine_locations.build_job_location_record(
        existing=existing,
        job_id=job_id,
        app_name=ORCA_AUTO_ORCA_APP_NAME,
        job_type=job_type_identifier(job_type),
        status=status or "unknown",
        job_dir=job_dir,
        selected_input_xyz=selected_input_text,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
        default_molecule_key_fn=lambda original_run_dir, selected: molecule_key_from_selected_inp(
            selected,
            original_run_dir,
        ),
    )


def upsert_job_record(
    cfg: AppConfig,
    *,
    job_id: str,
    status: str,
    job_dir: Path,
    job_type: str,
    selected_input_xyz: str,
    molecule_key: str = "",
    resource_request: dict[str, int] | None = None,
    resource_actual: dict[str, int] | None = None,
) -> JobLocationRecord:
    root = index_root_for_cfg(cfg)
    existing = get_job_location(root, job_id)
    record = build_job_location_record(
        existing=existing,
        job_id=job_id,
        status=status,
        job_dir=job_dir,
        job_type=job_type,
        selected_input_xyz=selected_input_xyz,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
    )
    return upsert_job_location(root, record)


def list_job_location_records(index_root: str | Path) -> list[JobLocationRecord]:
    return list(list_job_locations(index_root))


def resolve_record_job_dir(record: JobLocationRecord) -> Path | None:
    for value in (record.latest_known_path, record.original_run_dir):
        raw = normalize_text(value)
        if not raw:
            continue
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_dir():
            return resolved
    return None


@dataclass(frozen=True)
class _ArtifactRecordPayloads:
    state: dict[str, Any]
    report: dict[str, Any]


@dataclass(frozen=True)
class _ArtifactRecordParts:
    job_id: str
    status: str
    selected_input_xyz: str
    job_type: str
    molecule_key: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]
    original_run_dir: str


def _artifact_payloads(
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
) -> _ArtifactRecordPayloads:
    return _ArtifactRecordPayloads(
        state=state or {},
        report=report or {},
    )


def _artifact_record_identity(
    *,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
    fallback_job_id: str,
) -> tuple[str, str, str]:
    sources = (report, state)
    job_id = (
        _engine_artifacts.first_artifact_text(sources, "job_id")
        or normalize_text(fallback_job_id)
        or normalize_text(existing.job_id if existing else "")
        or _engine_artifacts.first_artifact_text(sources, "run_id")
    )
    status = _engine_artifacts.first_artifact_text(sources, "status") or "unknown"
    selected_inp = normalize_path_text(
        _engine_artifacts.first_artifact_value((report, state), "selected_inp")
    )
    selected_input_xyz = normalize_path_text(
        _engine_artifacts.first_artifact_value(
            (report, state),
            "selected_input_xyz",
        )
    )
    if not selected_input_xyz.lower().endswith(".xyz"):
        selected_input_xyz = derive_selected_input_xyz(selected_inp)
    selected_input_xyz = (
        selected_input_xyz or selected_inp or (existing.selected_input_xyz if existing else "")
    )
    return job_id, status, selected_input_xyz


def _artifact_job_metadata(
    *,
    job_dir: Path,
    selected_input_xyz: str,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
    default_job_type: str,
) -> tuple[str, str]:
    derived_job_type, derived_molecule_key = resolve_job_metadata(selected_input_xyz, job_dir)
    sources = (report, state)
    job_type = (
        normalize_text(
            _engine_artifacts.first_artifact_value(sources, "job_type")
            or derived_job_type
            or default_job_type
        )
        or default_job_type
    )
    molecule_key = normalize_text(
        _engine_artifacts.first_artifact_value(sources, "molecule_key")
        or (existing.molecule_key if existing else "")
        or derived_molecule_key
    )
    return job_type, molecule_key


def _artifact_resources(
    *,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
) -> tuple[dict[str, int], dict[str, int]]:
    return _engine_artifacts.artifact_resources(
        state=state,
        report=report,
        existing=existing,
        resource_mapping_fn=resource_dict_from_any,
    )


def _artifact_dirs(
    *,
    job_dir: Path,
    state: dict[str, Any],
    report: dict[str, Any],
    existing: JobLocationRecord | None,
) -> str:
    sources = (report, state)
    return (
        _engine_artifacts.first_artifact_text(sources, "original_run_dir")
        or normalize_text(existing.original_run_dir if existing else "")
        or str(job_dir)
    )


def _artifact_record_parts(
    *,
    job_dir: Path,
    payloads: _ArtifactRecordPayloads,
    existing: JobLocationRecord | None,
    fallback_job_id: str,
    default_job_type: str,
) -> _ArtifactRecordParts | None:
    job_id, status, selected_input_xyz = _artifact_record_identity(
        state=payloads.state,
        report=payloads.report,
        existing=existing,
        fallback_job_id=fallback_job_id,
    )
    if not job_id:
        return None

    job_type, molecule_key = _artifact_job_metadata(
        job_dir=job_dir,
        selected_input_xyz=selected_input_xyz,
        state=payloads.state,
        report=payloads.report,
        existing=existing,
        default_job_type=default_job_type,
    )
    resource_request, resource_actual = _artifact_resources(
        state=payloads.state,
        report=payloads.report,
        existing=existing,
    )
    original_run_dir = _artifact_dirs(
        job_dir=job_dir,
        state=payloads.state,
        report=payloads.report,
        existing=existing,
    )
    return _ArtifactRecordParts(
        job_id=job_id,
        status=status,
        selected_input_xyz=selected_input_xyz,
        job_type=job_type,
        molecule_key=molecule_key,
        resource_request=resource_request,
        resource_actual=resource_actual,
        original_run_dir=original_run_dir,
    )


def _record_from_artifact_parts(
    *,
    existing: JobLocationRecord | None,
    parts: _ArtifactRecordParts,
) -> JobLocationRecord:
    return build_job_location_record(
        existing=existing,
        job_id=parts.job_id,
        status=parts.status,
        job_dir=Path(parts.original_run_dir),
        job_type=parts.job_type,
        selected_input_xyz=parts.selected_input_xyz,
        molecule_key=parts.molecule_key,
        resource_request=parts.resource_request,
        resource_actual=parts.resource_actual,
    )


def record_from_artifacts(
    *,
    job_dir: Path,
    state: dict[str, Any] | None,
    report: dict[str, Any] | None,
    existing: JobLocationRecord | None = None,
    fallback_job_id: str = "",
    default_job_type: str = "other",
) -> JobLocationRecord | None:
    payloads = _artifact_payloads(state, report)
    parts = _artifact_record_parts(
        job_dir=job_dir,
        payloads=payloads,
        existing=existing,
        fallback_job_id=fallback_job_id,
        default_job_type=default_job_type,
    )
    if parts is None:
        return None
    return _record_from_artifact_parts(
        existing=existing,
        parts=parts,
    )


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
    return _engine_artifacts.first_artifact_text(
        sources, "job_id"
    ) or _engine_artifacts.first_artifact_text(sources, "run_id")


def _artifact_status(state: dict[str, Any], report: dict[str, Any]) -> str:
    return _engine_artifacts.first_artifact_text((report, state), "status").lower()


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
