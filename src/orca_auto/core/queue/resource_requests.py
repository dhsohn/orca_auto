from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .metadata import entry_metadata_value


def engine_resource_caps(
    cfg: Any,
    *,
    resource_dict_fn: Callable[[Any, Any], dict[str, int]],
) -> dict[str, int]:
    return resource_dict_fn(
        cfg.resources.max_cores_per_task,
        cfg.resources.max_memory_gb_per_task,
    )


def coerce_resource_request(value: Any) -> dict[str, int]:
    from orca_auto.core.config import engines as _config_engines

    return _config_engines.positive_int_mapping(value)


def entry_resource_request(
    cfg: Any,
    entry: Any,
    *,
    resource_caps_fn: Callable[[Any], dict[str, int]],
) -> dict[str, int]:
    return coerce_resource_request(
        entry_metadata_value(entry, "resource_request")
    ) or resource_caps_fn(cfg)


__all__ = ["coerce_resource_request", "engine_resource_caps", "entry_resource_request"]
