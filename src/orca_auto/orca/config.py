from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.config import (
    CommonResourceConfig,
    MessengerConfig,
)
from orca_auto.core.config.files import (
    SharedConfig,
    configured_mapping_section,
    default_shared_admission_root,
    load_shared_config,
    load_yaml_mapping,
    validate_optional_text_field,
    validate_shared_config_sections,
    validated_runs_root_text,
)
from orca_auto.core.config.schema import (
    OrcaRuntimeConfig,
    as_nonempty_str,
    reject_unknown_config_fields,
)

from .config_validation import _validate_config
from .scratch_config import ScratchConfig, scratch_config_from_runtime_mapping

logger = logging.getLogger(__name__)

_TEMPLATE_ALLOWED_ROOT = "/path/to/orca_runs"
_TEMPLATE_ORCA_EXECUTABLE = "/path/to/orca/orca"
_ORCA_CONFIG_FIELDS = frozenset({"paths", "runtime"})
_ORCA_RUNTIME_CONFIG_FIELDS = frozenset({"scratch_min_free_gb", "scratch_root"})
_ORCA_PATH_CONFIG_FIELDS = frozenset({"orca_executable"})
_INVALID_MAPPING_MESSAGE = "YAML top-level is not a mapping: {path}"


@dataclass(frozen=True)
class OrcaConfigSections:
    """Validated ``orca.paths`` and ``orca.runtime`` sections; defaults applied.

    ``orca_executable`` is the configured text ("" when omitted); its path rules
    are applied by ``load_config``.
    """

    orca_executable: str = ""
    scratch: ScratchConfig = field(default_factory=ScratchConfig)


def orca_sections_from_mapping(orca: Mapping[str, Any]) -> OrcaConfigSections:
    """Validate the ``orca`` engine section: only ``paths`` and ``runtime``.

    The ``resources``, ``scheduler`` and ``messenger`` sections are top-level
    only, so an ``orca.*`` copy is rejected as an unknown field.
    """

    reject_unknown_config_fields(orca, allowed=_ORCA_CONFIG_FIELDS, section="orca")
    orca_runtime = configured_mapping_section(orca, "runtime", field_name="orca.runtime")
    reject_unknown_config_fields(
        orca_runtime,
        allowed=_ORCA_RUNTIME_CONFIG_FIELDS,
        section="orca.runtime",
    )
    scratch = scratch_config_from_runtime_mapping(orca_runtime)
    orca_paths = configured_mapping_section(orca, "paths", field_name="orca.paths")
    reject_unknown_config_fields(
        orca_paths,
        allowed=_ORCA_PATH_CONFIG_FIELDS,
        section="orca.paths",
    )
    validate_optional_text_field(
        orca_paths,
        "orca_executable",
        field_name="orca.paths.orca_executable",
    )
    return OrcaConfigSections(
        orca_executable=as_nonempty_str(orca_paths.get("orca_executable"), ""),
        scratch=scratch,
    )


def validate_orca_shared_config(
    raw: Mapping[str, Any],
) -> tuple[SharedConfig, OrcaConfigSections]:
    """Validate the complete shared-config shape: top-level sections plus ``orca``."""

    shared = validate_shared_config_sections(raw)
    return shared, orca_sections_from_mapping(shared.engine_section)


def load_orca_shared_config(
    config_path: str | Path,
    *,
    missing_error: Callable[[Path], Exception] | None = None,
    invalid_message: str = _INVALID_MAPPING_MESSAGE,
) -> tuple[Path, SharedConfig, OrcaConfigSections]:
    """Require, load, and fully validate one shared ``orca_auto.yaml``."""

    path, shared = load_shared_config(
        config_path,
        missing_error=missing_error,
        invalid_message=invalid_message,
    )
    return path, shared, orca_sections_from_mapping(shared.engine_section)


def load_orca_shared_config_mapping(
    config_path: str | Path,
    *,
    invalid_message: str = _INVALID_MAPPING_MESSAGE,
) -> tuple[Path, dict[str, Any]]:
    """Load and fully validate one shared ``orca_auto.yaml``, returning the raw mapping."""

    path, raw = load_yaml_mapping(config_path, invalid_message=invalid_message)
    validate_orca_shared_config(raw)
    return path, raw


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


def _require_configured_paths(
    path: Path,
    shared: SharedConfig,
    orca_sections: OrcaConfigSections,
) -> None:
    missing_keys: list[str] = []
    if not shared.runs_root:
        missing_keys.append("runs_root")
    if not orca_sections.orca_executable:
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

    path, shared, orca_sections = load_orca_shared_config(
        config_path,
        missing_error=_missing_config_error,
        invalid_message="Config file is invalid: {path}",
    )
    runs_root = validated_runs_root_text(shared.runs_root) if shared.runs_root else ""
    _require_configured_paths(path, shared, orca_sections)

    cfg = AppConfig(
        runtime=OrcaRuntimeConfig(
            allowed_root=runs_root,
            max_concurrent=shared.scheduler.max_active_simulations,
            admission_root=shared.scheduler.admission_root
            or default_shared_admission_root(runs_root),
            admission_limit=shared.scheduler.admission_limit,
        ),
        paths=PathsConfig(orca_executable=orca_sections.orca_executable),
        resources=shared.resources,
        scratch=orca_sections.scratch,
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
