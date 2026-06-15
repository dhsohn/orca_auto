from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict

from orca_auto.core.config import CommonResourceConfig, TelegramConfig
from orca_auto.core.config import engines as _config_engines
from orca_auto.core.config.files import (
    default_shared_admission_root,
    engine_config_mapping,
    load_required_yaml_mapping,
    workflow_root_from_mapping,
)
from orca_auto.core.config.schema import (
    RetryRuntimeConfig,
    default_sibling_organized_root,
    telegram_config_from_mapping,
)

from .config_validation import _validate_config

logger = logging.getLogger(__name__)

_CONFIG_TEMPLATE_RELATIVE_PATH = Path("config") / "orca_auto.yaml.example"
_TEMPLATE_ALLOWED_ROOT = "/path/to/orca_runs"
_TEMPLATE_ORGANIZED_ROOT = "/path/to/orca_outputs"
_TEMPLATE_ORCA_EXECUTABLE = "/path/to/orca/orca"


def _config_template_path() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / _CONFIG_TEMPLATE_RELATIVE_PATH


def _default_organized_root(allowed_root: str) -> str:
    return default_sibling_organized_root(allowed_root, "orca_outputs")


def _missing_config_error(path: Path) -> ValueError:
    template_path = _config_template_path()
    return ValueError(
        "Config file not found: "
        f"{path}. Copy {template_path} to {path} and set explicit Linux paths for "
        "orca.runtime.allowed_root, orca.runtime.organized_root, and "
        "orca.paths.orca_executable."
    )


def _missing_required_settings_error(path: Path, missing_keys: list[str]) -> ValueError:
    keys = ", ".join(missing_keys)
    return ValueError(
        "Config is missing required settings: "
        f"{keys}. orca_auto no longer assumes personal defaults like ~/orca_runs or "
        f"~/opt/orca/orca. Update {path} with explicit Linux paths."
    )


def _placeholder_settings_error(path: Path, placeholder_keys: list[str]) -> ValueError:
    keys = ", ".join(placeholder_keys)
    return ValueError(
        "Config still contains template placeholder paths in "
        f"{keys}. Edit {path} and replace /path/to/... values with your real Linux paths."
    )


@dataclass
class CommonRuntimeConfig(RetryRuntimeConfig):
    # max retry count, not total execution count
    default_organized_root_name: ClassVar[str] = "orca_outputs"


RuntimeConfig = CommonRuntimeConfig


@dataclass
class PathsConfig:
    orca_executable: str = ""


@dataclass
class BehaviorConfig:
    auto_organize_on_terminal: bool = False


@dataclass
class AppConfig:
    runtime: CommonRuntimeConfig = field(default_factory=CommonRuntimeConfig)
    workflow_root: str = ""
    paths: PathsConfig = field(default_factory=PathsConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    resources: CommonResourceConfig = field(default_factory=CommonResourceConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def _load_raw_config(path: Path) -> Dict[str, Any]:
    _, parsed = load_required_yaml_mapping(
        path,
        missing_error=_missing_config_error,
        invalid_message="Config file is invalid: {path}",
    )
    return parsed


def _section_mapping(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    section = raw.get(key, {})
    return section if isinstance(section, dict) else {}


def _required_runtime_paths(
    path: Path,
    runtime_raw: Dict[str, Any],
    paths_raw: Dict[str, Any],
) -> tuple[str, str]:
    allowed_root = _config_engines.as_nonempty_str(runtime_raw.get("allowed_root"), "")
    orca_executable = _config_engines.as_nonempty_str(paths_raw.get("orca_executable"), "")
    missing_keys: list[str] = []
    if not allowed_root:
        missing_keys.append("orca.runtime.allowed_root")
    if not orca_executable:
        missing_keys.append("orca.paths.orca_executable")
    if missing_keys:
        raise _missing_required_settings_error(path, missing_keys)
    return allowed_root, orca_executable


def _scheduler_runtime_settings(
    path: Path,
    scheduler_raw: Dict[str, Any],
    allowed_root: str,
) -> tuple[int, str, int | None]:
    scheduler_enabled = bool(scheduler_raw)
    settings = _config_engines.scheduler_runtime_settings(
        scheduler_raw,
        default_max_active=RuntimeConfig.max_concurrent,
        default_admission_root=default_shared_admission_root(path)
        if scheduler_enabled
        else allowed_root,
        admission_limit_enabled=scheduler_enabled,
        reject_nonpositive=True,
    )
    return settings.max_active, settings.admission_root, settings.admission_limit


def _placeholder_keys(cfg: AppConfig) -> list[str]:
    placeholder_keys: list[str] = []
    if cfg.runtime.allowed_root == _TEMPLATE_ALLOWED_ROOT:
        placeholder_keys.append("orca.runtime.allowed_root")
    if cfg.runtime.organized_root == _TEMPLATE_ORGANIZED_ROOT:
        placeholder_keys.append("orca.runtime.organized_root")
    if cfg.paths.orca_executable == _TEMPLATE_ORCA_EXECUTABLE:
        placeholder_keys.append("orca.paths.orca_executable")
    return placeholder_keys


def load_config(config_path: str) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    raw = _load_raw_config(path)
    workflow_root = _config_engines.as_nonempty_str(workflow_root_from_mapping(raw), "")
    raw = engine_config_mapping(
        raw, "orca", inherit_keys=("behavior", "resources", "telegram", "scheduler")
    )
    scheduler_raw = _section_mapping(raw, "scheduler")
    runtime_raw = _section_mapping(raw, "runtime")
    paths_raw = _section_mapping(raw, "paths")
    behavior_raw = _section_mapping(raw, "behavior")
    telegram_raw = _section_mapping(raw, "telegram")
    resources_raw = _section_mapping(raw, "resources")

    allowed_root, orca_executable = _required_runtime_paths(path, runtime_raw, paths_raw)
    organized_root = _config_engines.as_nonempty_str(
        runtime_raw.get("organized_root"),
        _default_organized_root(allowed_root),
    )
    default_max_retries = _config_engines.as_int(
        runtime_raw.get("default_max_retries"),
        RuntimeConfig.default_max_retries,
    )
    max_concurrent, admission_root, admission_limit = _scheduler_runtime_settings(
        path,
        scheduler_raw,
        allowed_root,
    )
    telegram_cfg = telegram_config_from_mapping(telegram_raw)

    cfg = AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=allowed_root,
            organized_root=organized_root,
            default_max_retries=max(0, default_max_retries),
            max_concurrent=max_concurrent,
            admission_root=admission_root,
            admission_limit=admission_limit,
        ),
        workflow_root=workflow_root,
        paths=PathsConfig(
            orca_executable=orca_executable,
        ),
        behavior=BehaviorConfig(
            auto_organize_on_terminal=_config_engines.as_bool(
                behavior_raw.get("auto_organize_on_terminal"),
                False,
            ),
        ),
        resources=_config_engines.resource_config_from_mapping(resources_raw),
        telegram=telegram_cfg,
    )
    placeholder_keys = _placeholder_keys(cfg)
    if placeholder_keys:
        raise _placeholder_settings_error(path, placeholder_keys)

    _validate_config(cfg)

    logger.info(
        "Config loaded: allowed_root=%s, organized_root=%s, admission_root=%s, orca_executable=%s, max_concurrent=%d, admission_limit=%d",
        cfg.runtime.allowed_root,
        cfg.runtime.organized_root,
        cfg.runtime.resolved_admission_root,
        cfg.paths.orca_executable,
        cfg.runtime.max_concurrent,
        cfg.runtime.resolved_admission_limit,
    )
    return cfg
