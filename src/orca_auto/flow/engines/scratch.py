from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.engine_scratch import (
    EngineScratchPolicy,
    EngineScratchWorkspace,
    ScratchPublication,
)


def create_engine_scratch_workspace(
    cfg: Any,
    *,
    job_dir: Path,
    manifest_filename: str,
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
        job_dir / manifest_filename,
    )


def publish_engine_scratch_workspace(
    workspace: EngineScratchWorkspace,
    *,
    logger: logging.Logger,
) -> ScratchPublication:
    try:
        publication = workspace.publish()
    except BaseException:
        workspace.close()
        raise
    try:
        workspace.cleanup()
    except BaseException:  # noqa: BLE001
        logger.exception(
            "Published engine scratch workspace could not be removed; future scratch runs "
            "will remain fail-closed until it is inspected: %s",
            workspace.path,
        )
    finally:
        workspace.close()
    return publication


__all__ = ["create_engine_scratch_workspace", "publish_engine_scratch_workspace"]
