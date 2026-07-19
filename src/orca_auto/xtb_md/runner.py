from __future__ import annotations

import logging
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
from orca_auto.core.engine_scratch import (
    EngineScratchPolicy,
    EngineScratchWorkspace,
    ScratchPublication,
    publish_engine_scratch_workspace,
    scratch_publication_provenance,
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
_SCRATCH_PUBLISH_NAMES = frozenset(
    {
        _STDOUT_NAME,
        _STDERR_NAME,
        "xtb.trj",
        "mdrestart",
        "xtbmdok",
    }
)
LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _XtbMdProcessResult:
    forced_status: str
    forced_reason: str
    exit_code: int | None
    started_at: str
    finished_at: str


def _safe_reason(prefix: str, value: Any) -> str:
    text = " ".join(str(value).split())[:500]
    if prefix == "EngineScratchError":
        text = text.replace("ORCA", "engine")
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


def _scratch_output_policy_reason(root: Path) -> str:
    try:
        output_bytes, _output_files = _directory_usage(root)
    except ValueError as exc:
        return _safe_reason("output_policy_violation", exc)
    if output_bytes > MAX_XTB_MD_OUTPUT_BYTES:
        return "output_size_limit_exceeded"
    for name, limit in (
        (_STDOUT_NAME, _MAX_LOG_BYTES),
        (_STDERR_NAME, _MAX_LOG_BYTES),
        ("xtb.trj", MAX_XTB_MD_OUTPUT_BYTES),
        ("mdrestart", _MAX_CHECKPOINT_BYTES),
        ("xtbmdok", 1),
    ):
        candidate = root / name
        try:
            status = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if status.st_size > limit:
            return f"output_policy_violation:{name}_size_limit_exceeded"
    return ""


def _xtb_md_scratch_dependencies(_primary_payload: bytes) -> tuple[str, ...]:
    return (_MD_INPUT_NAME,)


def _publish_xtb_md_scratch_name(name: str) -> bool:
    return name in _SCRATCH_PUBLISH_NAMES


def _create_scratch_workspace(
    cfg: Any,
    *,
    execution_dir: Path,
    input_xyz: Path,
    max_memory_gb: int,
) -> EngineScratchWorkspace | None:
    scratch = getattr(cfg, "scratch", None)
    if scratch is None or not bool(getattr(scratch, "enabled", False)):
        return None
    return EngineScratchWorkspace.create(
        EngineScratchPolicy(
            root=Path(str(scratch.root)),
            min_free_bytes=int(scratch.min_free_gb) * 1024**3,
            max_task_memory_bytes=max_memory_gb * 1024**3,
            dependency_names_from_primary=_xtb_md_scratch_dependencies,
            publish_name=_publish_xtb_md_scratch_name,
        ),
        input_xyz,
        durable_output_dir=execution_dir,
    )


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
    process_command: tuple[str, ...],
    reported_command: tuple[str, ...],
    process_dir: Path,
    durable_execution_dir: Path,
    manifest: XtbMdManifest,
    resources: dict[str, int],
    runtime_environment: Mapping[str, str],
    admission_root: str | Path,
    admission_token: str,
    should_cancel: Callable[[], bool],
    shutdown_requested: Callable[[], bool],
    validate_job_dir: Callable[[], Path],
    on_started: Callable[[str, str, tuple[str, ...]], None] | None,
) -> _XtbMdProcessResult:
    stdout_log = process_dir / _STDOUT_NAME
    stderr_log = process_dir / _STDERR_NAME
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
            process_command,
            cwd=process_dir,
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
            on_started(
                str(durable_execution_dir),
                launched.started_at,
                reported_command,
            )
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
                forced_reason = _scratch_output_policy_reason(process_dir)
                if forced_reason:
                    forced_status = "failed"
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
    if not forced_status:
        validate_job_dir()
    final_policy_reason = _scratch_output_policy_reason(process_dir)
    if final_policy_reason:
        forced_status = "failed"
        forced_reason = final_policy_reason
    return _XtbMdProcessResult(
        forced_status=forced_status,
        forced_reason=forced_reason,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
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
    reported_command: tuple[str, ...] = ()
    process_result: _XtbMdProcessResult | None = None
    scratch_workspace: EngineScratchWorkspace | None = None
    scratch_publication: ScratchPublication | None = None
    scratch_publication_attempted = False
    scratch_provenance: dict[str, Any] = {}
    engine_payload: dict[str, Any] = {}
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
        reported_command = build_xtb_md_command(
            executable=executable,
            input_xyz=manifest.input_xyz,
            md_input=active_dir / _MD_INPUT_NAME,
            manifest=manifest,
            max_cores=resources["max_cores"],
        )
        attempt = capture_attempt_identity(active_dir)
        scratch_workspace = _create_scratch_workspace(
            cfg,
            execution_dir=active_dir,
            input_xyz=manifest.input_xyz,
            max_memory_gb=resources["max_memory_gb"],
        )
        process_dir = scratch_workspace.path if scratch_workspace is not None else active_dir
        process_command = reported_command
        if scratch_workspace is not None:
            process_command = build_xtb_md_command(
                executable=executable,
                input_xyz=scratch_workspace.scratch_input,
                md_input=scratch_workspace.path / _MD_INPUT_NAME,
                manifest=manifest,
                max_cores=resources["max_cores"],
            )
        process_result = _run_process(
            process_command=process_command,
            reported_command=reported_command,
            process_dir=process_dir,
            durable_execution_dir=active_dir,
            manifest=manifest,
            resources=resources,
            runtime_environment=runtime_environment,
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
        if scratch_workspace is not None:
            if process_result.forced_reason.startswith(
                ("job_directory_identity_changed:", "output_policy_", "output_size_")
            ):
                scratch_provenance = {
                    "used": True,
                    "filesystem": "tmpfs",
                    "publication_status": "unresolved",
                }
            else:
                scratch_publication_attempted = True
                scratch_publication = publish_engine_scratch_workspace(
                    scratch_workspace,
                    logger=LOGGER,
                )
                scratch_provenance = scratch_publication_provenance(scratch_publication)

        if process_result.forced_status:
            engine_payload = (
                {"scratch_provenance": scratch_provenance} if scratch_provenance else {}
            )
            return XtbMdRunResult(
                status=process_result.forced_status,
                reason=process_result.forced_reason,
                exit_code=process_result.exit_code,
                execution_dir=execution_dir,
                started_at=process_result.started_at,
                finished_at=process_result.finished_at,
                command=reported_command,
                engine_payload=engine_payload,
            )

        validate_execution_snapshot_job_dir(cfg.runtime.allowed_root, execution_snapshot)
        output_bytes, output_files = _directory_usage(active_dir)
        if output_bytes > MAX_XTB_MD_OUTPUT_BYTES:
            raise XtbMdArtifactError("xTB-MD final output exceeds the server byte limit")
        terminal = validate_terminal_artifacts(
            attempt,
            manifest=manifest,
            exit_code=int(process_result.exit_code if process_result.exit_code is not None else -1),
            stdout_log=active_dir / _STDOUT_NAME,
            stderr_log=active_dir / _STDERR_NAME,
            max_log_bytes=_MAX_LOG_BYTES,
            max_trajectory_bytes=MAX_XTB_MD_OUTPUT_BYTES,
            max_checkpoint_bytes=_MAX_CHECKPOINT_BYTES,
        )
        engine_payload = {
            "completed_steps": terminal.completed_steps,
            "trajectory_frames": terminal.frame_count,
            "atom_count": terminal.atom_count,
            "output_bytes": output_bytes,
            "output_files": output_files,
            "checkpoint_is_diagnostic_only": True,
        }
        if scratch_provenance:
            engine_payload["scratch_provenance"] = scratch_provenance
        return XtbMdRunResult(
            status="completed",
            reason="completed",
            exit_code=process_result.exit_code,
            execution_dir=execution_dir,
            started_at=process_result.started_at,
            finished_at=process_result.finished_at,
            command=reported_command,
            artifacts=terminal.output_identities,
            engine_payload=engine_payload,
        )
    except BaseException as caught:  # noqa: BLE001
        error = caught
        if scratch_workspace is not None and not scratch_publication_attempted:
            scratch_publication_attempted = True
            try:
                scratch_publication = publish_engine_scratch_workspace(
                    scratch_workspace,
                    logger=LOGGER,
                )
            except BaseException as publication_error:  # noqa: BLE001
                error = publication_error
                scratch_provenance = {
                    "used": True,
                    "filesystem": "tmpfs",
                    "publication_status": "unresolved",
                }
            else:
                scratch_provenance = scratch_publication_provenance(scratch_publication)
        elif (
            scratch_workspace is not None
            and scratch_publication_attempted
            and scratch_publication is None
        ):
            scratch_provenance = {
                "used": True,
                "filesystem": "tmpfs",
                "publication_status": "unresolved",
            }
        if not isinstance(error, Exception):
            if error is caught:
                raise
            raise error from caught
        engine_payload = {"scratch_provenance": scratch_provenance} if scratch_provenance else {}
        return XtbMdRunResult(
            status="failed",
            reason=_safe_reason(f"{error.__class__.__name__}", error),
            exit_code=process_result.exit_code if process_result is not None else None,
            execution_dir=execution_dir,
            started_at=process_result.started_at if process_result is not None else "",
            finished_at=(
                process_result.finished_at if process_result is not None else now_utc_iso()
            ),
            command=reported_command,
            engine_payload=engine_payload,
        )
    finally:
        if scratch_workspace is not None:
            scratch_workspace.close()


__all__ = ["XtbMdRunResult", "run_xtb_md_attempt"]
