from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    path_is_inside_workflow_workspace,
    should_exclude_from_production_runs_scan,
)
from orca_auto.core.utils.persistence import load_json_mapping_file

from .job_locations import list_job_location_records, resolve_record_job_dir
from .state import STATE_FILE_NAME, _state_from_normalized_payload

StateFileIdentity = tuple[int, int, int, int]


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
    reaction_dir_identity: tuple[int, int] | None = None
    state_file_identity: StateFileIdentity | None = None
    state_run_identity: str = ""
    state_generation_identity: str = ""


def _dir_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve())
    except OSError:
        return str(path)


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        path_stat = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(path_stat.st_mode):
        return None
    return path_stat.st_dev, path_stat.st_ino


def _state_file_identity(path_stat: os.stat_result) -> StateFileIdentity:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_size,
        path_stat.st_mtime_ns,
    )


def _state_publication_identity(
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    job = payload.get("job")
    job_payload = dict(job) if isinstance(job, Mapping) else {}
    engine = payload.get("engine_payload")
    engine_payload = dict(engine) if isinstance(engine, Mapping) else {}
    run_identity = str(engine_payload.get("run_id") or job_payload.get("id") or "").strip()
    generation_identity = str(
        job_payload.get("generation")
        or job_payload.get("queue_id")
        or job_payload.get("task_id")
        or run_identity
    ).strip()
    return run_identity, generation_identity


def _load_pinned_state(
    directory_fd: int,
) -> tuple[dict[str, Any], StateFileIdentity] | None:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        state_fd = os.open(STATE_FILE_NAME, flags, dir_fd=directory_fd)
    except OSError:
        return None

    try:
        opened_identity = _state_file_identity(os.fstat(state_fd))
        payload = load_json_mapping_file(Path("/proc/self/fd") / str(state_fd))
        if payload is None:
            return None
        current_identity = _state_file_identity(
            os.stat(STATE_FILE_NAME, dir_fd=directory_fd, follow_symlinks=False)
        )
        if current_identity != opened_identity:
            return None
        return payload, opened_identity
    except OSError:
        return None
    finally:
        os.close(state_fd)


def _open_snapshot_directory(
    path: Path,
    *,
    expected_identity: tuple[int, int],
) -> int | None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return None

    try:
        opened_stat = os.fstat(directory_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != expected_identity:
            raise OSError("snapshot directory identity changed")
        pinned_path = Path("/proc/self/fd") / str(directory_fd)
        pinned_stat = pinned_path.stat()
        if (pinned_stat.st_dev, pinned_stat.st_ino) != expected_identity:
            raise OSError("snapshot directory fd path identity changed")
    except OSError:
        os.close(directory_fd)
        return None
    return directory_fd


def _snapshot_directory_is_still_production_visible(
    reaction_dir: Path,
    allowed_root: Path,
    directory_fd: int,
    *,
    expected_identity: tuple[int, int],
    original_run_dir: Path | None,
) -> bool:
    try:
        opened_stat = os.fstat(directory_fd)
        path_stat = reaction_dir.lstat()
    except OSError:
        return False
    opened_identity = (opened_stat.st_dev, opened_stat.st_ino)
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or opened_identity != expected_identity
        or (path_stat.st_dev, path_stat.st_ino) != opened_identity
    ):
        return False
    return not (
        should_exclude_from_production_runs_scan(reaction_dir, allowed_root)
        or should_exclude_from_production_runs_scan(
            reaction_dir / STATE_FILE_NAME,
            allowed_root,
        )
        or (
            original_run_dir is not None
            and should_exclude_from_production_runs_scan(original_run_dir, allowed_root)
        )
    )


def _original_run_dir(record: JobLocationRecord) -> Path | None:
    raw = record.original_run_dir
    if not raw.strip():
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _record_has_excluded_path(record: JobLocationRecord, allowed_root: Path) -> bool:
    for raw in (record.latest_known_path, record.original_run_dir):
        if str(raw).strip() and should_exclude_from_production_runs_scan(raw, allowed_root):
            return True
    return False


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


def _candidate_snapshot_dirs(
    allowed_root: Path,
) -> list[tuple[Path, Path | None, tuple[int, int]]]:
    candidates: list[tuple[Path, Path | None, tuple[int, int]]] = []
    seen: set[str] = set()

    for record in list_job_location_records(allowed_root):
        if _record_has_excluded_path(record, allowed_root):
            continue
        reaction_dir = resolve_record_job_dir(record)
        if reaction_dir is None:
            continue
        original_run_dir = _original_run_dir(record)
        if (
            should_exclude_from_production_runs_scan(reaction_dir, allowed_root)
            or should_exclude_from_production_runs_scan(
                reaction_dir / STATE_FILE_NAME,
                allowed_root,
            )
            or (
                original_run_dir is not None
                and should_exclude_from_production_runs_scan(original_run_dir, allowed_root)
            )
        ):
            continue
        if path_is_inside_workflow_workspace(reaction_dir, allowed_root):
            continue
        key = _dir_key(reaction_dir)
        if key in seen:
            continue
        identity = _directory_identity(reaction_dir)
        if identity is None:
            continue
        seen.add(key)
        candidates.append((reaction_dir, original_run_dir, identity))

    for state_path in iter_production_runs_artifacts(allowed_root, STATE_FILE_NAME):
        if should_exclude_from_production_runs_scan(state_path, allowed_root):
            continue
        reaction_dir = state_path.parent
        # Workflow-internal jobs surface through the workflow activity view;
        # listing them here too would double-count them as standalone runs.
        if path_is_inside_workflow_workspace(reaction_dir, allowed_root):
            continue
        key = _dir_key(reaction_dir)
        if key in seen:
            continue
        identity = _directory_identity(reaction_dir)
        if identity is None:
            continue
        seen.add(key)
        candidates.append((reaction_dir, None, identity))

    return candidates


def collect_run_snapshots(allowed_root: Path) -> list[RunSnapshot]:
    snapshots: list[RunSnapshot] = []
    if not allowed_root.is_dir():
        return snapshots

    for reaction_dir, original_run_dir, reaction_dir_identity in _candidate_snapshot_dirs(
        allowed_root
    ):
        directory_fd = _open_snapshot_directory(
            reaction_dir,
            expected_identity=reaction_dir_identity,
        )
        if directory_fd is None:
            continue
        try:
            loaded_state = _load_pinned_state(directory_fd)
            if loaded_state is None:
                continue
            state_payload, state_file_identity = loaded_state
            state = _state_from_normalized_payload(state_payload)
            if state is None:
                continue
            state_run_identity, state_generation_identity = _state_publication_identity(
                state_payload
            )

            final_result = state.get("final_result")
            completed_at = ""
            if isinstance(final_result, dict):
                completed_at = str(final_result.get("completed_at", "")).strip()

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
            if not _snapshot_directory_is_still_production_visible(
                reaction_dir,
                allowed_root,
                directory_fd,
                expected_identity=reaction_dir_identity,
                original_run_dir=original_run_dir,
            ):
                continue

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
                    reaction_dir_identity=reaction_dir_identity,
                    state_file_identity=state_file_identity,
                    state_run_identity=state_run_identity,
                    state_generation_identity=state_generation_identity,
                )
            )
        finally:
            os.close(directory_fd)

    snapshots.sort(key=lambda snapshot: snapshot.started_at, reverse=True)
    return snapshots
