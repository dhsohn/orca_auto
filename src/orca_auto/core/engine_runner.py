from __future__ import annotations

import shutil
from typing import Any

from orca_auto.core.config import engines as _config_engines
from orca_auto.core.paths import (
    validate_configured_executable_path,
    validate_executable_file,
)


def resolve_configured_executable(
    cfg: Any,
    *,
    path_attr: str,
    executable_name: str,
    display_name: str,
) -> str:
    configured = str(getattr(cfg.paths, path_attr, "")).strip()
    if configured:
        path = validate_configured_executable_path(
            configured,
            label=f"Configured {display_name} executable",
            display_name=display_name,
        )
        return str(path)

    discovered = shutil.which(executable_name)
    if discovered:
        return discovered
    raise ValueError(f"{display_name} executable not configured and not found on PATH.")


def resource_actual_dict(resource_request: dict[str, int]) -> dict[str, int]:
    return _config_engines.resource_actual_from_request(resource_request)


def bool_flag(manifest: dict[str, Any], key: str) -> bool:
    return _config_engines.as_bool(manifest.get(key), False)


def manifest_int(
    manifest: dict[str, Any],
    key: str,
    *,
    zero_is_absent: bool = False,
) -> int | None:
    value = manifest.get(key)
    absent_values = (None, "", 0, "0") if zero_is_absent else (None, "")
    if value in absent_values:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or (zero_is_absent and stripped == "0"):
            return None
        return int(stripped)
    if isinstance(value, (int, float)):
        return int(value)
    raise ValueError(f"Manifest field {key!r} must be an integer-compatible value.")


def manifest_scalar_text(manifest: dict[str, Any], key: str) -> str | None:
    value = manifest.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, bool):
        return "true" if value else None
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text or None


def append_solvent_option(command: list[str], manifest: dict[str, Any]) -> None:
    solvent_model = str(manifest.get("solvent_model", "")).strip().lower()
    solvent = str(manifest.get("solvent", "")).strip()
    if solvent and solvent_model in {"gbsa", "alpb"}:
        command.extend([f"--{solvent_model}", solvent])


__all__ = [
    "append_solvent_option",
    "bool_flag",
    "manifest_int",
    "manifest_scalar_text",
    "resolve_configured_executable",
    "resource_actual_dict",
    "validate_executable_file",
]
