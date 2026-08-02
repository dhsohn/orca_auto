from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.config.files import default_config_path_from_repo_root

logger = logging.getLogger(__name__)

CONFIG_ENV_VAR = "ORCA_AUTO_CONFIG"
_MAX_SAMPLE_FILES = 10


def default_config_path() -> str:
    repo_root = Path(__file__).resolve().parents[4]
    return default_config_path_from_repo_root(repo_root, env_var=CONFIG_ENV_VAR)


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
