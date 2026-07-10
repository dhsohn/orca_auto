from __future__ import annotations

import os
import resource
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.engine_process import (
    cleanup_failed_logged_process_start,
    start_logged_process,
)
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils import process as process_utils

from .job_inputs import (
    MANIFEST_FILE_NAME,
    job_mode,
    load_job_manifest,
)

_RETAINED_ENSEMBLE_CANDIDATES = (
    "crest_conformers.xyz",
    "crest_ensemble.xyz",
    "crest_rotamers.xyz",
    "crest_best.xyz",
)


@dataclass(frozen=True)
class CrestRunResult:
    status: str
    reason: str
    command: tuple[str, ...]
    exit_code: int
    started_at: str
    finished_at: str
    stdout_log: str
    stderr_log: str
    selected_input_xyz: str
    mode: str
    retained_conformer_count: int
    retained_conformer_paths: tuple[str, ...]
    manifest_path: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]


@dataclass
class CrestRunningJob:
    process: subprocess.Popen[str]
    command: tuple[str, ...]
    started_at: str
    stdout_log: str
    stderr_log: str
    stdout_handle: TextIO
    stderr_handle: TextIO
    selected_input_xyz: str
    mode: str
    manifest_path: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]
    job_dir: str


def _resolve_crest_executable(cfg: AppConfig) -> str:
    return _engine_runner.resolve_configured_executable(
        cfg,
        path_attr="crest_executable",
        executable_name="crest",
        display_name="CREST",
    )


def _append_crest_mode_flags(command: list[str], manifest: dict[str, Any]) -> None:
    if job_mode(manifest) == "nci":
        command.append("--nci")

    speed = str(manifest.get("speed", "")).strip().lower()
    if speed in {"quick", "squick", "mquick"}:
        command.append(f"--{speed}")


def _append_crest_bool_flags(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (
        ("dry_run", "--dry"),
        ("keepdir", "--keepdir"),
        ("no_preopt", "--noopt"),
        ("noreftopo", "--noreftopo"),
        ("no_reftopo", "--noreftopo"),
        ("notopo", "--notopo"),
        ("no_topo", "--notopo"),
        ("nocbonds", "--nocbonds"),
        ("no_cbonds", "--nocbonds"),
    ):
        if _engine_runner.bool_flag(manifest, manifest_key):
            if option in command:
                continue
            command.append(option)


def _append_crest_gfn_flag(command: list[str], manifest: dict[str, Any]) -> None:
    gfn_options = {
        "1": "--gfn1",
        "gfn1": "--gfn1",
        "2": "--gfn2",
        "gfn2": "--gfn2",
        "ff": "--gfnff",
        "gfnff": "--gfnff",
        "2//ff": "--gfn2//gfnff",
        "gfn2//gfnff": "--gfn2//gfnff",
    }
    option = gfn_options.get(str(manifest.get("gfn", "")).strip().lower())
    if option:
        command.append(option)


def _append_crest_int_options(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (("charge", "--chrg"), ("uhf", "--uhf")):
        value = _engine_runner.manifest_int(manifest, manifest_key, zero_is_absent=True)
        if value is not None:
            command.extend([option, str(value)])


def _append_crest_scalar_options(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (
        ("rthr", "--rthr"),
        ("ewin", "--ewin"),
        ("ethr", "--ethr"),
        ("bthr", "--bthr"),
        ("cluster", "--cluster"),
    ):
        value = _engine_runner.manifest_scalar_text(manifest, manifest_key)
        if value:
            command.extend([option, value])


def _build_command(
    cfg: AppConfig,
    *,
    job_dir: Path,
    selected_xyz: Path,
    manifest: dict[str, Any],
) -> list[str]:
    resource_request = resource_request_from_manifest(cfg, manifest)
    try:
        selected_xyz_arg = str(selected_xyz.resolve().relative_to(job_dir.resolve()))
    except ValueError as exc:
        raise ValueError(f"CREST input must be inside the job directory: {selected_xyz}") from exc
    if selected_xyz_arg.startswith("-"):
        selected_xyz_arg = f"./{selected_xyz_arg}"
    command = [
        _resolve_crest_executable(cfg),
        selected_xyz_arg,
        "--T",
        str(resource_request["max_cores"]),
    ]

    _append_crest_mode_flags(command, manifest)
    _append_crest_bool_flags(command, manifest)
    _append_crest_gfn_flag(command, manifest)
    _append_crest_int_options(command, manifest)
    _engine_runner.append_solvent_option(command, manifest)
    _append_crest_scalar_options(command, manifest)
    if _engine_runner.bool_flag(manifest, "esort"):
        command.append("--esort")

    scratch_dir = job_dir / ".crest_scratch"
    command.extend(["--scratch", str(scratch_dir)])

    return command


def _count_xyz_structures(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0

    count = 0
    index = 0
    total = len(lines)
    while index < total:
        text = lines[index].strip()
        if not text:
            index += 1
            continue
        try:
            atom_count = int(text)
        except ValueError:
            break
        index += 1  # atom count
        if index < total:
            index += 1  # comment line
        index += atom_count
        count += 1
    return count


def _retained_outputs(job_dir: Path) -> tuple[int, tuple[str, ...]]:
    found: list[str] = []
    count = 0
    for name in _RETAINED_ENSEMBLE_CANDIDATES:
        path = job_dir / name
        if not path.exists():
            continue
        resolved = str(path.resolve())
        found.append(resolved)
        count = max(count, _count_xyz_structures(path))
    return count, tuple(found)


def start_crest_job(
    cfg: AppConfig,
    *,
    job_dir: Path,
    selected_xyz: Path,
    before_popen: Callable[[], None] | None = None,
    on_launch_aborted: Callable[[], None] | None = None,
) -> CrestRunningJob:
    manifest = load_job_manifest(job_dir)
    resource_request = resource_request_from_manifest(cfg, manifest)
    resource_actual = _engine_runner.resource_actual_dict(resource_request)
    command = _build_command(cfg, job_dir=job_dir, selected_xyz=selected_xyz, manifest=manifest)

    stdout_log = job_dir / "crest.stdout.log"
    stderr_log = job_dir / "crest.stderr.log"
    resolved_stdout_log = str(stdout_log.resolve())
    resolved_stderr_log = str(stderr_log.resolve())
    resolved_selected_xyz = str(selected_xyz.resolve())
    resolved_job_dir = str(job_dir.resolve())
    resolved_mode = job_mode(manifest)
    manifest_file = job_dir / MANIFEST_FILE_NAME
    resolved_manifest_path = str(manifest_file.resolve()) if manifest_file.exists() else ""
    if before_popen is not None:
        before_popen()
    try:
        launched = start_logged_process(
            command,
            cwd=job_dir,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            max_cores=resource_request["max_cores"],
            base_env=os.environ,
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
        if on_launch_aborted is not None:
            on_launch_aborted()
        raise
    try:
        return CrestRunningJob(
            process=launched.process,
            command=tuple(command),
            started_at=launched.started_at,
            stdout_log=resolved_stdout_log,
            stderr_log=resolved_stderr_log,
            stdout_handle=launched.stdout_handle,
            stderr_handle=launched.stderr_handle,
            selected_input_xyz=resolved_selected_xyz,
            mode=resolved_mode,
            manifest_path=resolved_manifest_path,
            job_dir=resolved_job_dir,
            resource_request=resource_request,
            resource_actual=resource_actual,
        )
    except BaseException:
        cleanup_failed_logged_process_start(launched)
        if on_launch_aborted is not None:
            on_launch_aborted()
        raise


def finalize_crest_job(
    running: CrestRunningJob,
    *,
    forced_status: str | None = None,
    forced_reason: str | None = None,
) -> CrestRunResult:
    try:
        running.stdout_handle.flush()
        running.stderr_handle.flush()
    finally:
        running.stdout_handle.close()
        running.stderr_handle.close()

    exit_code = running.process.poll()
    if exit_code is None:
        exit_code = running.process.wait()

    retained_count, retained_paths = _retained_outputs(Path(running.job_dir))
    finished_at = now_utc_iso()

    if forced_status is not None:
        status = forced_status
    else:
        status = "completed" if exit_code == 0 else "failed"

    if forced_reason is not None:
        reason = forced_reason
    else:
        reason = "completed" if exit_code == 0 else f"crest_exit_code_{exit_code}"

    return CrestRunResult(
        status=status,
        reason=reason,
        command=running.command,
        exit_code=int(exit_code),
        started_at=running.started_at,
        finished_at=finished_at,
        stdout_log=running.stdout_log,
        stderr_log=running.stderr_log,
        selected_input_xyz=running.selected_input_xyz,
        mode=running.mode,
        retained_conformer_count=retained_count,
        retained_conformer_paths=retained_paths,
        manifest_path=running.manifest_path,
        resource_request=running.resource_request,
        resource_actual=running.resource_actual,
    )
