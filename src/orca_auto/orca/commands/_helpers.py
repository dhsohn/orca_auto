from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import default_config_path_from_repo_root

from ..config import AppConfig

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"
_MAX_SAMPLE_FILES = 10


def default_config_path() -> str:
    repo_root = Path(__file__).resolve().parents[4]
    return default_config_path_from_repo_root(repo_root, env_var=CONFIG_ENV_VAR)


def _to_resolved_local(path_text: str) -> Path:
    return Path(path_text).expanduser().resolve()


def _validate_root_scan_dir(cfg: AppConfig, root_raw: str) -> Path:
    root = _to_resolved_local(root_raw)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Root directory not found: {root}")

    allowed_root = _to_resolved_local(cfg.runtime.allowed_root)
    if root != allowed_root:
        raise ValueError(f"--root must exactly match runs_root: {allowed_root}. got={root}")
    return root


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def finalize_batch_apply(
    summary: dict[str, Any],
    emit_fn: Callable[[dict[str, Any]], None],
    failures: list[dict[str, Any]],
) -> int:
    emit_fn(summary)
    return 1 if failures else 0
