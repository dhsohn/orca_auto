from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    WORKFLOW_STAGE_DIRNAME_ALIASES,
    WORKFLOW_STAGE_DIRNAMES,
    iter_workflow_runtime_workspaces,
    workflow_root_dir,
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths,
    workflow_workspace_internal_engine_paths_from_path,
)
from orca_auto.core.utils import (
    atomic_write_json,
    file_lock,
)
from orca_auto.core.utils import (
    normalize_text as _normalize_text,
)
from orca_auto.flow.contracts.workflow import coerce_workflow_plan_payload

WORKFLOW_LOCK_NAME = "workflow.lock"
WORKFLOW_CREATE_LOCK_NAME = ".workflow_create.lock"


def _workflow_parent_dir(path: Path) -> Path:
    if path.is_file() and path.name == WORKFLOW_FILE_NAME:
        return path.parent
    return path


def _is_under_workflow_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_workflow_workspace(*, target: str, workflow_root: str | Path | None = None) -> Path:
    raw_target = _normalize_text(target)
    if not raw_target:
        raise ValueError("workflow target is required")

    root = workflow_root_dir(workflow_root) if workflow_root is not None else None

    try:
        direct = Path(raw_target).expanduser().resolve()
    except OSError:
        direct = None
    if direct is not None and direct.exists():
        parent = _workflow_parent_dir(direct)
        if parent.is_dir() and (root is None or _is_under_workflow_root(parent, root)):
            return parent

    if root is None:
        raise FileNotFoundError(f"workflow not found: {target}")

    candidate = (root / raw_target).resolve()
    if _is_under_workflow_root(candidate, root) and candidate.exists():
        parent = _workflow_parent_dir(candidate)
        if parent.is_dir():
            return parent
    raise FileNotFoundError(f"workflow not found: {target}")


def workflow_file_path(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir).expanduser().resolve() / WORKFLOW_FILE_NAME


def workflow_lock_path(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir).expanduser().resolve() / WORKFLOW_LOCK_NAME


def workflow_create_lock_path(workflow_root: str | Path) -> Path:
    return workflow_root_dir(workflow_root) / WORKFLOW_CREATE_LOCK_NAME


@contextmanager
def acquire_workflow_lock(workspace_dir: str | Path, *, timeout_seconds: float = 10.0):
    with file_lock(workflow_lock_path(workspace_dir), timeout_seconds=timeout_seconds):
        yield


@contextmanager
def acquire_workflow_create_lock(workflow_root: str | Path, *, timeout_seconds: float = 10.0):
    with file_lock(workflow_create_lock_path(workflow_root), timeout_seconds=timeout_seconds):
        yield


def load_workflow_payload(workspace_dir: str | Path) -> dict[str, Any]:
    path = workflow_file_path(workspace_dir)
    if not path.exists():
        raise FileNotFoundError(f"workflow file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"workflow file is not a JSON object: {path}")
    return dict(coerce_workflow_plan_payload(raw))


def write_workflow_payload(workspace_dir: str | Path, payload: dict[str, Any]) -> Path:
    path = workflow_file_path(workspace_dir)
    atomic_write_json(path, payload, ensure_ascii=True, indent=2)
    return path


def iter_workflow_workspaces(workflow_root: str | Path) -> list[Path]:
    root = workflow_root_dir(workflow_root)
    if not root.exists():
        return []
    candidates = [
        item for item in root.iterdir() if item.is_dir() and (item / WORKFLOW_FILE_NAME).exists()
    ]
    return sorted(candidates, key=lambda item: item.name, reverse=True)


__all__ = [
    "WORKFLOW_FILE_NAME",
    "WORKFLOW_CREATE_LOCK_NAME",
    "WORKFLOW_STAGE_DIRNAME_ALIASES",
    "WORKFLOW_STAGE_DIRNAMES",
    "WORKFLOW_LOCK_NAME",
    "acquire_workflow_create_lock",
    "acquire_workflow_lock",
    "iter_workflow_runtime_workspaces",
    "iter_workflow_workspaces",
    "load_workflow_payload",
    "resolve_workflow_workspace",
    "workflow_create_lock_path",
    "workflow_file_path",
    "workflow_lock_path",
    "workflow_root_dir",
    "workflow_stage_dirnames_for_engine",
    "workflow_workspace_internal_engine_paths",
    "workflow_workspace_internal_engine_paths_from_path",
    "write_workflow_payload",
]
