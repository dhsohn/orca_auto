from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_ORCA_APP_NAME
from orca_auto.core.indexing import (
    JobLocationRecord,
    get_job_location,
    list_job_locations,
    upsert_job_location,
)
from orca_auto.core.indexing import engine_artifacts as _engine_artifacts
from orca_auto.core.indexing import engines as _engine_locations
from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    path_is_inside_workflow_workspace,
    should_exclude_from_production_runs_scan,
)
from orca_auto.core.utils.persistence import load_json_mapping_file

from ..config import AppConfig
from ..job_type import detect_job_type
from ..molecule_key import resolve_molecule_key
from ..state import state_path
from ._utils import (
    TERMINAL_STATUSES,
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


def is_terminal_status(status: str) -> bool:
    return normalize_text(status).lower() in TERMINAL_STATUSES


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


def _load_artifact_record_payloads(job_dir: Path) -> _ArtifactRecordPayloads:
    state_data = load_json_mapping_file(state_path(job_dir))
    return _artifact_payloads(
        dict(state_data) if state_data is not None else {},
        {},
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


def _record_from_job_dir_artifacts(
    job_dir: Path,
    *,
    artifact_dir: Path | None = None,
) -> JobLocationRecord | None:
    payloads = _load_artifact_record_payloads(artifact_dir or job_dir)
    record = record_from_artifacts(
        job_dir=job_dir,
        state=payloads.state,
        report=payloads.report,
    )
    return record


def _reindex_payload_from_record(record: JobLocationRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "status": record.status,
        "job_type": record.job_type,
        "job_dir": record.original_run_dir,
        "selected_input_xyz": record.selected_input_xyz,
        "molecule_key": record.molecule_key,
        "resource_request": dict(record.resource_request),
        "resource_actual": dict(record.resource_actual),
    }


def collect_reindex_payload(job_dir: Path) -> dict[str, Any] | None:
    resolved_job_dir = job_dir.expanduser().resolve()
    record = _record_from_job_dir_artifacts(resolved_job_dir)
    if record is None:
        return None
    return _reindex_payload_from_record(record)


def _job_dir_has_unsafe_reindex_artifact(job_dir: Path, root: Path) -> bool:
    for filename in ("job_state.json",):
        artifact = job_dir / filename
        try:
            present = artifact.exists() or artifact.is_symlink()
        except OSError:
            return True
        if present and should_exclude_from_production_runs_scan(artifact, root):
            return True
    return False


@dataclass(frozen=True)
class _ReindexSourceIdentity:
    directory: tuple[int, int]
    artifacts: tuple[tuple[str, int, int, int, int] | None, ...]


def _safe_reindex_source_identity(
    job_dir: Path,
    root: Path,
    *,
    opened_directory: bool = False,
) -> _ReindexSourceIdentity | None:
    if should_exclude_from_production_runs_scan(job_dir, root):
        return None
    try:
        directory_stat = job_dir.stat() if opened_directory else job_dir.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(directory_stat.st_mode):
        return None
    if _job_dir_has_unsafe_reindex_artifact(job_dir, root):
        return None

    artifact_rows: list[tuple[str, int, int, int, int] | None] = []
    for filename in ("job_state.json",):
        artifact = job_dir / filename
        try:
            artifact_stat = artifact.lstat()
        except FileNotFoundError:
            artifact_rows.append(None)
            continue
        except OSError:
            return None
        if not stat.S_ISREG(artifact_stat.st_mode):
            return None
        artifact_rows.append(
            (
                filename,
                artifact_stat.st_dev,
                artifact_stat.st_ino,
                artifact_stat.st_size,
                artifact_stat.st_mtime_ns,
            )
        )
    return _ReindexSourceIdentity(
        directory=(directory_stat.st_dev, directory_stat.st_ino),
        artifacts=tuple(artifact_rows),
    )


def _open_reindex_source(
    job_dir: Path,
    root: Path,
    *,
    expected_identity: _ReindexSourceIdentity,
) -> tuple[int, Path] | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(job_dir, flags)
    except OSError:
        return None

    try:
        pinned_job_dir = Path("/proc/self/fd") / str(directory_fd)
        if (
            _safe_reindex_source_identity(
                pinned_job_dir,
                root,
                opened_directory=True,
            )
            != expected_identity
        ):
            raise OSError("reindex source identity changed before it was opened")
    except OSError:
        os.close(directory_fd)
        return None
    return directory_fd, pinned_job_dir


def _candidate_reindex_dirs(root: Path) -> set[Path]:
    candidate_dirs: set[Path] = set()
    excluded_dirs: set[Path] = set()
    for pattern in ("job_state.json",):
        for artifact in iter_production_runs_artifacts(root, pattern):
            job_dir = artifact.parent
            if _job_dir_has_unsafe_reindex_artifact(job_dir, root):
                excluded_dirs.add(job_dir)
                candidate_dirs.discard(job_dir)
                continue
            # Workflow workspaces share the runs root; their internal jobs are
            # indexed by their own stage roots, not the standalone index.
            if path_is_inside_workflow_workspace(job_dir, root):
                continue
            if job_dir not in excluded_dirs:
                candidate_dirs.add(job_dir)
    return candidate_dirs - excluded_dirs


def reindex_job_locations(cfg: AppConfig) -> int:
    root = index_root_for_cfg(cfg)
    if not root.exists():
        return 0

    updated = 0
    for job_dir in sorted(_candidate_reindex_dirs(root)):
        source_identity = _safe_reindex_source_identity(job_dir, root)
        if source_identity is None:
            continue
        opened_source = _open_reindex_source(
            job_dir,
            root,
            expected_identity=source_identity,
        )
        if opened_source is None:
            continue
        directory_fd, pinned_job_dir = opened_source
        try:
            record = _record_from_job_dir_artifacts(
                job_dir,
                artifact_dir=pinned_job_dir,
            )
        finally:
            os.close(directory_fd)
        if record is None:
            continue
        if _safe_reindex_source_identity(job_dir, root) != source_identity:
            continue
        if should_exclude_from_production_runs_scan(record.original_run_dir, root):
            continue
        upsert_job_location(root, record)
        updated += 1
    return updated
