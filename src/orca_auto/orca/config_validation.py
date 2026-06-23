"""Configuration validation and normalization helpers.

Extracted from config.py to reduce its size while keeping dataclasses
and ``load_config()`` in the main module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.paths import (
    is_rejected_windows_path,
    is_subpath,
    validate_configured_executable_path,
)


def _validate_config(cfg: Any) -> None:
    """Validate core path constraints on an AppConfig instance."""
    for label, path_val in [
        ("allowed_root", cfg.runtime.allowed_root),
        ("organized_root", cfg.runtime.organized_root),
        (
            "admission_root",
            getattr(cfg.runtime, "resolved_admission_root", cfg.runtime.admission_root),
        ),
        ("orca_executable", cfg.paths.orca_executable),
    ]:
        if is_rejected_windows_path(path_val):
            raise ValueError(
                f"{label} must be a Linux path (Windows paths are not supported): {path_val!r}"
            )
        if not Path(path_val).is_absolute():
            raise ValueError(f"{label} must be an absolute Linux path: {path_val!r}")
    validate_configured_executable_path(
        cfg.paths.orca_executable,
        label="orca_executable",
        display_name="ORCA",
    )

    allowed_root = Path(cfg.runtime.allowed_root)
    if not allowed_root.exists():
        raise ValueError(
            f"allowed_root directory not found: {cfg.runtime.allowed_root!r}. "
            "Create the directory or update the config."
        )
    if not allowed_root.is_dir():
        raise ValueError(f"allowed_root is not a directory: {cfg.runtime.allowed_root!r}")

    ar = Path(cfg.runtime.allowed_root).resolve()
    org = Path(cfg.runtime.organized_root).resolve()
    if is_subpath(org, ar) or is_subpath(ar, org):
        raise ValueError(
            f"organized_root and allowed_root must not contain each other: "
            f"allowed_root={ar}, organized_root={org}"
        )
