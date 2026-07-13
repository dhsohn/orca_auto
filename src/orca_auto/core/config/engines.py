from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from orca_auto.core.paths import validate_configured_executable_path

from .files import (
    ORCA_AUTO_CONFIG_ENV_VAR,
    default_config_path_from_repo_root,
    default_shared_admission_root,
    load_required_yaml_mapping,
    mapping_section,
    messenger_mapping_from_root,
    runs_root_from_mapping,
    validate_shared_config_sections,
    validated_absolute_linux_path_text,
    validated_runs_root_text,
)
from .schema import (
    CommonResourceConfig,
    CommonRuntimeConfig,
    EmptyBehaviorConfig,
    MessengerConfig,
    TelegramConfig,
    reconcile_legacy_telegram_alias,
)
from .schema import (
    as_bool as as_bool,
)
from .schema import (
    as_float as as_float,
)
from .schema import (
    as_int as as_int,
)
from .schema import (
    as_nonempty_str as as_nonempty_str,
)
from .schema import (
    as_str as as_str,
)
from .schema import (
    explicit_positive_int as explicit_positive_int,
)
from .schema import (
    messenger_config_from_mapping as messenger_config_from_mapping,
)
from .schema import (
    normalize_admission_limit as normalize_admission_limit,
)
from .schema import (
    normalize_default_max_retries as normalize_default_max_retries,
)
from .schema import (
    normalize_max_concurrent as normalize_max_concurrent,
)
from .schema import (
    positive_int as positive_int,
)
from .schema import (
    resolved_admission_limit as resolved_admission_limit,
)
from .schema import (
    telegram_config_from_mapping as telegram_config_from_mapping,
)

CONFIG_ENV_VAR = ORCA_AUTO_CONFIG_ENV_VAR
_AppConfigT = TypeVar("_AppConfigT")


@dataclass(frozen=True)
class WorkflowEnginePathsConfig:
    xtb_executable: str = ""
    crest_executable: str = ""


WorkflowEngineBehaviorConfig = EmptyBehaviorConfig


@dataclass(frozen=True)
class WorkflowEngineAppConfig:
    runtime: CommonRuntimeConfig
    workflow_root: str = ""
    paths: WorkflowEnginePathsConfig = field(default_factory=WorkflowEnginePathsConfig)
    behavior: WorkflowEngineBehaviorConfig = field(default_factory=WorkflowEngineBehaviorConfig)
    resources: CommonResourceConfig = field(default_factory=CommonResourceConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    messenger: MessengerConfig = field(default_factory=MessengerConfig)

    def __post_init__(self) -> None:
        messenger, telegram = reconcile_legacy_telegram_alias(self.messenger, self.telegram)
        object.__setattr__(self, "messenger", messenger)
        object.__setattr__(self, "telegram", telegram)


@dataclass(frozen=True)
class SchedulerRuntimeSettings:
    max_active: int
    admission_root: str
    admission_limit: int | None


def positive_int_mapping(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        parsed = positive_int(value)
        if parsed is not None:
            result[key_text] = parsed
    return result


def default_shared_config_path() -> str:
    repo_root = Path(__file__).resolve().parents[4]
    return default_config_path_from_repo_root(repo_root, env_var=CONFIG_ENV_VAR)


def resource_request_from_manifest(cfg: Any, manifest: dict[str, Any]) -> dict[str, int]:
    resources = manifest.get("resources")
    if resources is not None and not isinstance(resources, dict):
        raise ValueError("Manifest field 'resources' must be a mapping.")
    resource_overrides = dict(resources or {})
    unknown_resource_keys = set(resource_overrides) - {
        "max_cores",
        "max_cores_per_task",
        "max_memory_gb",
        "max_memory_gb_per_task",
    }
    if unknown_resource_keys:
        raise ValueError(
            f"Unknown manifest resource fields: {sorted(str(key) for key in unknown_resource_keys)}"
        )

    def explicit_positive_value(*values: tuple[str, Any]) -> int | None:
        parsed_values: list[tuple[str, int]] = []
        for label, raw in values:
            if raw is None or raw == "":
                continue
            if isinstance(raw, bool):
                raise ValueError(f"Manifest resource {label!r} must be a positive integer.")
            if isinstance(raw, int):
                parsed = raw
            elif isinstance(raw, str) and raw.strip().isdigit():
                parsed = int(raw.strip())
            else:
                raise ValueError(f"Manifest resource {label!r} must be a positive integer.")
            if parsed < 1:
                raise ValueError(f"Manifest resource {label!r} must be a positive integer.")
            parsed_values.append((label, parsed))
        if not parsed_values:
            return None
        if len({value for _label, value in parsed_values}) != 1:
            labels = ", ".join(label for label, _value in parsed_values)
            raise ValueError(f"Conflicting manifest resource aliases: {labels}")
        return parsed_values[0][1]

    default_cores = max(1, int(cfg.resources.max_cores_per_task))
    default_memory = max(1, int(cfg.resources.max_memory_gb_per_task))
    max_cores = (
        explicit_positive_value(
            ("resources.max_cores", resource_overrides.get("max_cores")),
            ("resources.max_cores_per_task", resource_overrides.get("max_cores_per_task")),
            ("max_cores", manifest.get("max_cores")),
            ("max_cores_per_task", manifest.get("max_cores_per_task")),
        )
        or default_cores
    )
    max_memory_gb = (
        explicit_positive_value(
            ("resources.max_memory_gb", resource_overrides.get("max_memory_gb")),
            (
                "resources.max_memory_gb_per_task",
                resource_overrides.get("max_memory_gb_per_task"),
            ),
            ("max_memory_gb", manifest.get("max_memory_gb")),
            ("max_memory_gb_per_task", manifest.get("max_memory_gb_per_task")),
        )
        or default_memory
    )
    return {
        "max_cores": max_cores,
        "max_memory_gb": max_memory_gb,
    }


def resource_actual_from_request(resource_request: dict[str, int]) -> dict[str, int]:
    cores = max(1, int(resource_request.get("max_cores", 1)))
    memory_gb = max(1, int(resource_request.get("max_memory_gb", 1)))
    return {
        "assigned_cores": cores,
        "memory_limit_gb": memory_gb,
        "omp_num_threads": cores,
        "openblas_num_threads": cores,
        "mkl_num_threads": cores,
        "numexpr_num_threads": cores,
    }


def _load_config_mapping(path: Path) -> dict[str, Any]:
    _, raw = load_required_yaml_mapping(
        path,
        missing_error=lambda missing: ValueError(
            "Config file not found: "
            f"{missing}. Copy config/orca_auto.yaml.example to this path and edit the workflow section."
        ),
        invalid_message="Config file is invalid: {path}",
    )
    return raw


def scheduler_runtime_settings(
    scheduler_raw: dict[str, Any],
    *,
    default_max_active: int,
    default_admission_root: str,
    admission_limit_enabled: bool,
    reject_nonpositive: bool = False,
) -> SchedulerRuntimeSettings:
    if "max_active_simulations" in scheduler_raw:
        raw_max_active = explicit_positive_int(
            scheduler_raw.get("max_active_simulations"),
            field_name="scheduler.max_active_simulations",
        )
    else:
        raw_max_active = default_max_active
    if reject_nonpositive and raw_max_active < 1:
        raise ValueError("scheduler.max_active_simulations must be an integer >= 1.")
    max_active = max(1, raw_max_active)
    if "admission_root" in scheduler_raw:
        admission_root = validated_absolute_linux_path_text(
            as_str(scheduler_raw.get("admission_root")),
            field_name="scheduler.admission_root",
        )
    else:
        admission_root = default_admission_root
    return SchedulerRuntimeSettings(
        max_active=max_active,
        admission_root=admission_root,
        admission_limit=max_active if admission_limit_enabled else None,
    )


def _required_workflow_root(raw: dict[str, Any], path: Path) -> str:
    workflow_root = runs_root_from_mapping(raw)
    if not workflow_root:
        raise ValueError(f"Config is missing runs_root: {path}")
    return str(Path(validated_runs_root_text(workflow_root)).expanduser().resolve())


def _runtime_config_from_scheduler(
    scheduler_raw: dict[str, Any],
    workflow_root: str,
    *,
    engine_admission_limit: int | None = None,
) -> CommonRuntimeConfig:
    scheduler = scheduler_runtime_settings(
        scheduler_raw,
        default_max_active=4,
        default_admission_root=default_shared_admission_root(workflow_root),
        admission_limit_enabled=True,
    )
    return CommonRuntimeConfig(
        allowed_root=workflow_root,
        max_concurrent=scheduler.max_active,
        admission_root=scheduler.admission_root,
        admission_limit=scheduler.admission_limit,
        engine_admission_limit=engine_admission_limit,
    )


def resource_config_from_mapping(resources_raw: dict[str, Any]) -> CommonResourceConfig:
    def configured_positive_int(key: str, default: int) -> int:
        if key not in resources_raw:
            return default
        return explicit_positive_int(
            resources_raw.get(key),
            field_name=f"resources.{key}",
        )

    return CommonResourceConfig(
        max_cores_per_task=configured_positive_int("max_cores_per_task", 8),
        max_memory_gb_per_task=configured_positive_int("max_memory_gb_per_task", 32),
    )


def _resource_config(resources_raw: dict[str, Any]) -> CommonResourceConfig:
    return resource_config_from_mapping(resources_raw)


def _validate_workflow_engine_executable(
    value: str,
    *,
    executable_key: str,
    display_name: str,
) -> str:
    if not value:
        return ""
    return str(
        validate_configured_executable_path(
            value,
            label=f"workflow.paths.{executable_key}",
            display_name=display_name,
        )
    )


def load_workflow_engine_config(
    config_path: str | None,
    *,
    default_config_path_fn: Callable[[], str],
    executable_key: str,
    executable_display_name: str,
    additional_executables: tuple[tuple[str, str], ...] = (),
    paths_cls: Callable[..., Any],
    behavior_cls: Callable[..., Any],
    app_config_cls: Callable[..., _AppConfigT],
) -> _AppConfigT:
    path = Path(config_path or default_config_path_fn()).expanduser().resolve()
    raw = _load_config_mapping(path)
    validate_shared_config_sections(raw)

    scheduler_raw = mapping_section(raw, "scheduler")
    workflow_raw = mapping_section(raw, "workflow")
    workflow_paths_raw = mapping_section(workflow_raw, "paths")
    resources_raw = mapping_section(raw, "resources")
    messenger_raw = messenger_mapping_from_root(raw)
    workflow_root = _required_workflow_root(raw, path)
    executable_values = {
        executable_key: _validate_workflow_engine_executable(
            as_str(workflow_paths_raw.get(executable_key)),
            executable_key=executable_key,
            display_name=executable_display_name,
        )
    }
    for additional_key, additional_display_name in additional_executables:
        executable_values[additional_key] = _validate_workflow_engine_executable(
            as_str(workflow_paths_raw.get(additional_key)),
            executable_key=additional_key,
            display_name=additional_display_name,
        )
    messenger = messenger_config_from_mapping(messenger_raw)

    return app_config_cls(
        runtime=_runtime_config_from_scheduler(scheduler_raw, workflow_root),
        workflow_root=workflow_root,
        paths=paths_cls(**executable_values),
        behavior=behavior_cls(),
        resources=_resource_config(resources_raw),
        telegram=messenger.telegram,
        messenger=messenger,
    )


def load_xtb_config(config_path: str | None = None) -> WorkflowEngineAppConfig:
    return load_workflow_engine_config(
        config_path,
        default_config_path_fn=default_shared_config_path,
        executable_key="xtb_executable",
        executable_display_name="xTB",
        paths_cls=WorkflowEnginePathsConfig,
        behavior_cls=WorkflowEngineBehaviorConfig,
        app_config_cls=WorkflowEngineAppConfig,
    )


def load_xtb_md_config(config_path: str | None = None) -> WorkflowEngineAppConfig:
    path = Path(config_path or default_shared_config_path()).expanduser().resolve()
    raw = _load_config_mapping(path)
    validate_shared_config_sections(raw)

    scheduler_raw = mapping_section(raw, "scheduler")
    workflow_raw = mapping_section(raw, "workflow")
    workflow_paths_raw = mapping_section(workflow_raw, "paths")
    resources_raw = mapping_section(raw, "resources")
    messenger_raw = messenger_mapping_from_root(raw)
    runs_root = _required_workflow_root(raw, path)
    xtb_executable = _validate_workflow_engine_executable(
        as_str(workflow_paths_raw.get("xtb_executable")),
        executable_key="xtb_executable",
        display_name="xTB",
    )
    if "max_active_xtb_md" in scheduler_raw:
        engine_admission_limit = explicit_positive_int(
            scheduler_raw.get("max_active_xtb_md"),
            field_name="scheduler.max_active_xtb_md",
        )
    else:
        engine_admission_limit = 1
    messenger = messenger_config_from_mapping(messenger_raw)

    return WorkflowEngineAppConfig(
        runtime=_runtime_config_from_scheduler(
            scheduler_raw,
            runs_root,
            engine_admission_limit=engine_admission_limit,
        ),
        workflow_root="",
        paths=WorkflowEnginePathsConfig(xtb_executable=xtb_executable),
        behavior=WorkflowEngineBehaviorConfig(),
        resources=_resource_config(resources_raw),
        telegram=messenger.telegram,
        messenger=messenger,
    )


def load_crest_config(config_path: str | None = None) -> WorkflowEngineAppConfig:
    return load_workflow_engine_config(
        config_path,
        default_config_path_fn=default_shared_config_path,
        executable_key="crest_executable",
        executable_display_name="CREST",
        additional_executables=(("xtb_executable", "xTB"),),
        paths_cls=WorkflowEnginePathsConfig,
        behavior_cls=WorkflowEngineBehaviorConfig,
        app_config_cls=WorkflowEngineAppConfig,
    )
