from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.engine_catalog import find_engine_catalog_entry
from orca_auto.core.paths.workflow import (
    iter_workflow_runtime_workspaces,
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths,
    workflow_workspace_internal_engine_paths_from_path,
)

from .location import JobLocationRecord
from .store import list_job_locations, resolve_job_location
from .text import normalize_index_text as normalize_text

_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_identifier(value: str, *, default: str) -> str:
    collapsed = _KEY_RE.sub("_", normalize_text(value)).strip("._-")
    return collapsed.lower() or default


def index_root_for_cfg(cfg: Any) -> Path:
    return Path(cfg.runtime.allowed_root).expanduser().resolve()


def append_unique_root(roots: list[Path], candidate: Path) -> None:
    resolved = candidate.expanduser().resolve()
    if resolved not in roots:
        roots.append(resolved)


def runtime_roots_for_cfg(cfg: Any, *, engine: str) -> tuple[Path, ...]:
    roots: list[Path] = []
    append_unique_root(roots, index_root_for_cfg(cfg))

    catalog_entry = find_engine_catalog_entry(engine)
    if catalog_entry is not None and catalog_entry.workflow_stage_role == "none":
        return tuple(roots)

    workflow_root = normalize_text(getattr(cfg, "workflow_root", ""))
    if not workflow_root:
        return tuple(roots)

    for workspace_dir in iter_workflow_runtime_workspaces(workflow_root, engine=engine):
        for stage_dirname in workflow_stage_dirnames_for_engine(engine):
            runtime_paths = workflow_workspace_internal_engine_paths(
                workspace_dir,
                engine=engine,
                stage_dirname=stage_dirname,
            )
            allowed_root = runtime_paths["allowed_root"]
            if allowed_root.exists():
                append_unique_root(roots, allowed_root)
    return tuple(roots)


def index_root_for_path(
    cfg: Any,
    *paths: str | Path | None,
    engine: str,
) -> Path:
    workflow_root = normalize_text(getattr(cfg, "workflow_root", ""))
    if workflow_root:
        for raw_path in paths:
            text = normalize_text(raw_path)
            if not text:
                continue
            runtime_paths = workflow_workspace_internal_engine_paths_from_path(
                text,
                workflow_root=workflow_root,
                engine=engine,
            )
            if runtime_paths is None:
                continue
            return runtime_paths["allowed_root"].expanduser().resolve()
    return index_root_for_cfg(cfg)


def lookup_roots_for_target(cfg: Any, target: str, *, engine: str) -> tuple[Path, ...]:
    roots = list(runtime_roots_for_cfg(cfg, engine=engine))
    specific_root = index_root_for_path(cfg, target, engine=engine)
    if specific_root in roots:
        roots.remove(specific_root)
        roots.insert(0, specific_root)
    return tuple(roots)


def list_job_records_for_cfg(
    cfg: Any,
    *,
    engine: str,
    list_job_locations_fn: Callable[[str | Path], list[JobLocationRecord]] = list_job_locations,
) -> list[tuple[Path, JobLocationRecord]]:
    rows: list[tuple[Path, JobLocationRecord]] = []
    for root in runtime_roots_for_cfg(cfg, engine=engine):
        for record in list_job_locations_fn(root):
            rows.append((root, record))
    return rows


def resolve_job_location_for_cfg(
    cfg: Any,
    target: str,
    *,
    engine: str,
    resolve_job_location_fn: Callable[
        [str | Path, str], JobLocationRecord | None
    ] = resolve_job_location,
) -> tuple[Path | None, JobLocationRecord | None]:
    for root in lookup_roots_for_target(cfg, target, engine=engine):
        record = resolve_job_location_fn(root, target)
        if record is not None:
            return root, record
    return None, None


def resolve_latest_job_dir(
    index_root: str | Path,
    target: str,
    *,
    resolve_job_location_fn: Callable[
        [str | Path, str], JobLocationRecord | None
    ] = resolve_job_location,
) -> Path | None:
    record = resolve_job_location_fn(index_root, target)
    if record is None:
        candidate = Path(normalize_text(target)).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved.exists() and resolved.is_dir() else None

    candidates = [record.latest_known_path, record.original_run_dir]
    for latest in candidates:
        if not latest:
            continue
        path = Path(latest).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.exists() and resolved.is_dir():
            return resolved
    return None


def load_job_artifacts(
    index_root: str | Path,
    target: str,
    *,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None] | None,
    resolve_latest_job_dir_fn: Callable[[str | Path, str], Path | None],
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    job_dir = resolve_latest_job_dir_fn(index_root, target)
    if job_dir is None:
        return None, None, None
    report = load_report_json_fn(job_dir) if load_report_json_fn is not None else None
    return job_dir, load_state_fn(job_dir), report


def load_job_artifacts_for_cfg(
    cfg: Any,
    target: str,
    *,
    engine: str,
    load_state_fn: Callable[[Path], dict[str, Any] | None],
    load_report_json_fn: Callable[[Path], dict[str, Any] | None] | None,
    resolve_latest_job_dir_fn: Callable[[str | Path, str], Path | None],
    resolve_job_location_fn: Callable[
        [str | Path, str], JobLocationRecord | None
    ] = resolve_job_location,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None, JobLocationRecord | None]:
    resolved_record: JobLocationRecord | None = None
    for root in lookup_roots_for_target(cfg, target, engine=engine):
        record = resolve_job_location_fn(root, target)
        job_dir = resolve_latest_job_dir_fn(root, target)
        if job_dir is None:
            continue
        resolved_record = record
        report = load_report_json_fn(job_dir) if load_report_json_fn is not None else None
        return job_dir, load_state_fn(job_dir), report, resolved_record
    return None, None, None, resolved_record


__all__ = [
    "append_unique_root",
    "index_root_for_cfg",
    "index_root_for_path",
    "list_job_records_for_cfg",
    "load_job_artifacts",
    "load_job_artifacts_for_cfg",
    "lookup_roots_for_target",
    "normalize_identifier",
    "normalize_text",
    "resolve_job_location_for_cfg",
    "resolve_latest_job_dir",
    "runtime_roots_for_cfg",
]
