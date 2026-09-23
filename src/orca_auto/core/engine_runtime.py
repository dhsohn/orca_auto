from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.config.files import (
    engine_config_mapping,
    load_shared_config_mapping,
    mapping_section,
    runs_root_from_mapping,
    scheduler_admission_root,
    validated_runs_root_text,
)
from orca_auto.core.engine_catalog import get_engine_catalog_entry


def _load_engine_config(config_path: str) -> tuple[Path, dict[str, Any]]:
    return load_shared_config_mapping(
        config_path,
        invalid_message="Invalid engine config file: {path}",
    )


def engine_runtime_paths(config_path: str, *, engine: str | None = None) -> dict[str, Path]:
    """Resolve standalone ORCA roots without validating the engine executable."""
    path, raw = _load_engine_config(config_path)
    runs_root = runs_root_from_mapping(raw)
    if not runs_root:
        raise ValueError(f"Missing runs_root in config: {path}")

    resolved_root = Path(validated_runs_root_text(runs_root)).expanduser().resolve()
    resolved: dict[str, Path] = {
        "allowed_root": resolved_root,
    }
    scheduler_raw = mapping_section(raw, "scheduler")
    if engine:
        get_engine_catalog_entry(engine)
        engine_raw = engine_config_mapping(raw, engine, inherit_keys=("scheduler",))
        scheduler_raw = mapping_section(engine_raw, "scheduler") or scheduler_raw
    admission_root = scheduler_admission_root(
        scheduler_raw,
        default_runs_root=resolved_root,
    )
    if admission_root is not None:
        resolved["admission_root"] = admission_root
    return resolved


__all__ = [
    "engine_runtime_paths",
]
