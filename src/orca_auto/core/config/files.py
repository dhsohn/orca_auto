from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.paths import is_rejected_windows_path
from orca_auto.core.utils.coercion import normalize_text

ORCA_AUTO_CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"
DEFAULT_CONFIG_FILENAME = "orca_auto.yaml"
DEFAULT_SHARED_ADMISSION_DIRNAME = ".admission"
SECURE_CONFIG_FILE_MODE = 0o600
YAML_CONFIG_LOAD_EXCEPTIONS = (OSError, ValueError, yaml.YAMLError)


def config_env_value(env_var: str = ORCA_AUTO_CONFIG_ENV_VAR) -> str:
    return os.getenv(env_var, "").strip()


def secure_config_file_permissions(
    config_path: str | Path,
    *,
    mode: int = SECURE_CONFIG_FILE_MODE,
) -> None:
    Path(config_path).chmod(mode)


def default_config_path_from_repo_root(
    repo_root: Path,
    *,
    env_var: str = ORCA_AUTO_CONFIG_ENV_VAR,
) -> str:
    env_path = config_env_value(env_var)
    if env_path:
        return env_path

    repo_default = repo_root / "config" / DEFAULT_CONFIG_FILENAME
    if repo_default.exists():
        return str(repo_default)

    home_default = Path.home() / "orca_auto" / "config" / DEFAULT_CONFIG_FILENAME
    if home_default.exists():
        return str(home_default)

    return str(repo_default)


def discover_shared_config_path(
    explicit: str | Path | None,
    repo_root: Path,
    *,
    env_var: str = ORCA_AUTO_CONFIG_ENV_VAR,
) -> str | None:
    explicit_text = str(explicit or "").strip()
    if explicit_text:
        return str(Path(explicit_text).expanduser().resolve())

    discovered = default_config_path_from_repo_root(repo_root, env_var=env_var)
    if config_env_value(env_var):
        return str(Path(discovered).expanduser().resolve())

    path = Path(discovered).expanduser().resolve()
    return str(path) if path.exists() else None


def load_yaml_mapping(
    config_path: str | Path,
    *,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    path = Path(config_path).expanduser().resolve()
    with path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    if not isinstance(parsed, dict):
        raise ValueError(invalid_message.format(path=path))
    return path, parsed


def load_required_yaml_mapping(
    config_path: str | Path,
    *,
    missing_error: Callable[[Path], Exception] | None = None,
    invalid_message: str = "YAML top-level is not a mapping: {path}",
) -> tuple[Path, dict[str, Any]]:
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        if missing_error is not None:
            raise missing_error(path)
        raise FileNotFoundError(path)
    return load_yaml_mapping(path, invalid_message=invalid_message)


def mapping_section(raw: dict[str, Any] | None, key: str) -> dict[str, Any]:
    section = raw.get(key) if isinstance(raw, dict) else None
    return section if isinstance(section, dict) else {}


def resolve_configured_path(value: Any) -> Path | None:
    text = normalize_text(value)
    return Path(text).expanduser().resolve() if text else None


def engine_config_mapping(
    raw: dict[str, Any],
    engine: str,
    *,
    inherit_keys: Iterable[str] = ("behavior", "resources", "messenger"),
) -> dict[str, Any]:
    section = raw.get(engine)
    if not isinstance(section, dict):
        return {}

    resolved = dict(section)
    for key in inherit_keys:
        inherited = raw.get(key)
        override = resolved.get(key)
        if key == "scheduler" and key in resolved and not isinstance(override, dict):
            raise ValueError(f"{engine}.scheduler must be a mapping when configured.")
        if key == "scheduler" and isinstance(override, dict):
            mismatched = sorted(
                override_key
                for override_key, override_value in override.items()
                if not isinstance(inherited, dict)
                or override_key not in inherited
                or inherited[override_key] != override_value
            )
            if mismatched:
                joined = ", ".join(mismatched)
                raise ValueError(
                    f"{engine}.scheduler cannot override the shared top-level scheduler "
                    f"({joined}); configure machine-wide admission under scheduler instead."
                )
        if not isinstance(inherited, dict):
            continue
        if key not in resolved:
            resolved[key] = dict(inherited)
        elif key == "scheduler" and isinstance(override, dict):
            # Preserve the shared limit when an engine redundantly repeats only
            # one scheduler key. Divergent values were rejected above.
            resolved[key] = {**inherited, **override}
    return resolved


def default_shared_admission_root(runs_root: str | Path | None) -> str:
    """Default shared admission directory: hidden under the single runs root."""
    text = normalize_text(runs_root)
    if not text:
        return ""
    return str(Path(text).expanduser().resolve() / DEFAULT_SHARED_ADMISSION_DIRNAME)


def scheduler_admission_root(
    scheduler: dict[str, Any] | None,
    *,
    default_runs_root: str | Path | None = None,
) -> Path | None:
    scheduler_raw = scheduler if isinstance(scheduler, dict) else {}
    admission_root = resolve_configured_path(scheduler_raw.get("admission_root"))
    if admission_root is None:
        admission_root = resolve_configured_path(default_shared_admission_root(default_runs_root))
    return admission_root


def runs_root_from_mapping(raw: dict[str, Any] | None) -> str:
    """Read the shared runs root from the top-level runs_root key.

    Returns the configured text as-is (no resolution) so callers can validate
    the raw value before resolving it.
    """
    return normalize_text((raw.get("runs_root") or "") if isinstance(raw, dict) else "")


def validated_runs_root_text(root_text: str) -> str:
    """Reject Windows-style and non-absolute runs_root values before resolution.

    Resolving first would silently anchor a bad value on the worker cwd, so
    every runs_root consumer must validate the raw text through this helper.
    """
    if is_rejected_windows_path(root_text):
        raise ValueError(
            f"runs_root must be a Linux path (Windows paths are not supported): {root_text!r}"
        )
    if not Path(root_text).is_absolute():
        raise ValueError(f"runs_root must be an absolute Linux path: {root_text!r}")
    return root_text


def usable_runs_root_from_mapping(raw: dict[str, Any] | None) -> str:
    """runs_root text when present and valid, else "".

    For soft consumers (discovery, capacity preflight, systemd rendering) that
    must ignore an invalid root rather than raise: an unvalidated resolve would
    silently anchor the value on the caller cwd.
    """
    root_text = runs_root_from_mapping(raw)
    if not root_text:
        return ""
    try:
        return validated_runs_root_text(root_text)
    except ValueError:
        return ""


def shared_workflow_root_from_config(config_path: str | Path | None) -> str | None:
    if config_path is None:
        return None

    try:
        path = Path(config_path).expanduser().resolve()
    except OSError:
        return None
    if not path.exists():
        return None

    try:
        _, parsed = load_yaml_mapping(path)
    except YAML_CONFIG_LOAD_EXCEPTIONS:
        return None

    root_text = usable_runs_root_from_mapping(parsed)
    if not root_text:
        return None
    return str(Path(root_text).expanduser().resolve())
