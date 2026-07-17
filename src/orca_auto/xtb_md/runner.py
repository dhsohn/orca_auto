from __future__ import annotations

import os
import resource
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner
from orca_auto.core.admission import (
    build_slot_engine_process_preparer,
    build_slot_engine_process_registrar,
)
from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
    ensure_confined_directory,
    start_logged_process,
)
from orca_auto.core.queue.cancellable import retain_process_ownership_until_exit
from orca_auto.core.queue.engine.input_snapshot import (
    read_stable_regular_file,
    verify_input_snapshots,
)
from orca_auto.core.queue.processes import terminate_process_group
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.persistence import fsync_directory

from .artifacts import (
    AttemptIdentity,
    XtbMdArtifactError,
    capture_attempt_identity,
    validate_terminal_artifacts,
)
from .command import build_xtb_md_command
from .limits import (
    MAX_XTB_MD_OUTPUT_BYTES,
    MAX_XTB_MD_OUTPUT_FILES,
    manifest_limits_for_config,
)
from .manifest import XtbMdManifest, parse_manifest
from .path_identity import validate_execution_snapshot_job_dir
from .version import probe_xtb_version

_EXECUTION_PARENT = ".orca_auto_xtb_md_executions"
_STDOUT_NAME = "xtb.stdout.log"
_STDERR_NAME = "xtb.stderr.log"
_MD_INPUT_NAME = "md.inp"
_POLL_SECONDS = 0.25
_MAX_LOG_BYTES = 64 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class XtbMdRunResult:
    status: str
    reason: str
    exit_code: int | None
    execution_dir: str = ""
    started_at: str = ""
    finished_at: str = ""
    command: tuple[str, ...] = ()
    artifacts: dict[str, Any] = field(default_factory=dict)
    engine_payload: dict[str, Any] = field(default_factory=dict)


def _safe_reason(prefix: str, value: Any) -> str:
    text = " ".join(str(value).split())[:500]
    return f"{prefix}:{text}" if text else prefix


def _private_execution_dir(job_dir: Path, task_id: str) -> Path:
    if not task_id or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in task_id
    ):
        raise ValueError("xTB-MD task identity is not a safe execution-directory name")
    parent = ensure_confined_directory(
        job_dir,
        job_dir / _EXECUTION_PARENT,
        label="xTB-MD execution parent",
    )
    execution_dir = parent / task_id
    if execution_dir.exists() or execution_dir.is_symlink():
        raise ValueError(
            "xTB-MD execution generation already exists; a submitted generation cannot retry"
        )
    execution_dir.mkdir(mode=0o700, exist_ok=False)
    fsync_directory(parent)
    return execution_dir.resolve()


def _directory_usage(root: Path) -> tuple[int, int]:
    total_bytes = 0
    total_files = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                status = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(status.st_mode):
                    if entry.is_symlink():
                        raise ValueError("xTB-MD output directory contains a symlink")
                    pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise ValueError("xTB-MD output contains a non-regular file")
                total_files += 1
                total_bytes += status.st_size
                if total_files > MAX_XTB_MD_OUTPUT_FILES:
                    raise ValueError("xTB-MD output exceeds the server file-count limit")
    return total_bytes, total_files


def _validate_snapshot(
    cfg: Any,
    job_dir: Path,
    execution_snapshot: Mapping[str, Any],
    *,
    execution_dir: Path,
) -> tuple[XtbMdManifest, dict[str, int], str, dict[str, str]]:
    if execution_snapshot.get("version") != 1:
        raise ValueError("xTB-MD execution snapshot has an unsupported version")
    if execution_snapshot.get("attempt_limit") != 1:
        raise ValueError("xTB-MD execution snapshot does not enforce one attempt")
    if execution_snapshot.get("retry_supported") is not False:
        raise ValueError("xTB-MD execution snapshot unexpectedly enables retry")
    if execution_snapshot.get("resume_supported") is not False:
        raise ValueError("xTB-MD execution snapshot unexpectedly enables resume")
    if validate_execution_snapshot_job_dir(cfg.runtime.allowed_root, execution_snapshot) != job_dir:
        raise ValueError("xTB-MD execution snapshot job directory changed")

    descriptors = execution_snapshot.get("input_snapshots")
    if not isinstance(descriptors, Mapping) or set(descriptors) != {
        "geometry",
        "manifest",
        "md_input",
    }:
        raise ValueError("xTB-MD execution snapshot input set is invalid")
    verified = verify_input_snapshots(job_dir, descriptors)
    raw_manifest = execution_snapshot.get("manifest")
    if not isinstance(raw_manifest, Mapping):
        raise ValueError("xTB-MD execution snapshot has no canonical manifest")
    input_name = str(raw_manifest.get("input_xyz") or "")
    if not input_name or Path(input_name).name != input_name:
        raise ValueError("xTB-MD snapshot input name is invalid")
    input_path = execution_dir / input_name
    md_input_path = execution_dir / _MD_INPUT_NAME
    atomic_write_confined_bytes(
        execution_dir,
        input_path,
        read_stable_regular_file(verified["geometry"], require_single_link=True),
        label="xTB-MD geometry",
        mode=0o400,
    )
    atomic_write_confined_bytes(
        execution_dir,
        md_input_path,
        read_stable_regular_file(verified["md_input"], require_single_link=True),
        label="xTB-MD control input",
        mode=0o400,
    )
    manifest = parse_manifest(
        raw_manifest,
        job_dir=execution_dir,
        limits=manifest_limits_for_config(cfg),
    )
    if manifest.public_dict() != dict(raw_manifest):
        raise ValueError("xTB-MD canonical manifest changed after snapshot validation")

    raw_resources = execution_snapshot.get("resource_request")
    if not isinstance(raw_resources, Mapping):
        raise ValueError("xTB-MD execution snapshot has no resource request")
    resources: dict[str, int] = {}
    for key, current_limit in (
        ("max_cores", int(cfg.resources.max_cores_per_task)),
        ("max_memory_gb", int(cfg.resources.max_memory_gb_per_task)),
    ):
        value = raw_resources.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= current_limit:
            raise ValueError(f"xTB-MD queued resource {key!r} exceeds the current server cap")
        resources[key] = value

    derived = execution_snapshot.get("derived_budget")
    expected_derived = {
        "atom_count": manifest.atom_count,
        "expected_steps": manifest.expected_steps,
        "expected_frames": manifest.expected_frames,
        "atom_steps": manifest.atom_steps,
        "walltime_seconds": manifest.walltime_seconds,
        "max_output_bytes": MAX_XTB_MD_OUTPUT_BYTES,
    }
    if derived != expected_derived:
        raise ValueError("xTB-MD derived execution budget does not match the manifest")

    executable = engine_runner.verify_executable_identity(
        execution_snapshot.get("executable_identity")
    )
    runtime_identity = engine_runner.rebase_engine_runtime_identity(
        execution_snapshot.get("runtime_identity"),
        execution_dir,
    )
    runtime_environment = engine_runner.verified_engine_runtime_environment(
        execution_dir,
        runtime_identity,
    )
    current_version = probe_xtb_version(executable, env=runtime_environment)
    if current_version != execution_snapshot.get("xtb_version"):
        raise ValueError("xTB-MD executable version identity changed after submission")
    return manifest, resources, executable, runtime_environment


def _stop_process(process: Any) -> None:
    try:
        terminated = terminate_process_group(process)
    except Exception:  # noqa: BLE001
        terminated = False
    if terminated is not True or process.poll() is None:
        retain_process_ownership_until_exit(
            process,
            terminate_process=terminate_process_group,
            poll_interval_seconds=0.5,
        )


def _run_process(
    *,
    command: tuple[str, ...],
    execution_dir: Path,
    manifest: XtbMdManifest,
    resources: dict[str, int],
    runtime_environment: Mapping[str, str],
    attempt: AttemptIdentity,
    admission_root: str | Path,
    admission_token: str,
    should_cancel: Callable[[], bool],
    shutdown_requested: Callable[[], bool],
    validate_job_dir: Callable[[], Path],
    on_started: Callable[[str, str, tuple[str, ...]], None] | None,
) -> XtbMdRunResult:
    stdout_log = execution_dir / _STDOUT_NAME
    stderr_log = execution_dir / _STDERR_NAME
    preparer = build_slot_engine_process_preparer(admission_root, admission_token)
    registrar = build_slot_engine_process_registrar(admission_root, admission_token)
    launched: Any | None = None
    prepared = False
    forced_status = ""
    forced_reason = ""
    exit_code: int | None = None
    started_monotonic = time.monotonic()
    try:
        validate_job_dir()
        preparer()
        prepared = True
        launched = start_logged_process(
            command,
            cwd=execution_dir,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            max_cores=resources["max_cores"],
            base_env=runtime_environment,
            now_utc_iso_fn=now_utc_iso,
            popen_fn=subprocess.Popen,
            stdin_value=subprocess.DEVNULL,
            preexec_fn=process_utils.memory_limit_preexec(
                resources["max_memory_gb"],
                setrlimit_fn=resource.setrlimit,
                limit_resource=resource.RLIMIT_AS,
            ),
        )
        registrar(launched)
        if on_started is not None:
            on_started(str(execution_dir), launched.started_at, command)
        while True:
            try:
                validate_job_dir()
            except Exception as exc:  # noqa: BLE001
                forced_status = "failed"
                forced_reason = _safe_reason("job_directory_identity_changed", exc)
                _stop_process(launched.process)
                exit_code = launched.process.poll()
                break
            polled = launched.process.poll()
            if polled is not None:
                exit_code = int(polled)
                break
            if should_cancel():
                forced_status = "cancelled"
                forced_reason = "cancel_requested"
            elif shutdown_requested():
                forced_status = "failed"
                forced_reason = "worker_shutdown_no_retry"
            elif time.monotonic() - started_monotonic > manifest.walltime_seconds:
                forced_status = "failed"
                forced_reason = "walltime_limit_exceeded"
            else:
                try:
                    output_bytes, _files = _directory_usage(execution_dir)
                except ValueError as exc:
                    forced_status = "failed"
                    forced_reason = _safe_reason("output_policy_violation", exc)
                else:
                    if output_bytes > MAX_XTB_MD_OUTPUT_BYTES:
                        forced_status = "failed"
                        forced_reason = "output_size_limit_exceeded"
            if forced_status:
                _stop_process(launched.process)
                exit_code = launched.process.poll()
                break
            time.sleep(_POLL_SECONDS)
    finally:
        if launched is not None and launched.process.poll() is None:
            _stop_process(launched.process)
        if launched is not None:
            launched.stdout_handle.close()
            launched.stderr_handle.close()
        if prepared:
            registrar(None)

    finished_at = now_utc_iso()
    started_at = launched.started_at if launched is not None else ""
    if forced_status:
        return XtbMdRunResult(
            status=forced_status,
            reason=forced_reason,
            exit_code=exit_code,
            execution_dir=str(execution_dir),
            started_at=started_at,
            finished_at=finished_at,
            command=command,
        )
    validate_job_dir()
    output_bytes, output_files = _directory_usage(execution_dir)
    if output_bytes > MAX_XTB_MD_OUTPUT_BYTES:
        raise XtbMdArtifactError("xTB-MD final output exceeds the server byte limit")
    validate_job_dir()
    terminal = validate_terminal_artifacts(
        attempt,
        manifest=manifest,
        exit_code=int(exit_code if exit_code is not None else -1),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        max_log_bytes=_MAX_LOG_BYTES,
        max_trajectory_bytes=MAX_XTB_MD_OUTPUT_BYTES,
        max_checkpoint_bytes=_MAX_CHECKPOINT_BYTES,
    )
    return XtbMdRunResult(
        status="completed",
        reason="completed",
        exit_code=exit_code,
        execution_dir=str(execution_dir),
        started_at=started_at,
        finished_at=finished_at,
        command=command,
        artifacts=terminal.output_identities,
        engine_payload={
            "completed_steps": terminal.completed_steps,
            "trajectory_frames": terminal.frame_count,
            "atom_count": terminal.atom_count,
            "output_bytes": output_bytes,
            "output_files": output_files,
            "checkpoint_is_diagnostic_only": True,
        },
    )


def run_xtb_md_attempt(
    cfg: Any,
    entry: Any,
    *,
    execution_snapshot: Mapping[str, Any],
    admission_root: str | Path,
    admission_token: str,
    should_cancel: Callable[[], bool],
    shutdown_requested: Callable[[], bool],
    on_started: Callable[[str, str, tuple[str, ...]], None] | None = None,
) -> XtbMdRunResult:
    execution_dir = ""
    try:
        job_dir = validate_execution_snapshot_job_dir(
            cfg.runtime.allowed_root,
            execution_snapshot,
        )
        active_dir = _private_execution_dir(job_dir, str(getattr(entry, "task_id", "") or ""))
        execution_dir = str(active_dir)
        manifest, resources, executable, runtime_environment = _validate_snapshot(
            cfg,
            job_dir,
            execution_snapshot,
            execution_dir=active_dir,
        )
        command = build_xtb_md_command(
            executable=executable,
            input_xyz=manifest.input_xyz,
            md_input=active_dir / _MD_INPUT_NAME,
            manifest=manifest,
            max_cores=resources["max_cores"],
        )
        attempt = capture_attempt_identity(active_dir)
        return _run_process(
            command=command,
            execution_dir=active_dir,
            manifest=manifest,
            resources=resources,
            runtime_environment=runtime_environment,
            attempt=attempt,
            admission_root=admission_root,
            admission_token=admission_token,
            should_cancel=should_cancel,
            shutdown_requested=shutdown_requested,
            validate_job_dir=lambda: validate_execution_snapshot_job_dir(
                cfg.runtime.allowed_root,
                execution_snapshot,
            ),
            on_started=on_started,
        )
    except Exception as exc:  # noqa: BLE001
        return XtbMdRunResult(
            status="failed",
            reason=_safe_reason(f"{exc.__class__.__name__}", exc),
            exit_code=None,
            execution_dir=execution_dir,
            finished_at=now_utc_iso(),
        )


__all__ = ["XtbMdRunResult", "run_xtb_md_attempt"]
