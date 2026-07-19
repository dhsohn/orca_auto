from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import resource
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from orca_auto.core.engine_process import (
    atomic_write_confined_bytes,
    cleanup_failed_logged_process_start,
    recreate_confined_directory,
    start_logged_process,
)
from orca_auto.core.engine_scratch import (
    EngineScratchWorkspace,
    scratch_publication_provenance,
)
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.engine.input_snapshot import (
    read_stable_regular_file,
    verify_input_snapshots,
)
from orca_auto.core.queue.processes import terminate_process_group
from orca_auto.core.utils import fsync_directory, now_utc_iso
from orca_auto.core.utils import process as process_utils
from orca_auto.flow.engines.scratch import (
    create_engine_scratch_workspace,
    publish_engine_scratch_workspace,
)
from orca_auto.flow.hessian_utils import parse_xtb_hessian

from . import runner_ranking as _runner_ranking
from .job_inputs import (
    MANIFEST_FILE_NAME,
    load_job_manifest,
    resolve_job_inputs,
)
from .runner_artifacts import (
    _collect_hessian_candidates,
    _collect_opt_candidates,
    _collect_path_search_candidates,
    _collect_sp_candidates,
    _extract_sp_energy,
)

LOGGER = logging.getLogger(__name__)

_STALE_OUTPUT_NAMES_BY_JOB_TYPE: dict[str, tuple[str, ...]] = {
    "opt": ("xtbopt.xyz", "xtbopt.log", ".xtboptok"),
    "sp": ("xtbout.json", "charges", "wbo", "xtbtopo.mol"),
    "hess": ("hessian", "vibspectrum", "xtbout.json"),
}


def _publish_xtb_scratch_name(job_type: str, name: str) -> bool:
    if name in {"xtb.stdout.log", "xtb.stderr.log"}:
        return True
    if name in _STALE_OUTPUT_NAMES_BY_JOB_TYPE.get(job_type, ()):
        return True
    if job_type == "path_search" and name.startswith("xtbpath") and name.endswith(".xyz"):
        return True
    return False


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
    output_identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    scratch_provenance: dict[str, Any] = field(default_factory=dict)


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
    durable_job_dir: str = ""
    scratch_workspace: EngineScratchWorkspace | None = None
    manifest_snapshot: dict[str, Any] = field(default_factory=dict)
    execution_snapshot: dict[str, Any] = field(default_factory=dict)


def _resolve_xtb_executable(cfg: AppConfig) -> str:
    return _engine_runner.resolve_configured_executable(
        cfg,
        path_attr="xtb_executable",
        executable_name="xtb",
        display_name="xTB",
    )


def _verify_execution_manifest(
    execution_snapshot: dict[str, Any],
    verified_inputs: dict[str, Path],
) -> dict[str, Any]:
    manifest = execution_snapshot.get("manifest")
    manifest_path = verified_inputs.get("manifest")
    if not isinstance(manifest, dict) or manifest_path is None:
        raise ValueError("Queued xTB execution snapshot has no immutable manifest")
    if str(execution_snapshot.get("manifest_path") or "") != str(
        manifest_path
    ) or read_stable_regular_file(
        manifest_path,
        require_single_link=True,
    ) != json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8"):
        raise ValueError("Queued xTB manifest payload does not match its immutable snapshot")
    return dict(manifest)


def _append_xtb_scalar_options(command: list[str], manifest: dict[str, Any]) -> None:
    gfn = str(manifest.get("gfn", "2")).strip().lower()
    if gfn not in {"1", "2", "ff", "gfnff"}:
        raise ValueError("xTB gfn must be one of 1, 2, or ff.")
    if gfn:
        command.extend(["--gfn", "ff" if gfn == "gfnff" else gfn])

    for manifest_key, option in (("charge", "--chrg"), ("uhf", "--uhf")):
        value = _engine_runner.manifest_int(manifest, manifest_key)
        if value is None:
            value = 0
        if manifest_key == "uhf" and value < 0:
            raise ValueError("xTB uhf must be a nonnegative integer.")
        command.extend([option, str(value)])


def _append_xtb_optional_text_options(command: list[str], manifest: dict[str, Any]) -> None:
    namespace = str(manifest.get("namespace", "")).strip()
    if namespace:
        raise ValueError("xTB namespace is not supported by the canonical artifact contract")
    xcontrol = str(manifest.get("xcontrol", "")).strip()
    if xcontrol:
        command.extend(["--input", xcontrol])


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
        if opt_level not in {
            "crude",
            "sloppy",
            "loose",
            "lax",
            "normal",
            "tight",
            "verytight",
            "extreme",
        }:
            raise ValueError("Unsupported xTB optimization level")
        command.extend(["--opt", opt_level])
        return
    if job_type == "sp":
        command.append("--sp")
        return
    if job_type == "hess":
        command.append("--hess")
        return
    raise ValueError(f"Unsupported xtb job_type: {job_type}")


def _clear_stale_xtb_outputs(
    job_dir: Path,
    *,
    job_type: str,
    selected_input_xyz: Path,
    secondary_input_xyz: Path | None,
) -> None:
    protected = {selected_input_xyz.expanduser().resolve()}
    if secondary_input_xyz is not None:
        protected.add(secondary_input_xyz.expanduser().resolve())
    candidates = [job_dir / name for name in _STALE_OUTPUT_NAMES_BY_JOB_TYPE.get(job_type, ())]
    candidates.append(job_dir / "xtbrestart")
    if job_type == "path_search":
        candidates.extend(job_dir.glob("xtbpath*.xyz"))
    removed = False
    for path in candidates:
        if path.parent.resolve() / path.name in protected:
            raise ValueError(f"xTB input path conflicts with canonical output name: {path}")
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed = True
        elif path.exists():
            raise ValueError(f"xTB stale output path must not be a directory: {path}")
    if removed:
        fsync_directory(job_dir)


def _build_command(
    cfg: AppConfig,
    *,
    manifest: dict[str, Any],
    selected_input_xyz: Path,
    secondary_input_xyz: Path | None,
    job_type: str,
    resource_request: dict[str, int] | None = None,
) -> list[str]:
    resources = dict(resource_request or resource_request_from_manifest(cfg, manifest))
    executable = str(manifest.get("_orca_auto_xtb_executable") or "").strip()
    if not executable:
        executable = _resolve_xtb_executable(cfg)
    command = [
        executable,
        str(selected_input_xyz),
        "--parallel",
        str(resources["max_cores"]),
        "--json",
        "--norestart",
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
    job_type: str = "sp",
    should_cancel: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    on_running_job: Callable[[XtbRunningJob | None], None] | None = None,
    terminate_process: Callable[[subprocess.Popen[str]], bool] | None = None,
    candidate_payload: bytes | None = None,
) -> XtbRunResult:
    recreate_confined_directory(
        candidate_run_dir.parent,
        candidate_run_dir,
        label="xTB ranking candidate directory",
    )
    candidate_input = candidate_run_dir / "input.xyz"
    bound_candidate_payload = (
        candidate_payload
        if candidate_payload is not None
        else read_stable_regular_file(candidate_xyz)
    )
    atomic_write_confined_bytes(
        candidate_run_dir,
        candidate_input,
        bound_candidate_payload,
        label="xTB ranking candidate input",
    )
    candidate_manifest = dict(manifest)
    candidate_manifest["job_type"] = job_type
    candidate_manifest["input_xyz"] = "input.xyz"
    if job_type != "path_search":
        candidate_manifest.pop("xcontrol", None)
    parent_runtime_identity = candidate_manifest.get("_orca_auto_runtime_identity")
    if parent_runtime_identity is not None:
        candidate_manifest["_orca_auto_runtime_identity"] = (
            _engine_runner.rebase_engine_runtime_identity(
                parent_runtime_identity,
                candidate_run_dir,
            )
        )
    candidate_manifest_path = candidate_run_dir / MANIFEST_FILE_NAME
    atomic_write_confined_bytes(
        candidate_run_dir,
        candidate_manifest_path,
        yaml.safe_dump(candidate_manifest, sort_keys=False).encode("utf-8"),
        label="xTB ranking candidate manifest",
    )

    def start_candidate() -> XtbRunningJob:
        launch_kwargs: dict[str, Any] = {}
        if prepare_running_job is not None:
            launch_kwargs["before_popen"] = prepare_running_job
            if on_running_job is not None:
                launch_kwargs["on_launch_aborted"] = lambda: on_running_job(None)
        return start_xtb_job(
            cfg,
            job_dir=candidate_run_dir,
            selected_input_xyz=candidate_input,
            **launch_kwargs,
        )

    result = _engine_execution.run_cancellable_engine_process(
        start_job=start_candidate,
        finalize_job=finalize_xtb_job,
        terminate_process=terminate_process or terminate_process_group,
        build_failure_result=lambda exc: exc,
        should_cancel=should_cancel,
        check_cancel_before_poll=True,
        register_running_job=on_running_job,
        should_reraise_exception=lambda _exc: True,
    )
    try:
        executed_payload = read_stable_regular_file(candidate_input)
    except (OSError, ValueError):
        executed_payload = b""
    input_sha256 = hashlib.sha256(bound_candidate_payload).hexdigest()
    if executed_payload != bound_candidate_payload:
        return dataclasses.replace(
            result,
            status="failed",
            reason="candidate_input_changed_during_execution",
            exit_code=1,
            analysis_summary={
                **result.analysis_summary,
                "executed_input_sha256": hashlib.sha256(executed_payload).hexdigest(),
                "expected_input_sha256": input_sha256,
            },
        )
    return dataclasses.replace(
        result,
        analysis_summary={
            **result.analysis_summary,
            "executed_input_sha256": input_sha256,
            "executed_input_size_bytes": len(bound_candidate_payload),
        },
    )


def _wait_for_candidate_sp_result(
    running: Any,
    *,
    should_cancel: Callable[[], bool] | None,
    on_cancel: Callable[[subprocess.Popen[str]], bool] | None,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 1.0,
) -> XtbRunResult:
    process = getattr(running, "process", None)
    if process is None:
        return finalize_xtb_job(running)
    return _engine_execution.run_cancellable_engine_process(
        start_job=lambda: running,
        finalize_job=finalize_xtb_job,
        terminate_process=on_cancel or terminate_process_group,
        build_failure_result=lambda exc: exc,
        should_cancel=should_cancel,
        sleep=sleep_fn,
        poll_interval_seconds=poll_interval_seconds,
        check_cancel_before_poll=True,
        should_reraise_exception=lambda _exc: True,
    )


def _request_candidate_process_stop(
    process: subprocess.Popen[str],
    *,
    on_cancel: Callable[[subprocess.Popen[str]], bool] | None,
) -> bool:
    if process.poll() is not None:
        return True
    if on_cancel is not None:
        return on_cancel(process)
    return terminate_process_group(process)


TS_HESSIAN_DIR_NAME = "ts_hess"


def _with_ts_hessian_provenance(
    result: XtbRunResult,
    provenance: dict[str, Any],
    *,
    ts_detail: dict[str, Any] | None = None,
) -> XtbRunResult:
    candidate_details = tuple(
        {**item, "hessian_provenance": dict(provenance)} if item is ts_detail else item
        for item in result.candidate_details
    )
    return dataclasses.replace(
        result,
        candidate_details=candidate_details,
        analysis_summary={
            **result.analysis_summary,
            "ts_hessian_provenance": dict(provenance),
        },
    )


def run_path_search_ts_hessian_followup(
    cfg: AppConfig,
    result: XtbRunResult,
    *,
    job_dir: Path,
    should_cancel: Callable[[], bool] | None = None,
    prepare_running_job: Callable[[], None] | None = None,
    on_running_job: Callable[[XtbRunningJob | None], None] | None = None,
    terminate_process: Callable[[subprocess.Popen[str]], bool] | None = None,
    execution_snapshot: dict[str, Any] | None = None,
) -> XtbRunResult:
    """Compute a GFN Hessian for the path-search TS guess and record its path.

    The Hessian seeds the downstream ORCA OptTS stage (``InHess Read``). It is
    best-effort: a scientific failure leaves the path result usable and records
    why no Hessian was produced. Cancellation remains cancellation.
    """
    if result.status != "completed" or result.job_type != "path_search":
        return result
    ts_detail = next(
        (item for item in result.candidate_details if item.get("kind") == "ts_guess"),
        None,
    )
    if ts_detail is None:
        return _with_ts_hessian_provenance(
            result,
            {"status": "skipped", "reason": "ts_guess_missing"},
        )
    if ts_detail.get("geometry_valid") is False:
        LOGGER.info("TS guess failed geometry validation; skipping Hessian follow-up")
        return _with_ts_hessian_provenance(
            result,
            {"status": "skipped", "reason": "ts_guess_geometry_invalid"},
            ts_detail=ts_detail,
        )
    ts_guess_xyz = Path(str(ts_detail.get("path") or ""))
    if not ts_guess_xyz.is_file():
        return _with_ts_hessian_provenance(
            result,
            {"status": "skipped", "reason": "ts_guess_missing_on_disk"},
            ts_detail=ts_detail,
        )
    try:
        ts_guess_bytes = read_stable_regular_file(ts_guess_xyz)
    except (OSError, ValueError) as exc:
        return _with_ts_hessian_provenance(
            result,
            {
                "status": "skipped",
                "reason": "ts_guess_unreadable",
                "error_type": type(exc).__name__,
            },
            ts_detail=ts_detail,
        )
    ts_guess_sha256 = hashlib.sha256(ts_guess_bytes).hexdigest()
    hessian_run_dir = job_dir / TS_HESSIAN_DIR_NAME
    try:
        recreate_confined_directory(
            job_dir,
            hessian_run_dir,
            label="xTB Hessian follow-up directory",
        )
        manifest = (
            dict(execution_snapshot.get("manifest") or {})
            if execution_snapshot is not None
            else load_job_manifest(job_dir)
        )
        hessian_result = _run_candidate_sp_job(
            cfg,
            candidate_xyz=ts_guess_xyz,
            candidate_run_dir=hessian_run_dir,
            manifest=manifest,
            job_type="hess",
            should_cancel=should_cancel,
            prepare_running_job=prepare_running_job,
            on_running_job=on_running_job,
            terminate_process=terminate_process,
            candidate_payload=ts_guess_bytes,
        )
    except _engine_execution.ProcessCleanupError:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("TS guess Hessian follow-up crashed; continuing without a Hessian")
        return _with_ts_hessian_provenance(
            result,
            {
                "status": "failed",
                "reason": "hessian_followup_exception",
                "error_type": type(exc).__name__,
                "ts_guess_sha256": ts_guess_sha256,
            },
            ts_detail=ts_detail,
        )
    hessian_path = hessian_run_dir / "hessian"
    attempted_provenance = {
        "status": hessian_result.status,
        "reason": hessian_result.reason,
        "exit_code": hessian_result.exit_code,
        "command": list(hessian_result.command),
        "selected_input_xyz": hessian_result.selected_input_xyz,
        "manifest_path": hessian_result.manifest_path,
        "resource_request": dict(hessian_result.resource_request),
        "resource_actual": dict(hessian_result.resource_actual),
        "started_at": hessian_result.started_at,
        "finished_at": hessian_result.finished_at,
        "stdout_log": hessian_result.stdout_log,
        "stderr_log": hessian_result.stderr_log,
        "analysis_summary": dict(hessian_result.analysis_summary),
        "ts_guess_sha256": ts_guess_sha256,
    }
    try:
        executed_ts_bytes = read_stable_regular_file(hessian_run_dir / "input.xyz")
    except (OSError, ValueError):
        executed_ts_bytes = b""
    if hashlib.sha256(executed_ts_bytes).hexdigest() != ts_guess_sha256:
        return _with_ts_hessian_provenance(
            result,
            {
                **attempted_provenance,
                "status": "failed",
                "reason": "ts_guess_changed_before_hessian_execution",
            },
            ts_detail=ts_detail,
        )
    if hessian_result.status == "cancelled":
        cancelled = _with_ts_hessian_provenance(
            result,
            attempted_provenance,
            ts_detail=ts_detail,
        )
        return dataclasses.replace(
            cancelled,
            status="cancelled",
            reason="cancel_requested",
            exit_code=hessian_result.exit_code,
        )
    if hessian_result.status != "completed" or not hessian_path.is_file():
        LOGGER.warning(
            "TS guess Hessian follow-up did not produce a hessian file "
            "(status=%s, expected=%s); continuing without a Hessian",
            hessian_result.status,
            hessian_path,
        )
        return _with_ts_hessian_provenance(
            result,
            attempted_provenance,
            ts_detail=ts_detail,
        )
    resolved_hessian = str(hessian_path.resolve())
    try:
        hessian_bytes = read_stable_regular_file(hessian_path)
        hessian_matrix = parse_xtb_hessian(hessian_path, payload=hessian_bytes)
    except (OSError, ValueError):
        LOGGER.warning("TS guess Hessian became invalid before provenance capture", exc_info=True)
        return _with_ts_hessian_provenance(
            result,
            {
                **attempted_provenance,
                "status": "failed",
                "reason": "hessian_provenance_invalid",
            },
            ts_detail=ts_detail,
        )
    hessian_provenance = {
        **attempted_provenance,
        "hessian_path": resolved_hessian,
        "hessian_sha256": hashlib.sha256(hessian_bytes).hexdigest(),
        "hessian_size_bytes": len(hessian_bytes),
        "hessian_dimension": len(hessian_matrix),
    }
    candidate_details = tuple(
        {
            **item,
            "hessian_path": resolved_hessian,
            "hessian_provenance": hessian_provenance,
        }
        if item is ts_detail
        else item
        for item in result.candidate_details
    )
    analysis_summary = {
        **result.analysis_summary,
        "ts_hessian_path": resolved_hessian,
        "ts_hessian_provenance": hessian_provenance,
    }
    return dataclasses.replace(
        result,
        candidate_details=candidate_details,
        analysis_summary=analysis_summary,
    )


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
    prepare_running_job: Callable[[], None] | None = None,
    on_running_job: Callable[[XtbRunningJob | None], None] | None = None,
    terminate_process: Callable[[subprocess.Popen[str]], bool] | None = None,
    execution_snapshot: dict[str, Any] | None = None,
) -> XtbRunResult:
    if execution_snapshot is None:
        manifest = load_job_manifest(job_dir)
        inputs = resolve_job_inputs(job_dir, manifest)
    else:
        input_snapshots = execution_snapshot.get("input_snapshots")
        if not isinstance(input_snapshots, dict):
            raise ValueError("Queued xTB execution snapshot has no input descriptors")
        verified_inputs = verify_input_snapshots(job_dir, input_snapshots)
        manifest = _verify_execution_manifest(execution_snapshot, verified_inputs)
        identities = execution_snapshot.get("executable_identities")
        if not isinstance(identities, dict):
            raise ValueError("Queued xTB execution snapshot has no executable identity")
        executable_path = _engine_runner.verify_executable_identity(identities.get("xtb"))
        if str(manifest.get("_orca_auto_xtb_executable") or "") != executable_path:
            raise ValueError("Queued xTB manifest has a mismatched executable path")
        input_summary = execution_snapshot.get("input_summary")
        if not isinstance(input_summary, dict):
            raise ValueError("Queued xTB ranking snapshot has no input summary")
        expected_candidates = [verified_inputs["selected"]]
        expected_candidates.extend(
            verified_inputs[role]
            for role in sorted(verified_inputs)
            if role.startswith("candidate_")
        )
        actual_candidates = [
            Path(str(path)).expanduser().resolve()
            for path in input_summary.get("candidate_paths", [])
        ]
        if (
            actual_candidates != expected_candidates
            or int(input_summary.get("candidate_count", -1)) != len(expected_candidates)
            or Path(str(execution_snapshot.get("selected_input_xyz") or "")).resolve()
            != verified_inputs["selected"]
        ):
            raise ValueError("Queued xTB ranking candidates do not match immutable snapshots")
        inputs = {
            "job_type": str(execution_snapshot.get("job_type") or ""),
            "reaction_key": str(execution_snapshot.get("reaction_key") or ""),
            "selected_input_xyz": execution_snapshot.get("selected_input_xyz"),
            "secondary_input_xyz": execution_snapshot.get("secondary_input_xyz") or None,
            "input_summary": dict(execution_snapshot.get("input_summary") or {}),
            "manifest_path": str(execution_snapshot.get("manifest_path") or ""),
        }
    result = _runner_ranking.run_ranking_job(
        cfg,
        job_dir=job_dir,
        manifest=manifest,
        inputs=inputs,
        should_cancel=should_cancel,
        prepare_running_job=prepare_running_job,
        on_running_job=on_running_job,
        terminate_process=terminate_process,
        deps=_ranking_deps(),
    )
    if execution_snapshot is not None:
        try:
            descriptors = execution_snapshot.get("input_snapshots")
            if not isinstance(descriptors, dict):
                raise ValueError("Queued xTB execution snapshot has no input descriptors")
            verify_input_snapshots(job_dir, descriptors)
        except (OSError, RuntimeError, TypeError, ValueError):
            return dataclasses.replace(
                result,
                status="failed",
                reason="input_snapshot_changed",
                exit_code=1,
                candidate_count=0,
                selected_candidate_paths=(),
            )
    return result


def start_xtb_job(
    cfg: AppConfig,
    *,
    job_dir: Path,
    selected_input_xyz: Path,
    before_popen: Callable[[], None] | None = None,
    on_launch_aborted: Callable[[], None] | None = None,
    execution_snapshot: dict[str, Any] | None = None,
) -> XtbRunningJob:
    if execution_snapshot is None:
        manifest = load_job_manifest(job_dir)
        resource_request = resource_request_from_manifest(cfg, manifest)
        inputs = resolve_job_inputs(job_dir, manifest)
    else:
        input_snapshots = execution_snapshot.get("input_snapshots")
        if not isinstance(input_snapshots, dict):
            raise ValueError("Queued xTB execution snapshot has no input descriptors")
        verified_inputs = verify_input_snapshots(job_dir, input_snapshots)
        manifest = _verify_execution_manifest(execution_snapshot, verified_inputs)
        identities = execution_snapshot.get("executable_identities")
        if not isinstance(identities, dict):
            raise ValueError("Queued xTB execution snapshot has no executable identity")
        executable_path = _engine_runner.verify_executable_identity(identities.get("xtb"))
        if str(manifest.get("_orca_auto_xtb_executable") or "") != executable_path:
            raise ValueError("Queued xTB manifest has a mismatched executable path")
        if verified_inputs.get("selected") != selected_input_xyz.expanduser().resolve():
            raise ValueError("Selected xTB input does not match the queued immutable snapshot")
        secondary_text = str(execution_snapshot.get("secondary_input_xyz") or "").strip()
        if secondary_text:
            if verified_inputs.get("secondary") != Path(secondary_text).expanduser().resolve():
                raise ValueError("Secondary xTB input does not match the immutable snapshot")
        elif "secondary" in verified_inputs:
            raise ValueError("Queued xTB execution snapshot has an unexpected secondary input")
        resource_raw = execution_snapshot.get("resource_request")
        if not isinstance(resource_raw, dict):
            raise ValueError("Queued xTB execution snapshot has no resource request")
        resource_request = dict(resource_raw)
        inputs = {
            "job_type": str(execution_snapshot.get("job_type") or ""),
            "reaction_key": str(execution_snapshot.get("reaction_key") or ""),
            "selected_input_xyz": execution_snapshot.get("selected_input_xyz"),
            "secondary_input_xyz": execution_snapshot.get("secondary_input_xyz") or None,
            "input_summary": dict(execution_snapshot.get("input_summary") or {}),
        }
    runtime_identity = (
        execution_snapshot.get("runtime_identity")
        if execution_snapshot is not None
        else manifest.get("_orca_auto_runtime_identity")
    )
    if runtime_identity is None:
        runtime_identity = _engine_runner.engine_runtime_identity(job_dir)
    if execution_snapshot is not None and runtime_identity != manifest.get(
        "_orca_auto_runtime_identity"
    ):
        raise ValueError("Queued xTB manifest has a mismatched runtime identity")
    runtime_environment = _engine_runner.verified_engine_runtime_environment(
        job_dir,
        runtime_identity,
    )
    resource_actual = _engine_runner.resource_actual_dict(resource_request)
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
        resource_request=resource_request,
    )

    resolved_selected_input = str(selected_input_xyz.resolve())
    resolved_manifest_path = str(
        execution_snapshot.get("manifest_path")
        if execution_snapshot is not None
        else (job_dir / MANIFEST_FILE_NAME).resolve()
    )
    resolved_job_dir = str(job_dir.resolve())
    resolved_job_type = str(inputs["job_type"])
    resolved_reaction_key = str(inputs["reaction_key"])
    resolved_input_summary = dict(inputs["input_summary"])
    scratch_workspace: EngineScratchWorkspace | None = None
    try:
        _clear_stale_xtb_outputs(
            job_dir,
            job_type=resolved_job_type,
            selected_input_xyz=selected_input_xyz,
            secondary_input_xyz=secondary_input_xyz,
        )
        scratch_workspace = create_engine_scratch_workspace(
            cfg,
            job_dir=job_dir,
            manifest_filename=MANIFEST_FILE_NAME,
            max_memory_gb=resource_request["max_memory_gb"],
            publish_name=lambda name: _publish_xtb_scratch_name(resolved_job_type, name),
        )
        execution_dir = scratch_workspace.path if scratch_workspace is not None else job_dir
        stdout_log = execution_dir / "xtb.stdout.log"
        stderr_log = execution_dir / "xtb.stderr.log"
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
            base_env=runtime_environment,
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
        try:
            if scratch_workspace is not None:
                publish_engine_scratch_workspace(scratch_workspace, logger=LOGGER)
        finally:
            if on_launch_aborted is not None:
                on_launch_aborted()
        raise
    try:
        return XtbRunningJob(
            process=launched.process,
            command=tuple(command),
            started_at=launched.started_at,
            stdout_log=resolved_stdout_log,
            stderr_log=resolved_stderr_log,
            stdout_handle=launched.stdout_handle,
            stderr_handle=launched.stderr_handle,
            selected_input_xyz=resolved_selected_input,
            job_type=resolved_job_type,
            reaction_key=resolved_reaction_key,
            input_summary=resolved_input_summary,
            manifest_path=resolved_manifest_path,
            job_dir=str(execution_dir.resolve()),
            durable_job_dir=resolved_job_dir,
            scratch_workspace=scratch_workspace,
            resource_request=resource_request,
            resource_actual=resource_actual,
            manifest_snapshot=dict(manifest),
            execution_snapshot=dict(execution_snapshot or {}),
        )
    except BaseException:
        cleanup_failed_logged_process_start(launched)
        try:
            if scratch_workspace is not None:
                publish_engine_scratch_workspace(scratch_workspace, logger=LOGGER)
        finally:
            if on_launch_aborted is not None:
                on_launch_aborted()
        raise


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

    scratch_provenance: dict[str, Any] = {}
    if running.scratch_workspace is not None:
        publication = publish_engine_scratch_workspace(
            running.scratch_workspace,
            logger=LOGGER,
        )
        scratch_provenance = scratch_publication_provenance(publication)
        durable_job_dir = Path(running.durable_job_dir)
        running.job_dir = str(durable_job_dir)
        running.stdout_log = str((durable_job_dir / Path(running.stdout_log).name).resolve())
        running.stderr_log = str((durable_job_dir / Path(running.stderr_log).name).resolve())
        running.scratch_workspace = None

    status = forced_status if forced_status is not None else _status_from_exit_code(exit_code)
    reason = forced_reason if forced_reason is not None else _reason_from_exit_code(exit_code)
    snapshot_valid = True
    if running.execution_snapshot:
        try:
            descriptors = running.execution_snapshot.get("input_snapshots")
            if not isinstance(descriptors, dict):
                raise ValueError("Queued xTB execution snapshot has no input descriptors")
            verify_input_snapshots(running.job_dir, descriptors)
        except (OSError, RuntimeError, TypeError, ValueError):
            snapshot_valid = False
    if snapshot_valid:
        candidate_count, candidate_paths, candidate_details, analysis_summary = _collect_candidates(
            running
        )
        first_collection = (
            candidate_count,
            candidate_paths,
            candidate_details,
            analysis_summary,
        )
        candidate_count, candidate_paths, candidate_details, analysis_summary = _collect_candidates(
            running
        )
        collection_stable = first_collection == (
            candidate_count,
            candidate_paths,
            candidate_details,
            analysis_summary,
        )
    else:
        status = "failed"
        reason = "input_snapshot_changed"
        candidate_count, candidate_paths, candidate_details, analysis_summary = 0, (), (), {}
        collection_stable = False
    output_identities: dict[str, dict[str, Any]] = {}
    if snapshot_valid and collection_stable:
        output_paths = {
            *candidate_paths,
            running.stdout_log,
            running.stderr_log,
        }
        for detail in candidate_details:
            if isinstance(detail, dict):
                output_paths.update(str(detail.get(key) or "") for key in ("path", "hessian_path"))
        try:
            for output_path in sorted(path for path in output_paths if path):
                identity = _engine_runner.confined_output_identity(running.job_dir, output_path)
                output_identities[str(identity["path"])] = identity
            third_collection = _collect_candidates(running)
            identities_after: dict[str, dict[str, Any]] = {}
            for output_path in sorted(path for path in output_paths if path):
                identity = _engine_runner.confined_output_identity(running.job_dir, output_path)
                identities_after[str(identity["path"])] = identity
            collection_stable = (
                third_collection
                == (candidate_count, candidate_paths, candidate_details, analysis_summary)
                and identities_after == output_identities
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            collection_stable = False
    if snapshot_valid and not collection_stable:
        status = "failed"
        reason = "xtb_output_changed_during_collection"
        candidate_count, candidate_paths, candidate_details, analysis_summary = 0, (), (), {}
    if (
        forced_status is None
        and snapshot_valid
        and collection_stable
        and exit_code == 0
        and candidate_count < 1
    ):
        if running.job_type == "opt":
            status = "failed"
            reason = "xtb_opt_no_valid_geometry"
        elif running.job_type == "sp":
            status = "failed"
            reason = "xtb_sp_no_finite_energy"
        elif running.job_type == "hess":
            status = "failed"
            reason = "xtb_hess_invalid_hessian"

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
        output_identities=output_identities,
        scratch_provenance=scratch_provenance,
    )


def _status_from_exit_code(exit_code: int) -> str:
    return "completed" if exit_code == 0 else "failed"


def _reason_from_exit_code(exit_code: int) -> str:
    return "completed" if exit_code == 0 else f"xtb_exit_code_{exit_code}"


def _collect_candidates(
    running: XtbRunningJob,
) -> tuple[int, tuple[str, ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    if running.job_type == "path_search":
        job_dir = Path(running.job_dir)
        return _collect_path_search_candidates(
            job_dir,
            running.stdout_log,
            input_summary=dict(running.input_summary),
            manifest=dict(running.manifest_snapshot),
        )
    if running.job_type == "opt":
        return _collect_opt_candidates(
            Path(running.job_dir),
            selected_input_xyz=running.selected_input_xyz,
        )
    if running.job_type == "sp":
        return _collect_sp_candidates(Path(running.job_dir))
    if running.job_type == "hess":
        return _collect_hessian_candidates(
            Path(running.job_dir),
            selected_input_xyz=running.selected_input_xyz,
        )
    return 0, (), (), {}
