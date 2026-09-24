from __future__ import annotations

from pathlib import Path

from orca_auto.core.config.files import resolved_admission_root, validated_runs_root_text

from .config import load_orca_shared_config


def engine_runtime_paths(config_path: str) -> dict[str, Path]:
    """Resolve standalone ORCA roots without validating the engine executable.

    ``scheduler`` is a top-level-only section: ``load_orca_shared_config``
    rejects ``orca.scheduler`` before this function sees the settings.
    """
    path, shared, _orca_sections = load_orca_shared_config(
        config_path,
        invalid_message="Invalid engine config file: {path}",
    )
    if not shared.runs_root:
        raise ValueError(f"Missing runs_root in config: {path}")

    resolved_root = Path(validated_runs_root_text(shared.runs_root)).expanduser().resolve()
    resolved: dict[str, Path] = {
        "allowed_root": resolved_root,
    }
    admission_root = resolved_admission_root(shared.scheduler, runs_root=resolved_root)
    if admission_root is not None:
        resolved["admission_root"] = admission_root
    return resolved


__all__ = [
    "engine_runtime_paths",
]
