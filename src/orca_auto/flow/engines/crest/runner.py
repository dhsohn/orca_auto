from __future__ import annotations

import logging
import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import CREST_JOB_MANIFEST_FILE
from orca_auto.core.config.engines import (
    WorkflowEngineAppConfig as AppConfig,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.engine_process import (
    cleanup_failed_logged_process_start,
)
from orca_auto.core.engine_scratch import EngineScratchWorkspace
from orca_auto.core.queue.engine.input_snapshot import (
    verify_execution_manifest,
    verify_input_snapshots,
)
from orca_auto.core.utils import fsync_directory, now_utc_iso
from orca_auto.flow.engines.scratch import (
    abort_launch_publication,
    close_and_wait,
    finalize_snapshot_is_valid,
    launch_engine_process,
    publish_running_scratch,
)
from orca_auto.flow.xyz_utils import (
    load_output_xyz_frames,
    load_xyz_frames,
    validated_xyz_atom_count,
)

from .job_inputs import (
    job_mode,
    load_job_manifest,
)

LOGGER = logging.getLogger(__name__)

_RETAINED_ENSEMBLE_CANDIDATES = (
    "crest_conformers.xyz",
    "crest_ensemble.xyz",
    "crest_rotamers.xyz",
    "crest_best.xyz",
)
_CREST_SCRATCH_PUBLICATION_NAMES = frozenset(
    (*_RETAINED_ENSEMBLE_CANDIDATES, "crest.stdout.log", "crest.stderr.log")
)
_CREST_NATIVE_INT_MAX = (1 << 31) - 1
_CREST_DEFAULT_MAX_MD_STEPS = 10_000_000
_CREST_DEFAULT_MAX_AUTOMATIC_MD_STEPS = 14_000_000
_CREST_DEFAULT_MAX_DUMP_FRAMES = 100_000
_CREST_FIXED_REAL_MIN = 1e-6
_CREST_GFN_XTB_DEFAULT_TSTEP_FS = 5.0
_CREST_GFN_FF_DEFAULT_TSTEP_FS = 1.5
_CREST_COMPOSITE_DEFAULT_TSTEP_FS = 2.0
# CREST 3.0.2 auto-selects 2.5–500 ps when mdlen is absent. Native integer
# bounds are supplemented by a conservative admission budget; a larger budget
# requires an explicit high-cost acknowledgement in the manifest.
_CREST_TSTEP_MIN_FS = 0.001
_CREST_TSTEP_MAX_FS = 2500.0
_CREST_MAX_AUTOMATIC_MDLEN_FS = 500_000.0
_CREST_MAX_ATOM_MD_WORK_UNITS = 50_000_000_000


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
    output_identities: dict[str, dict[str, Any]] = field(default_factory=dict)
    scratch_provenance: dict[str, Any] = field(default_factory=dict)


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
    durable_job_dir: str = ""
    scratch_workspace: EngineScratchWorkspace | None = None
    manifest_snapshot: dict[str, Any] = field(default_factory=dict)
    execution_snapshot: dict[str, Any] = field(default_factory=dict)


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
    if speed and speed not in {"quick", "squick", "mquick"}:
        raise ValueError("CREST speed must be 'quick', 'squick', or 'mquick'.")
    if speed:
        command.append(f"--{speed}")


def _append_crest_bool_flags(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (
        ("dry_run", "--dry"),
        ("keepdir", "--keepdir"),
        ("no_preopt", "--noopt"),
        ("noreftopo", "--noreftopo"),
        ("notopo", "--notopo"),
        ("nocbonds", "--nocbonds"),
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
    if str(manifest.get("gfn", "")).strip() and option is None:
        raise ValueError("CREST gfn must be one of 1, 2, ff, or 2//ff.")
    if option:
        if option == "--gfn2//gfnff":
            command.append("--legacy")
        command.append(option)


def _append_crest_int_options(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (("charge", "--chrg"), ("uhf", "--uhf")):
        value = _engine_runner.manifest_int(manifest, manifest_key)
        if value is None:
            value = 0
        if manifest_key == "uhf" and value < 0:
            raise ValueError("CREST uhf must be a nonnegative integer.")
        command.extend([option, str(value)])


def _append_crest_scalar_options(command: list[str], manifest: dict[str, Any]) -> None:
    for manifest_key, option in (
        ("rthr", "--rthr"),
        ("ewin", "--ewin"),
        ("ethr", "--ethr"),
        ("bthr", "--bthr"),
    ):
        value = _crest_positive_real(manifest, manifest_key)
        if value:
            command.extend([option, value])
    cluster = _crest_int(manifest, "cluster")
    if cluster is not None:
        if not 1 <= cluster <= _CREST_NATIVE_INT_MAX:
            raise ValueError(
                f"CREST option 'cluster' must be between 1 and {_CREST_NATIVE_INT_MAX}"
            )
        command.extend(["--cluster", str(cluster)])


# CREST 3.0.2 (`crest --help conf`) conformational-search knobs, exposed as
# additive `crest:` manifest keys. Unlike the older lenient scalar options above,
# these are validated strictly and fail the job closed on a bad value rather than
# forwarding an arbitrary token to CREST. Flag spellings are verified against the
# pinned CREST version; CREST accepts the `--` prefix used across this runner.


def _crest_positive_real(
    manifest: dict[str, Any],
    key: str,
    *,
    minimum: float = _CREST_FIXED_REAL_MIN,
    maximum: float | None = None,
) -> str | None:
    """A strictly-positive real → normalized CREST arg (no exponent), or None if absent."""
    raw = manifest.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"CREST option {key!r} must be a positive number, not a boolean")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CREST option {key!r} must be a positive number, got {raw!r}") from exc
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"CREST option {key!r} must be a positive number, got {raw!r}")
    rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    if not rendered or float(rendered) <= 0:
        raise ValueError(f"CREST option {key!r} is too small to represent safely")
    return rendered


def _crest_int(manifest: dict[str, Any], key: str) -> int | None:
    """A whole-number value → int, or None if absent; rejects bools and fractional values."""
    raw = manifest.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"CREST option {key!r} must be a whole number, not a boolean")
    if isinstance(raw, int):
        return raw
    try:
        as_float = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CREST option {key!r} must be a whole number, got {raw!r}") from exc
    if not as_float.is_integer():
        raise ValueError(f"CREST option {key!r} must be a whole number, got {raw!r}")
    return int(as_float)


def _resolve_mdlen(manifest: dict[str, Any]) -> str | None:
    return _crest_positive_real(manifest, "mdlen")


def _crest_bool(manifest: dict[str, Any], key: str) -> bool:
    """Strict optional boolean for the new sampling contract."""
    raw = manifest.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"CREST option {key!r} must be a boolean, got {raw!r}")


def _validate_md_step_count(
    mdlen: str | None,
    tstep: str | None,
    *,
    gfn: Any = None,
    max_steps: int = _CREST_DEFAULT_MAX_MD_STEPS,
    trajectory_count: int = 1,
) -> int:
    if mdlen is None:
        if tstep is not None:
            raise ValueError("CREST tstep requires an explicit mdlen for work-budget admission")
        step_count = math.ceil(
            (_CREST_MAX_AUTOMATIC_MDLEN_FS / default_timestep_fs(gfn)) * trajectory_count
        )
        if step_count > min(_CREST_NATIVE_INT_MAX, max_steps):
            raise ValueError(
                "CREST automatic mdlen worst case exceeds the admitted work budget of "
                f"{min(_CREST_NATIVE_INT_MAX, max_steps)} MD steps"
            )
        return step_count
    step_fs = float(tstep) if tstep is not None else default_timestep_fs(gfn)
    raw_step_count = (float(mdlen) / step_fs) * 1000.0 * trajectory_count
    if not math.isfinite(raw_step_count):
        raise ValueError(
            "CREST mdlen/tstep combination must produce between 1 and "
            f"{_CREST_NATIVE_INT_MAX} MD steps"
        )
    step_count = math.floor(raw_step_count + 0.5)
    if step_count < 1 or step_count > min(_CREST_NATIVE_INT_MAX, max_steps):
        raise ValueError(
            "CREST mdlen/tstep combination exceeds the admitted work budget of "
            f"{min(_CREST_NATIVE_INT_MAX, max_steps)} MD steps"
        )
    return step_count


def default_timestep_fs(gfn: Any) -> float:
    """The scientific default MD timestep for one CREST GFN level.

    This mapping is safety-relevant physics and must stay the single source
    for admission work-budget estimation (including the remote-ingress bot)
    and CREST command construction alike.
    """
    normalized_gfn = str(gfn or "").strip().lower()
    if normalized_gfn in {"ff", "gfnff"}:
        return _CREST_GFN_FF_DEFAULT_TSTEP_FS
    if normalized_gfn in {"2//ff", "gfn2//gfnff"}:
        return _CREST_COMPOSITE_DEFAULT_TSTEP_FS
    return _CREST_GFN_XTB_DEFAULT_TSTEP_FS


def _append_crest_sampling_options(
    command: list[str],
    manifest: dict[str, Any],
    *,
    atom_count: int,
) -> None:
    mdlen = _resolve_mdlen(manifest)
    wscal = _crest_positive_real(manifest, "wscal")
    scientific_tstep_max = default_timestep_fs(manifest.get("gfn"))
    shake = _crest_int(manifest, "shake")
    if shake == 1:
        scientific_tstep_max = min(scientific_tstep_max, 2.0)
    allow_high_tstep = _crest_bool(manifest, "allow_high_tstep")
    tstep = _crest_positive_real(
        manifest,
        "tstep",
        minimum=_CREST_TSTEP_MIN_FS,
        maximum=_CREST_TSTEP_MAX_FS if allow_high_tstep else scientific_tstep_max,
    )
    configured_max_md_steps = _crest_int(manifest, "max_md_steps")
    max_md_steps = configured_max_md_steps
    if max_md_steps is None:
        max_md_steps = (
            _CREST_DEFAULT_MAX_AUTOMATIC_MD_STEPS if mdlen is None else _CREST_DEFAULT_MAX_MD_STEPS
        )
    if not 1 <= max_md_steps <= _CREST_NATIVE_INT_MAX:
        raise ValueError(f"CREST max_md_steps must be between 1 and {_CREST_NATIVE_INT_MAX}")
    if (
        configured_max_md_steps is not None
        and max_md_steps > _CREST_DEFAULT_MAX_MD_STEPS
        and not _crest_bool(manifest, "allow_high_cost_md")
    ):
        raise ValueError(
            "CREST max_md_steps above the default budget requires allow_high_cost_md=true"
        )
    speed = str(manifest.get("speed") or "").strip().lower()
    mode = job_mode(manifest)
    base_trajectories = 6 if mode == "nci" or speed in {"quick", "squick", "mquick"} else 14
    restart_iterations = 1 if speed == "mquick" else 5
    rotamer_factor = (
        1
        if mode == "nci"
        or speed in {"quick", "squick", "mquick"}
        or _crest_bool(manifest, "norotmd")
        else 2
    )
    trajectory_count = base_trajectories * restart_iterations * rotamer_factor
    estimated_md_steps = _validate_md_step_count(
        mdlen,
        tstep,
        gfn=manifest.get("gfn"),
        max_steps=max_md_steps,
        trajectory_count=trajectory_count,
    )
    estimated_work_units = atom_count * estimated_md_steps
    if estimated_work_units > _CREST_MAX_ATOM_MD_WORK_UNITS:
        raise ValueError(
            "CREST molecule size and aggregate MD steps exceed the server work-unit "
            f"ceiling of {_CREST_MAX_ATOM_MD_WORK_UNITS} atom-steps"
        )

    mddump = _crest_int(manifest, "mddump")
    if mddump is not None and not (1 <= mddump <= _CREST_NATIVE_INT_MAX):
        raise ValueError(f"CREST option 'mddump' must be between 1 and {_CREST_NATIVE_INT_MAX}")
    if mddump is not None and mdlen is None:
        raise ValueError("CREST mddump requires an explicit mdlen for volume-budget admission")
    max_dump_frames = _crest_int(manifest, "max_dump_frames")
    if max_dump_frames is None:
        max_dump_frames = _CREST_DEFAULT_MAX_DUMP_FRAMES
    if not 1 <= max_dump_frames <= _CREST_NATIVE_INT_MAX:
        raise ValueError(f"CREST max_dump_frames must be between 1 and {_CREST_NATIVE_INT_MAX}")
    if max_dump_frames > _CREST_DEFAULT_MAX_DUMP_FRAMES and not _crest_bool(
        manifest, "allow_high_volume_md"
    ):
        raise ValueError(
            "CREST max_dump_frames above the default requires allow_high_volume_md=true"
        )
    estimated_dump_frames = (
        math.ceil((float(mdlen) * 1000.0 * trajectory_count) / mddump)
        if mdlen is not None and mddump is not None
        else 0
    )
    if estimated_dump_frames > max_dump_frames:
        raise ValueError(
            "CREST mddump would exceed the admitted trajectory-volume budget of "
            f"{max_dump_frames} frames"
        )

    if mdlen is not None:
        command.extend(["--mdlen", mdlen])
    if wscal is not None:
        command.extend(["--wscal", wscal])
    if tstep is not None:
        command.extend(["--tstep", tstep])
    if mddump is not None:
        command.extend(["--mddump", str(mddump)])

    if shake is not None:
        if shake not in (0, 1, 2):
            raise ValueError(f"CREST option 'shake' must be 0, 1, or 2, got {shake}")
        command.extend(["--shake", str(shake)])

    if _crest_bool(manifest, "norotmd"):
        command.append("--norotmd")

    cross = _crest_bool(manifest, "cross")
    nocross = _crest_bool(manifest, "nocross")
    if cross and nocross:
        raise ValueError("CREST options 'cross' and 'nocross' are mutually exclusive")
    # GC crossing is CREST 3.0.2's default. Its advertised explicit ``--cross``
    # flag makes a dry run lose the conformational-search job type, so ``true``
    # validates the default without emitting that broken redundant flag.
    if nocross:
        command.append("--nocross")


def _build_command(
    cfg: AppConfig,
    *,
    job_dir: Path,
    selected_xyz: Path,
    manifest: dict[str, Any],
    resource_request: dict[str, int] | None = None,
) -> list[str]:
    resources = dict(resource_request or resource_request_from_manifest(cfg, manifest))
    crest_executable = str(manifest.get("_orca_auto_crest_executable") or "").strip()
    if not crest_executable:
        crest_executable = _resolve_crest_executable(cfg)
    xtb_executable = str(manifest.get("_orca_auto_xtb_executable") or "").strip()
    if not xtb_executable:
        xtb_executable = _engine_runner.resolve_configured_executable(
            cfg,
            path_attr="xtb_executable",
            executable_name="xtb",
            display_name="xTB",
        )
    resolved_selected_xyz = selected_xyz.resolve()
    if not resolved_selected_xyz.is_relative_to(job_dir.resolve()):
        raise ValueError(f"CREST input must be inside the job directory: {selected_xyz}")
    # Keep the exact immutable input absolute. This is unambiguous for nested
    # snapshots and avoids CREST 3.0.2's unsafe shell-based scratch copier.
    selected_xyz_arg = str(resolved_selected_xyz)
    if selected_xyz_arg.startswith("-"):
        selected_xyz_arg = f"./{selected_xyz_arg}"
    command = [
        crest_executable,
        selected_xyz_arg,
        "--T",
        str(resources["max_cores"]),
        "-xnam",
        xtb_executable,
    ]

    _append_crest_mode_flags(command, manifest)
    _append_crest_bool_flags(command, manifest)
    _append_crest_gfn_flag(command, manifest)
    _append_crest_int_options(command, manifest)
    _engine_runner.append_solvent_option(command, manifest)
    _append_crest_scalar_options(command, manifest)
    _append_crest_sampling_options(
        command,
        manifest,
        atom_count=validated_xyz_atom_count(resolved_selected_xyz),
    )
    if _engine_runner.bool_flag(manifest, "esort"):
        command.append("--esort")

    return command


def _frame_atom_sequence(frame: Any) -> tuple[str, ...]:
    return tuple(line.split()[0].casefold() for line in frame.atom_lines)


def _retained_outputs(
    job_dir: Path,
    *,
    selected_input_xyz: str | Path,
    output_identities: dict[str, dict[str, Any]] | None = None,
) -> tuple[int, tuple[str, ...]]:
    # CREST's named retained files overlap (``crest_best.xyz`` is commonly a
    # member of ``crest_conformers.xyz``), but later files can also contain
    # distinct rotamers. Preserve every strictly valid source and retain each
    # file's ensemble multiplicity while removing only cross-file overlap, as
    # downstream fan-out does.
    input_frames = load_xyz_frames(selected_input_xyz)
    if len(input_frames) != 1:
        return 0, ()
    expected_atoms = _frame_atom_sequence(input_frames[0])

    retained_paths: list[str] = []
    seen_geometries: set[tuple[tuple[str, float, float, float], ...]] = set()
    retained_count = 0
    for name in _RETAINED_ENSEMBLE_CANDIDATES:
        path = job_dir / name
        if not path.exists():
            continue
        resolved_path = path.expanduser().resolve()
        if not resolved_path.is_relative_to(job_dir.expanduser().resolve()):
            continue
        try:
            identity_before = _engine_runner.confined_output_identity(job_dir, resolved_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        frames = load_output_xyz_frames(resolved_path)
        try:
            identity_after = _engine_runner.confined_output_identity(job_dir, resolved_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if identity_before != identity_after:
            continue
        if not frames or any(_frame_atom_sequence(frame) != expected_atoms for frame in frames):
            continue
        if output_identities is not None:
            output_identities[str(resolved_path)] = identity_after
        retained_paths.append(str(resolved_path))
        prior_geometries = set(seen_geometries)
        for frame in frames:
            geometry_key = tuple(
                (parts[0].casefold(), float(parts[1]), float(parts[2]), float(parts[3]))
                for line in frame.atom_lines
                if len(parts := line.split()) >= 4
            )
            if geometry_key not in prior_geometries:
                retained_count += 1
            seen_geometries.add(geometry_key)
    return retained_count, tuple(retained_paths)


def _clear_stale_crest_outputs(job_dir: Path, *, selected_input_xyz: Path) -> None:
    protected = selected_input_xyz.expanduser().resolve()
    removed = False
    for name in _RETAINED_ENSEMBLE_CANDIDATES:
        path = job_dir / name
        if path.parent.resolve() / path.name == protected:
            raise ValueError(f"CREST input path conflicts with retained output name: {path}")
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed = True
    if removed:
        fsync_directory(job_dir)


def start_crest_job(
    cfg: AppConfig,
    *,
    job_dir: Path,
    selected_xyz: Path,
    before_popen: Callable[[], None] | None = None,
    on_launch_aborted: Callable[[], None] | None = None,
    execution_snapshot: dict[str, Any] | None = None,
) -> CrestRunningJob:
    if execution_snapshot is None:
        manifest = load_job_manifest(job_dir)
        resource_request = resource_request_from_manifest(cfg, manifest)
    else:
        input_snapshots = execution_snapshot.get("input_snapshots")
        if not isinstance(input_snapshots, dict):
            raise ValueError("Queued CREST execution snapshot has no input descriptors")
        verified_inputs = verify_input_snapshots(job_dir, input_snapshots)
        manifest = verify_execution_manifest(
            execution_snapshot, verified_inputs, display_name="CREST"
        )
        identities = execution_snapshot.get("executable_identities")
        if not isinstance(identities, dict):
            raise ValueError("Queued CREST execution snapshot has no executable identities")
        crest_path = _engine_runner.verify_executable_identity(identities.get("crest"))
        xtb_path = _engine_runner.verify_executable_identity(identities.get("xtb"))
        if (
            str(manifest.get("_orca_auto_crest_executable") or "") != crest_path
            or str(manifest.get("_orca_auto_xtb_executable") or "") != xtb_path
        ):
            raise ValueError("Queued CREST manifest has mismatched executable paths")
        if verified_inputs.get("selected") != selected_xyz.expanduser().resolve():
            raise ValueError("Selected CREST input does not match the queued immutable snapshot")
        resource_raw = execution_snapshot.get("resource_request")
        if not isinstance(resource_raw, dict):
            raise ValueError("Queued CREST execution snapshot has no resource request")
        resource_request = dict(resource_raw)
    runtime_environment = _engine_runner.verified_runtime_environment_for_job(
        job_dir,
        manifest=manifest,
        execution_snapshot=execution_snapshot,
        display_name="CREST",
    )
    resource_actual = _engine_runner.resource_actual_dict(resource_request)
    command = _build_command(
        cfg,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        manifest=manifest,
        resource_request=resource_request,
    )

    resolved_selected_xyz = str(selected_xyz.resolve())
    resolved_job_dir = str(job_dir.resolve())
    resolved_mode = job_mode(manifest)
    manifest_file = job_dir / CREST_JOB_MANIFEST_FILE
    resolved_manifest_path = (
        str(execution_snapshot.get("manifest_path") or "")
        if execution_snapshot is not None
        else (str(manifest_file.resolve()) if manifest_file.exists() else "")
    )
    launch = launch_engine_process(
        cfg,
        command,
        job_dir=job_dir,
        manifest_path=resolved_manifest_path,
        resource_request=resource_request,
        runtime_environment=runtime_environment,
        log_basename="crest",
        publish_name=_CREST_SCRATCH_PUBLICATION_NAMES.__contains__,
        clear_stale_outputs=lambda: _clear_stale_crest_outputs(
            job_dir, selected_input_xyz=selected_xyz
        ),
        before_popen=before_popen,
        on_launch_aborted=on_launch_aborted,
        logger=LOGGER,
    )
    launched = launch.launched
    try:
        return CrestRunningJob(
            process=launched.process,
            command=tuple(command),
            started_at=launched.started_at,
            stdout_log=launch.stdout_log,
            stderr_log=launch.stderr_log,
            stdout_handle=launched.stdout_handle,
            stderr_handle=launched.stderr_handle,
            selected_input_xyz=resolved_selected_xyz,
            mode=resolved_mode,
            manifest_path=resolved_manifest_path,
            job_dir=str(launch.execution_dir.resolve()),
            durable_job_dir=resolved_job_dir,
            scratch_workspace=launch.scratch_workspace,
            resource_request=resource_request,
            resource_actual=resource_actual,
            manifest_snapshot=dict(manifest),
            execution_snapshot=dict(execution_snapshot or {}),
        )
    except BaseException:
        cleanup_failed_logged_process_start(launched)
        abort_launch_publication(launch.scratch_workspace, on_launch_aborted, logger=LOGGER)
        raise


def finalize_crest_job(
    running: CrestRunningJob,
    *,
    forced_status: str | None = None,
    forced_reason: str | None = None,
) -> CrestRunResult:
    exit_code = close_and_wait(running)

    scratch_provenance = publish_running_scratch(running, logger=LOGGER)

    snapshot_valid = finalize_snapshot_is_valid(running, display_name="CREST")
    output_identities: dict[str, dict[str, Any]] = {}
    if snapshot_valid:
        retained_count, retained_paths = _retained_outputs(
            Path(running.job_dir),
            selected_input_xyz=running.selected_input_xyz,
            output_identities=output_identities,
        )
    else:
        retained_count, retained_paths = 0, ()
    finished_at = now_utc_iso()
    for evidence_path in (running.stdout_log, running.stderr_log):
        try:
            identity = _engine_runner.confined_output_identity(running.job_dir, evidence_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        output_identities[str(identity["path"])] = identity

    if not snapshot_valid:
        status = "failed"
    elif forced_status is not None:
        status = forced_status
    elif exit_code == 0 and retained_count < 1:
        status = "failed"
    else:
        status = "completed" if exit_code == 0 else "failed"

    if not snapshot_valid:
        reason = "input_snapshot_changed"
    elif forced_reason is not None:
        reason = forced_reason
    elif exit_code == 0 and retained_count < 1:
        reason = "crest_no_valid_retained_ensemble"
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
        output_identities=output_identities,
        scratch_provenance=scratch_provenance,
    )
