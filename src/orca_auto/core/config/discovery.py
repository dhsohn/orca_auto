"""Shared config and workflow-root discovery for command adapters.

Every command surface — the top-level CLI, the workflow worker entrypoint and
the workflow ``run-dir`` option parser — resolves the shared config file and
the workflow root the same way. This module is the one owner of that
resolution so the domain packages never reach up into the CLI layer for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.utils.coercion import normalize_text

from .files import discover_shared_config_path, shared_workflow_root_from_config


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def repo_root_for_subprocess() -> str | None:
    root = repo_root()
    if (root / "src" / "orca_auto").is_dir():
        return str(root)
    return None


def resolve_shared_config_path(explicit: str | None) -> str | None:
    return discover_shared_config_path(explicit, repo_root(), env_var=ORCA_AUTO_CONFIG_ENV_VAR)


def resolve_workflow_root(explicit: str | Path | None) -> str | None:
    explicit_text = normalize_text(explicit)
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())
    return None


def shared_config_text_from_args(args: Any) -> str:
    return normalize_text(getattr(args, "orca_auto_config", None)) or normalize_text(
        getattr(args, "config", None)
    )


def workflow_root_for_args(args: Any, *, config_path: str | None = None) -> str | None:
    explicit_root = resolve_workflow_root(getattr(args, "workflow_root", None))
    if explicit_root:
        return explicit_root
    config_text = normalize_text(config_path) or resolve_shared_config_path(
        shared_config_text_from_args(args)
    )
    return shared_workflow_root_from_config(config_text)


def engine_config_for_args(args: Any) -> str | None:
    config_path = resolve_shared_config_path(shared_config_text_from_args(args))
    if not config_path:
        return None
    return str(Path(config_path).expanduser().resolve())


def shared_config_for_args(args: Any) -> str | None:
    explicit = normalize_text(getattr(args, "orca_auto_config", None))
    if explicit:
        return str(Path(explicit).expanduser().resolve())
    return resolve_shared_config_path(None)


__all__ = [
    "engine_config_for_args",
    "repo_root",
    "repo_root_for_subprocess",
    "resolve_shared_config_path",
    "resolve_workflow_root",
    "shared_config_for_args",
    "shared_config_text_from_args",
    "workflow_root_for_args",
]
