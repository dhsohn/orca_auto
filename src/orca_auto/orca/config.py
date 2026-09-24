from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from orca_auto.core.config import (
    CommonResourceConfig,
    MessengerConfig,
    ScratchConfig,
)
from orca_auto.core.config.files import (
    SharedConfig,
    default_shared_admission_root,
    load_shared_config,
    validated_runs_root_text,
)
from orca_auto.core.config.schema import OrcaRuntimeConfig

from .config_validation import _validate_config

logger = logging.getLogger(__name__)

_TEMPLATE_ALLOWED_ROOT = "/path/to/orca_runs"
_TEMPLATE_ORCA_EXECUTABLE = "/path/to/orca/orca"


def _missing_config_error(path: Path) -> ValueError:
    # The package may be a wheel or prepared runtime, so no checkout-relative
    # template path can be promised here.
    return ValueError(
        "Config file not found: "
        f"{path}. Run `orca_auto init --config {path}` or copy "
        f"config/orca_auto.yaml.example from the ORCA_auto source tree to {path} and set "
        "explicit Linux paths for runs_root and orca.paths.orca_executable."
    )


def _missing_required_settings_error(path: Path, missing_keys: list[str]) -> ValueError:
    keys = ", ".join(missing_keys)
    return ValueError(
        "Config is missing required settings: "
        f"{keys}. Update {path} with explicit Linux paths for run roots and the "
        "ORCA executable."
    )


def _placeholder_settings_error(path: Path, placeholder_keys: list[str]) -> ValueError:
    keys = ", ".join(placeholder_keys)
    return ValueError(
        "Config still contains template placeholder paths in "
        f"{keys}. Edit {path} and replace /path/to/... values with your real Linux paths."
    )


@dataclass
class PathsConfig:
    orca_executable: str = ""


@dataclass
class AppConfig:
    runtime: OrcaRuntimeConfig = field(default_factory=OrcaRuntimeConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    resources: CommonResourceConfig = field(default_factory=CommonResourceConfig)
    scratch: ScratchConfig = field(default_factory=ScratchConfig)
    messenger: MessengerConfig = field(default_factory=MessengerConfig)


def _require_configured_paths(path: Path, shared: SharedConfig) -> None:
    missing_keys: list[str] = []
    if not shared.runs_root:
        missing_keys.append("runs_root")
    if not shared.orca_executable:
        missing_keys.append("orca.paths.orca_executable")
    if missing_keys:
        raise _missing_required_settings_error(path, missing_keys)


def _placeholder_keys(cfg: AppConfig) -> list[str]:
    placeholder_keys: list[str] = []
    if cfg.runtime.allowed_root == _TEMPLATE_ALLOWED_ROOT:
        placeholder_keys.append("runs_root")
    if cfg.paths.orca_executable == _TEMPLATE_ORCA_EXECUTABLE:
        placeholder_keys.append("orca.paths.orca_executable")
    return placeholder_keys


def load_config(config_path: str) -> AppConfig:
    """Load the worker configuration: one shared validation pass plus path checks."""

    path, shared = load_shared_config(
        config_path,
        missing_error=_missing_config_error,
        invalid_message="Config file is invalid: {path}",
    )
    runs_root = validated_runs_root_text(shared.runs_root) if shared.runs_root else ""
    _require_configured_paths(path, shared)

    cfg = AppConfig(
        runtime=OrcaRuntimeConfig(
            allowed_root=runs_root,
            max_concurrent=shared.scheduler.max_active_simulations,
            admission_root=shared.scheduler.admission_root
            or default_shared_admission_root(runs_root),
            admission_limit=shared.scheduler.admission_limit,
        ),
        paths=PathsConfig(orca_executable=shared.orca_executable),
        resources=shared.resources,
        scratch=shared.scratch,
        messenger=shared.messenger,
    )
    placeholder_keys = _placeholder_keys(cfg)
    if placeholder_keys:
        raise _placeholder_settings_error(path, placeholder_keys)

    _validate_config(cfg)

    logger.info(
        "Config loaded: allowed_root=%s, admission_root=%s, orca_executable=%s, max_concurrent=%d, admission_limit=%d",
        cfg.runtime.allowed_root,
        cfg.runtime.resolved_admission_root,
        cfg.paths.orca_executable,
        cfg.runtime.max_concurrent,
        cfg.runtime.resolved_admission_limit,
    )
    return cfg
