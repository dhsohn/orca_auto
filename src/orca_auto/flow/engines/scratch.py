from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.engine_scratch import (
    EngineScratchPolicy,
    EngineScratchWorkspace,
    publish_engine_scratch_workspace,
    scratch_publication_provenance,
)
from orca_auto.core.queue.engine.input_snapshot import verify_input_snapshots


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


def abort_launch_publication(
    scratch_workspace: EngineScratchWorkspace | None,
    on_launch_aborted: Callable[[], None] | None,
    *,
    logger: logging.Logger,
) -> None:
    """Publish any scratch workspace and signal the abort callback on failure."""

    try:
        if scratch_workspace is not None:
            publish_engine_scratch_workspace(scratch_workspace, logger=logger)
    finally:
        if on_launch_aborted is not None:
            on_launch_aborted()


def close_and_wait(running: Any) -> int:
    """Flush and close the log handles, then wait for the engine process."""

    try:
        running.stdout_handle.flush()
        running.stderr_handle.flush()
    finally:
        running.stdout_handle.close()
        running.stderr_handle.close()
    exit_code = running.process.poll()
    if exit_code is None:
        exit_code = running.process.wait()
    return exit_code


def publish_running_scratch(running: Any, *, logger: logging.Logger) -> dict[str, Any]:
    """Publish the job's scratch workspace and rebase its recorded paths."""

    if running.scratch_workspace is None:
        return {}
    publication = publish_engine_scratch_workspace(
        running.scratch_workspace,
        logger=logger,
    )
    scratch_provenance = scratch_publication_provenance(publication)
    durable_job_dir = Path(running.durable_job_dir)
    running.job_dir = str(durable_job_dir)
    running.stdout_log = str((durable_job_dir / Path(running.stdout_log).name).resolve())
    running.stderr_log = str((durable_job_dir / Path(running.stderr_log).name).resolve())
    running.scratch_workspace = None
    return scratch_provenance


def finalize_snapshot_is_valid(running: Any, *, display_name: str) -> bool:
    """Re-verify the queued input snapshots after the engine process exits."""

    if not running.execution_snapshot:
        return True
    try:
        descriptors = running.execution_snapshot.get("input_snapshots")
        if not isinstance(descriptors, dict):
            raise ValueError(f"Queued {display_name} execution snapshot has no input descriptors")
        verify_input_snapshots(running.job_dir, descriptors)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


__all__ = [
    "abort_launch_publication",
    "close_and_wait",
    "create_engine_scratch_workspace",
    "finalize_snapshot_is_valid",
    "publish_engine_scratch_workspace",
    "publish_running_scratch",
]
