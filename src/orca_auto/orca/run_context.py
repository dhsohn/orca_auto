from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.orca.config import AppConfig


@dataclass(frozen=True)
class RunExecutionContext:
    cfg: AppConfig
    reaction_dir: Path
    selected_inp: Path
    admission_root: Path
    reservation_token: str | None = None
    admission_app_name: str | None = None
    admission_task_id: str | None = None
    execution_provenance: dict[str, Any] | None = None
    queue_id: str | None = None
    queue_generation: str | None = None


__all__ = ["RunExecutionContext"]
