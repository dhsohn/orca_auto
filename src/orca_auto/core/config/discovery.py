"""Shared configuration discovery for standalone ORCA commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.utils.coercion import normalize_text

from .files import discover_shared_config_path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def repo_root_for_subprocess() -> str | None:
    root = repo_root()
    if (root / "src" / "orca_auto").is_dir():
        return str(root)
    return None


def resolve_shared_config_path(explicit: str | None) -> str | None:
    return discover_shared_config_path(explicit, repo_root(), env_var=ORCA_AUTO_CONFIG_ENV_VAR)


def shared_config_text_from_args(args: Any) -> str:
    return normalize_text(getattr(args, "orca_auto_config", None)) or normalize_text(
        getattr(args, "config", None)
    )


def engine_config_for_args(args: Any) -> str | None:
    config_path = resolve_shared_config_path(shared_config_text_from_args(args))
    if not config_path:
        return None
    return str(Path(config_path).expanduser().resolve())


__all__ = [
    "engine_config_for_args",
    "repo_root",
    "repo_root_for_subprocess",
    "resolve_shared_config_path",
    "shared_config_text_from_args",
]
