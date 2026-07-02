from __future__ import annotations

import logging
import os
import resource
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.engine_process import start_logged_process
from orca_auto.core.utils import now_utc_iso
from orca_auto.core.utils import process as process_utils

from . import runner_ranking as _runner_ranking
from .job_inputs import (
    MANIFEST_FILE_NAME,
    load_job_manifest,
    resolve_job_inputs,
)
from .runner_artifacts import (
    _collect_opt_candidates,
    _collect_path_search_candidates,
    _collect_sp_candidates,
    _extract_sp_energy,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class XtbRunResult:
    status: str
    reason: str
    command: tuple[str, ...]
    exit_code: int
    started_at: str
    finished_at: str
    stdout_log: str
    stderr_log: str
    selected_input_xyz: str
    job_type: str
    reaction_key: str
    input_summary: dict[str, Any]
    candidate_count: int
    selected_candidate_paths: tuple[str, ...]
    candidate_details: tuple[dict[str, Any], ...]
    analysis_summary: dict[str, Any]
    manifest_path: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]


@dataclass
class XtbRunningJob:
    process: subprocess.Popen[str]
    command: tuple[str, ...]
    started_at: str
    stdout_log: str
    stderr_log: str
    stdout_handle: TextIO
    stderr_handle: TextIO
    selected_input_xyz: str
    job_type: str
    reaction_key: str
    input_summary: dict[str, Any]
    manifest_path: str
    resource_request: dict[str, int]
    resource_actual: dict[str, int]
    job_dir: str


def _resolve_xtb_executable(cfg: AppConfig) -> str:
    return _engine_runner.resolve_configured_executable(
        cfg,
        path_attr="xtb_executable",
        executable_name="xtb",
        display_name="xTB",
    )


def _append_xtb_scalar_options(command: list[str], manifest: dict[str, Any]) -> None:
    gfn = str(manifest.get("gfn", "2")).strip()
    if gfn:
        command.extend(["--gfn", gfn])

    for manifest_key, option in (("charge", "--chrg"), ("uhf", "--uhf")):
        value = _engine_runner.manifest_int(manifest, manifest_key)
        if value is not None:
            command.extend([option, str(value)])


def _append_xtb_optional_text_options(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (("namespace", "--namespace"), ("xcontrol", "--input")):
        value = str(manifest.get(manifest_key, "")).strip()
        if value:
            command.extend([option, value])


def _append_xtb_job_type_options(
    command: list[str],
    *,
    manifest: dict[str, Any],
    secondary_input_xyz: Path | None,
    job_type: str,
) -> None:
    if job_type == "path_search":
        if secondary_input_xyz is None:
            raise ValueError("path_search requires a product/reference structure")
        command.extend(["--path", str(secondary_input_xyz)])
        return
    if job_type == "opt":
        opt_level = (
            str(manifest.get("opt_level", manifest.get("opt", "normal"))).strip().lower()
            or "normal"
        )
        command.extend(["--opt", opt_level])
        return
    if job_type == "sp":
        command.append("--sp")
        return
    raise ValueError(f"Unsupported xtb job_type: {job_type}")


def _build_command(
    cfg: AppConfig,
    *,
    manifest: dict[str, Any],
    selected_input_xyz: Path,
    secondary_input_xyz: Path | None,
    job_type: str,
) -> list[str]:
    resource_request = resource_request_from_manifest(cfg, manifest)
    command = [
        _resolve_xtb_executable(cfg),
        str(selected_input_xyz),
        "--parallel",
        str(resource_request["max_cores"]),
        "--json",
    ]

    _append_xtb_scalar_options(command, manifest)
    _engine_runner.append_solvent_option(command, manifest)
    _append_xtb_optional_text_options(command, manifest)
    _append_xtb_job_type_options(
        command,
        manifest=manifest,
        secondary_input_xyz=secondary_input_xyz,
        job_type=job_type,
    )

    if _engine_runner.bool_flag(manifest, "dry_run"):
        command.append("--define")

    return command


def _run_candidate_sp_job(
    cfg: AppConfig,
    *,
    candidate_xyz: Path,
    candidate_run_dir: Path,
    manifest: dict[str, Any],
    should_cancel: Callable[[], bool] | None = None,
    on_running_job: Callable[[XtbRunningJob | None], None] | None = None,
    terminate_process: Callable[[subprocess.Popen[str]], None] | None = None,
) -> XtbRunResult:
    candidate_run_dir.mkdir(parents=True, exist_ok=True)
    candidate_input = candidate_run_dir / "input.xyz"
    shutil.copy2(candidate_xyz, candidate_input)
    candidate_manifest = dict(manifest)
    candidate_manifest["job_type"] = "sp"
    candidate_manifest["input_xyz"] = "input.xyz"
    candidate_manifest_path = candidate_run_dir / MANIFEST_FILE_NAME
    candidate_manifest_path.write_text(
        yaml.safe_dump(candidate_manifest, sort_keys=False), encoding="utf-8"
    )
    running = start_xtb_job(
        cfg,
        job_dir=candidate_run_dir,
        selected_input_xyz=candidate_input,
    )
    if on_running_job is not None:
        on_running_job(running)
    try:
        return _wait_for_candidate_sp_result(
            running,
            should_cancel=should_cancel,
            on_cancel=terminate_process,
        )
    finally:
        if on_running_job is not None:
            on_running_job(None)


def _wait_for_candidate_sp_result(
    running: Any,
    *,
    should_cancel: Callable[[], bool] | None,
    on_cancel: Callable[[subprocess.Popen[str]], None] | None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 1.0,
) -> XtbRunResult:
    process = getattr(running, "process", None)
    if process is None:
        return finalize_xtb_job(running)
    while True:
        if should_cancel is not None and should_cancel():
            _request_candidate_process_stop(process, on_cancel=on_cancel)
            return finalize_xtb_job(
                running,
                forced_status="cancelled",
                forced_reason="cancel_requested",
            )
        if process.poll() is not None:
            return finalize_xtb_job(running)
        sleep_fn(poll_interval_seconds)


def _request_candidate_process_stop(
    process: subprocess.Popen[str],
    *,
    on_cancel: Callable[[subprocess.Popen[str]], None] | None,
) -> None:
    if process.poll() is not None:
        return
    if on_cancel is not None:
        on_cancel(process)
        return
    try:
        process.terminate()
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("xtb_process_terminate_failed: error=%s", exc)


def _ranking_deps() -> _runner_ranking.RankingDeps:
    return _runner_ranking.RankingDeps(
        now_utc_iso=now_utc_iso,
        resource_request_dict=resource_request_from_manifest,
        resource_actual_dict=_engine_runner.resource_actual_dict,
        run_candidate_sp_job=_run_candidate_sp_job,
        extract_sp_energy=_extract_sp_energy,
        result_cls=XtbRunResult,
    )


def run_xtb_ranking_job(
    cfg: AppConfig,
    *,
    job_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
    on_running_job: Callable[[XtbRunningJob | None], None] | None = None,
    terminate_process: Callable[[subprocess.Popen[str]], None] | None = None,
) -> XtbRunResult:
    manifest = load_job_manifest(job_dir)
    inputs = resolve_job_inputs(job_dir, manifest)
    return _runner_ranking.run_ranking_job(
        cfg,
        job_dir=job_dir,
        manifest=manifest,
        inputs=inputs,
        should_cancel=should_cancel,
        on_running_job=on_running_job,
        terminate_process=terminate_process,
        deps=_ranking_deps(),
    )


def start_xtb_job(cfg: AppConfig, *, job_dir: Path, selected_input_xyz: Path) -> XtbRunningJob:
    manifest = load_job_manifest(job_dir)
    resource_request = resource_request_from_manifest(cfg, manifest)
    resource_actual = _engine_runner.resource_actual_dict(resource_request)
    inputs = resolve_job_inputs(job_dir, manifest)
    secondary_raw = inputs.get("secondary_input_xyz")
    secondary_input_xyz = None
    if secondary_raw:
        secondary_input_xyz = Path(str(secondary_raw)).expanduser().resolve()
    command = _build_command(
        cfg,
        manifest=manifest,
        selected_input_xyz=selected_input_xyz,
        secondary_input_xyz=secondary_input_xyz,
        job_type=str(inputs["job_type"]),
    )

    stdout_log = job_dir / "xtb.stdout.log"
    stderr_log = job_dir / "xtb.stderr.log"
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
    return XtbRunningJob(
        process=launched.process,
        command=tuple(command),
        started_at=launched.started_at,
        stdout_log=str(launched.stdout_log.resolve()),
        stderr_log=str(launched.stderr_log.resolve()),
        stdout_handle=launched.stdout_handle,
        stderr_handle=launched.stderr_handle,
        selected_input_xyz=str(selected_input_xyz.resolve()),
        job_type=str(inputs["job_type"]),
        reaction_key=str(inputs["reaction_key"]),
        input_summary=dict(inputs["input_summary"]),
        manifest_path=str((job_dir / MANIFEST_FILE_NAME).resolve()),
        job_dir=str(job_dir.resolve()),
        resource_request=resource_request,
        resource_actual=resource_actual,
    )


def finalize_xtb_job(
    running: XtbRunningJob,
    *,
    forced_status: str | None = None,
    forced_reason: str | None = None,
) -> XtbRunResult:
    try:
        running.stdout_handle.flush()
        running.stderr_handle.flush()
    finally:
        running.stdout_handle.close()
        running.stderr_handle.close()

    exit_code = running.process.poll()
    if exit_code is None:
        exit_code = running.process.wait()
    finished_at = now_utc_iso()

    status = forced_status if forced_status is not None else _status_from_exit_code(exit_code)
    reason = forced_reason if forced_reason is not None else _reason_from_exit_code(exit_code)
    candidate_count, candidate_paths, candidate_details, analysis_summary = _collect_candidates(
        running
    )

    return XtbRunResult(
        status=status,
        reason=reason,
        command=running.command,
        exit_code=int(exit_code),
        started_at=running.started_at,
        finished_at=finished_at,
        stdout_log=running.stdout_log,
        stderr_log=running.stderr_log,
        selected_input_xyz=running.selected_input_xyz,
        job_type=running.job_type,
        reaction_key=running.reaction_key,
        input_summary=dict(running.input_summary),
        candidate_count=candidate_count,
        selected_candidate_paths=candidate_paths,
        candidate_details=candidate_details,
        analysis_summary=analysis_summary,
        manifest_path=running.manifest_path,
        resource_request=running.resource_request,
        resource_actual=running.resource_actual,
    )


def _status_from_exit_code(exit_code: int) -> str:
    return "completed" if exit_code == 0 else "failed"


def _reason_from_exit_code(exit_code: int) -> str:
    return "completed" if exit_code == 0 else f"xtb_exit_code_{exit_code}"


def _collect_candidates(
    running: XtbRunningJob,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    if running.job_type == "path_search":
        return _collect_path_search_candidates(
            Path(running.job_dir),
            running.stdout_log,
        )
    if running.job_type == "opt":
        return _collect_opt_candidates(Path(running.job_dir))
    if running.job_type == "sp":
        return _collect_sp_candidates(Path(running.job_dir))
    return 0, (), (), {}
