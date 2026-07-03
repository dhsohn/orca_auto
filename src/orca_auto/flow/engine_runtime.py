from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.config.files import (
    engine_config_mapping,
    load_yaml_mapping,
    mapping_section,
    resolve_configured_path,
    scheduler_admission_root,
    workflow_root_from_mapping,
)


def _load_engine_config(config_path: str) -> tuple[Path, dict[str, Any]]:
    return load_yaml_mapping(
        config_path,
        invalid_message="Invalid engine config file: {path}",
    )


def _runtime_section_label(engine: str | None) -> str:
    return f"{engine}.runtime" if engine else "runtime"


def _runtime_allowed_root_label(engine: str | None) -> str:
    return f"{_runtime_section_label(engine)}.allowed_root"


def _internal_engine_runtime_paths(path: Path, raw: dict[str, Any]) -> dict[str, Path]:
    workflow_root = workflow_root_from_mapping(raw)
    if not workflow_root:
        raise ValueError(
            f"Missing runs root (workflow.root or orca.runtime.allowed_root) in config: {path}"
        )
    resolved_workflow_root = Path(workflow_root).expanduser().resolve()
    resolved = {
        "workflow_root": resolved_workflow_root,
        "allowed_root": resolved_workflow_root,
    }
    admission_root = scheduler_admission_root(
        mapping_section(raw, "scheduler"),
        default_runs_root=resolved_workflow_root,
    )
    if admission_root is not None:
        resolved["admission_root"] = admission_root
    return resolved


def _configured_runtime_paths(
    path: Path, raw: dict[str, Any], *, engine: str | None = None
) -> dict[str, Path]:
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError(f"Missing {_runtime_section_label(engine)} section in config: {path}")
    scheduler = mapping_section(raw, "scheduler")

    resolved_runtime_paths: dict[str, Path] = {}
    resolved_path = resolve_configured_path(runtime.get("allowed_root"))
    if resolved_path is not None:
        resolved_runtime_paths["allowed_root"] = resolved_path

    if "allowed_root" not in resolved_runtime_paths:
        raise ValueError(f"Missing {_runtime_allowed_root_label(engine)} in config: {path}")

    admission_root = scheduler_admission_root(
        scheduler,
        default_runs_root=resolved_runtime_paths["allowed_root"],
    )
    if admission_root is not None:
        resolved_runtime_paths["admission_root"] = admission_root
    return resolved_runtime_paths


def engine_runtime_paths(config_path: str, *, engine: str | None = None) -> dict[str, Path]:
    path, raw = _load_engine_config(config_path)
    if engine in {"xtb", "crest"}:
        return _internal_engine_runtime_paths(path, raw)

    if engine:
        raw = engine_config_mapping(raw, engine, inherit_keys=("scheduler", "workflow"))
    return _configured_runtime_paths(path, raw, engine=engine)


__all__ = [
    "engine_runtime_paths",
]
