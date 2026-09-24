"""Shared configuration discovery for standalone ORCA commands.

Order: explicit ``--config``, ``ORCA_AUTO_CONFIG``, then
``~/orca_auto/config/orca_auto.yaml``. No checkout-relative location is probed:
the package can live in a source tree, a wheel, or a prepared runtime, and only
the operator's home is the same in all three.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.utils.coercion import normalize_text

from .files import default_config_path, discover_shared_config_path


def default_shared_config_path() -> str:
    return default_config_path(env_var=ORCA_AUTO_CONFIG_ENV_VAR)


def resolve_shared_config_path(explicit: str | None) -> str | None:
    return discover_shared_config_path(explicit, env_var=ORCA_AUTO_CONFIG_ENV_VAR)


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
    "default_shared_config_path",
    "engine_config_for_args",
    "resolve_shared_config_path",
    "shared_config_text_from_args",
]
