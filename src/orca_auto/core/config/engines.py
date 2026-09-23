from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orca_auto.core.app_ids import ORCA_AUTO_CONFIG_ENV_VAR
from orca_auto.core.paths.validation import validated_absolute_linux_path_text
from orca_auto.core.utils.coercion import positive_int

from .discovery import repo_root
from .files import (
    default_config_path_from_repo_root,
)
from .schema import (
    CommonResourceConfig,
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

CONFIG_ENV_VAR = ORCA_AUTO_CONFIG_ENV_VAR


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
    return default_config_path_from_repo_root(repo_root(), env_var=CONFIG_ENV_VAR)


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


def scheduler_runtime_settings(
    scheduler_raw: dict[str, Any],
    *,
    default_max_active: int,
    default_admission_root: str,
    admission_limit_enabled: bool,
) -> SchedulerRuntimeSettings:
    if "max_active_simulations" in scheduler_raw:
        raw_max_active = explicit_positive_int(
            scheduler_raw.get("max_active_simulations"),
            field_name="scheduler.max_active_simulations",
        )
    else:
        raw_max_active = default_max_active
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
