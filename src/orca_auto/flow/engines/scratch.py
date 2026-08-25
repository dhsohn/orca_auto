from __future__ import annotations

import logging
import resource
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import start_logged_process
from orca_auto.core.engine_runner import scratch_engine_runtime_environment
from orca_auto.core.engine_scratch import (
    EngineScratchPolicy,
    EngineScratchWorkspace,
    publish_engine_scratch_workspace,
    scratch_publication_provenance,
)
from orca_auto.core.queue.engine.input_snapshot import verify_input_snapshots
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils import process as process_utils


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


@dataclass(frozen=True)
class LaunchedEngineProcess:
    """The started engine process together with the workspace it runs in."""

    launched: Any
    scratch_workspace: EngineScratchWorkspace | None
    execution_dir: Path
    stdout_log: str
    stderr_log: str


def launch_engine_process(
    cfg: Any,
    command: list[str],
    *,
    job_dir: Path,
    manifest_path: str,
    resource_request: dict[str, int],
    runtime_environment: dict[str, str],
    log_basename: str,
    publish_name: Callable[[str], bool],
    clear_stale_outputs: Callable[[], None],
    before_popen: Callable[[], None] | None,
    on_launch_aborted: Callable[[], None] | None,
    logger: logging.Logger,
) -> LaunchedEngineProcess:
    """Create the scratch workspace and start the engine under its resource limits.

    Anything that fails between clearing the stale outputs and the started process
    publishes the workspace and signals the abort callback, so no caller observes a
    half-launched job.
    """

    scratch_workspace: EngineScratchWorkspace | None = None
    try:
        clear_stale_outputs()
        scratch_workspace = create_engine_scratch_workspace(
            cfg,
            job_dir=job_dir,
            manifest_path=Path(manifest_path),
            max_memory_gb=resource_request["max_memory_gb"],
            publish_name=publish_name,
        )
        execution_dir = scratch_workspace.path if scratch_workspace is not None else job_dir
        process_environment = runtime_environment
        if scratch_workspace is not None:
            process_environment = scratch_engine_runtime_environment(
                scratch_workspace.path,
                runtime_environment,
            )
        stdout_log = execution_dir / f"{log_basename}.stdout.log"
        stderr_log = execution_dir / f"{log_basename}.stderr.log"
        resolved_stdout_log = str(stdout_log.resolve())
        resolved_stderr_log = str(stderr_log.resolve())
        if before_popen is not None:
            before_popen()
        launched = start_logged_process(
            command,
            cwd=execution_dir,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            max_cores=resource_request["max_cores"],
            base_env=process_environment,
            now_utc_iso_fn=now_utc_iso,
            popen_fn=subprocess.Popen,
            stdin_value=subprocess.DEVNULL,
            preexec_fn=process_utils.memory_limit_preexec(
                resource_request["max_memory_gb"],
                setrlimit_fn=resource.setrlimit,
                limit_resource=resource.RLIMIT_AS,
            ),
        )
    except Exception:
        abort_launch_publication(scratch_workspace, on_launch_aborted, logger=logger)
        raise
    return LaunchedEngineProcess(
        launched=launched,
        scratch_workspace=scratch_workspace,
        execution_dir=execution_dir,
        stdout_log=resolved_stdout_log,
        stderr_log=resolved_stderr_log,
    )


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
    "LaunchedEngineProcess",
    "abort_launch_publication",
    "close_and_wait",
    "create_engine_scratch_workspace",
    "finalize_snapshot_is_valid",
    "launch_engine_process",
    "publish_engine_scratch_workspace",
    "publish_running_scratch",
]
