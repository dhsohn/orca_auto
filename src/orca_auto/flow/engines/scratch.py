from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.engine_scratch import (
    EngineScratchPolicy,
    EngineScratchWorkspace,
    publish_engine_scratch_workspace,
)


def create_engine_scratch_workspace(
    cfg: Any,
    *,
    job_dir: Path,
    manifest_path: Path,
    max_memory_gb: int,
    publish_name: Callable[[str], bool],
) -> EngineScratchWorkspace | None:
    scratch = getattr(cfg, "scratch", None)
    if scratch is None or not bool(getattr(scratch, "enabled", False)):
        return None
    return EngineScratchWorkspace.create(
        EngineScratchPolicy(
            root=Path(str(scratch.root)),
            min_free_bytes=int(scratch.min_free_gb) * 1024**3,
            max_task_memory_bytes=int(max_memory_gb) * 1024**3,
            publish_name=publish_name,
        ),
        manifest_path,
        durable_output_dir=job_dir,
    )


__all__ = ["create_engine_scratch_workspace", "publish_engine_scratch_workspace"]
