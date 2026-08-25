from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.core.artifacts import XTB_JOB_MANIFEST_FILE
from orca_auto.core.commands.run_dir import (
    EngineQueuedRecord,
    EngineQueuedRecordCallbacks,
    EngineRunDirSubmission,
    EngineSubmissionSpec,
    build_engine_queued_record,
    build_engine_run_dir_submission_from_spec,
    engine_run_dir_queued_recorder_from_callbacks,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.notifications import engines as _notification_engines
from orca_auto.core.queue.engine.input_snapshot import (
    MAX_INPUT_SNAPSHOT_BYTES,
    read_stable_regular_file,
    snapshot_input_file,
    snapshot_input_payload,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
)
from orca_auto.flow.engines.submission_snapshot import build_reserved_input_snapshot_submission

from ...xyz_utils import (
    MAX_HESSIAN_ADMISSION_ATOMS,
    load_xyz_atom_sequence,
    validate_electronic_state,
    validated_xyz_atom_count,
)
from . import job_locations as _job_locations
from . import state as _state
from .job_inputs import (
    new_job_id,
    queued_state_payload,
    resolve_job_inputs,
)
from .job_locations import index_root_for_path

notify_job_queued = _notification_engines.notify_xtb_job_queued
upsert_job_record = _job_locations.upsert_job_record
write_state = _state.write_state

_XCONTROL_PATH_KEYS = {"nrun", "npoint", "anopt", "kpush", "kpull", "ppull", "alp"}
_XTB_MANIFEST_KEYS = {
    "job_type",
    "reaction_key",
    "molecule_key",
    "input_xyz",
    "reactant_xyz",
    "product_xyz",
    "candidates_dir",
    "top_n",
    "max_ranking_evaluations",
    "allow_high_cost_ranking",
    "resources",
    "gfn",
    "charge",
    "uhf",
    "solvent_model",
    "solvent",
    "opt_level",
    "xcontrol",
    "dry_run",
    "ts_guess_validation",
}


def _validate_canonical_xcontrol(path: Path) -> None:
    try:
        lines = (
            read_stable_regular_file(path, require_single_link=True)
            .decode("utf-8", errors="strict")
            .splitlines()
        )
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"xTB xcontrol must be UTF-8 text: {path}") from exc
    in_path_section = False
    saw_path_section = False
    seen_keys: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        lowered = line.lower()
        if lowered == "$path":
            if in_path_section or saw_path_section:
                raise ValueError("xTB xcontrol may contain exactly one $path section")
            in_path_section = True
            saw_path_section = True
            continue
        if lowered == "$end":
            if not in_path_section:
                raise ValueError("xTB xcontrol has an unmatched $end")
            in_path_section = False
            continue
        if not in_path_section or "=" not in line:
            raise ValueError("xTB xcontrol supports only numeric assignments inside $path")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        normalized_key = key.lower()
        if normalized_key not in _XCONTROL_PATH_KEYS:
            raise ValueError(f"Unsupported xTB $path control: {key}")
        if normalized_key in seen_keys:
            raise ValueError(f"Duplicate xTB $path control: {key}")
        seen_keys.add(normalized_key)
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"xTB $path control {key!r} must be numeric") from exc
        if not math.isfinite(value):
            raise ValueError(f"xTB $path control {key!r} must be finite")
        integer_bounds = {
            "nrun": (1, 10),
            "npoint": (5, 1_000),
            "anopt": (1, 100),
        }
        if normalized_key in integer_bounds:
            minimum, maximum = integer_bounds[normalized_key]
            if not value.is_integer() or not minimum <= value <= maximum:
                raise ValueError(
                    f"xTB $path control {key!r} must be an integer between {minimum} and {maximum}"
                )
        elif normalized_key == "kpull":
            if not -10 <= value < 0:
                raise ValueError("xTB $path control 'kpull' must be in [-10, 0)")
        elif not 0 < value <= 10:
            raise ValueError(f"xTB $path control {key!r} must be in (0, 10]")
    if in_path_section or not saw_path_section:
        raise ValueError("xTB xcontrol must contain one closed $path section")


def _canonical_ts_validation_options(manifest: dict[str, Any]) -> None:
    if "ts_guess_validation" not in manifest:
        return
    raw = manifest.get("ts_guess_validation")
    if not isinstance(raw, dict):
        raise ValueError("xTB ts_guess_validation must be a mapping")
    unknown = set(raw) - {
        "bond_scale",
        "max_spurious_bond_changes",
        "reacting_bond_stretch_scale",
    }
    if unknown:
        raise ValueError(f"Unknown xTB ts_guess_validation fields: {sorted(unknown)}")
    canonical: dict[str, Any] = {}
    for key in ("bond_scale", "reacting_bond_stretch_scale"):
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, bool):
            raise ValueError(f"xTB ts_guess_validation.{key} must be positive and finite")
        try:
            parsed = float(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"xTB ts_guess_validation.{key} must be positive and finite") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"xTB ts_guess_validation.{key} must be positive and finite")
        canonical[key] = parsed
    if "max_spurious_bond_changes" in raw:
        value = raw.get("max_spurious_bond_changes")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                "xTB ts_guess_validation.max_spurious_bond_changes must be a nonnegative integer"
            )
        canonical["max_spurious_bond_changes"] = value
    manifest["ts_guess_validation"] = canonical


def _build_submission_impl(
    cfg: Any,
    job_dir: Any,
    manifest: dict[str, Any],
    args: Any,
    *,
    job_id: str,
    snapshot_namespace: str,
) -> EngineRunDirSubmission:
    unknown_manifest_keys = set(manifest) - _XTB_MANIFEST_KEYS
    if unknown_manifest_keys:
        raise ValueError(f"Unknown xTB manifest fields: {sorted(unknown_manifest_keys)}")
    job = resolve_job_inputs(job_dir, manifest)
    selected_source = Path(job["selected_input_xyz"]).expanduser().resolve()
    if job["job_type"] == "hess":
        validated_xyz_atom_count(
            selected_source,
            max_atoms=MAX_HESSIAN_ADMISSION_ATOMS,
        )
    resource_request = resource_request_from_manifest(cfg, manifest)
    manifest_snapshot = json.loads(json.dumps(manifest, allow_nan=False))
    if not isinstance(manifest_snapshot, dict):
        raise ValueError("xTB manifest must serialize to an object")
    manifest_snapshot["resources"] = dict(resource_request)
    manifest_snapshot.setdefault("charge", 0)
    manifest_snapshot.setdefault("uhf", 0)
    xtb_executable = _engine_runner.resolve_configured_executable(
        cfg,
        path_attr="xtb_executable",
        executable_name="xtb",
        display_name="xTB",
    )
    executable_identity = _engine_runner.executable_identity(xtb_executable)
    manifest_snapshot["_orca_auto_xtb_executable"] = executable_identity["path"]
    runtime_identity = _engine_runner.engine_runtime_identity(job_dir)
    manifest_snapshot["_orca_auto_runtime_identity"] = runtime_identity
    opt_level = str(manifest_snapshot.get("opt_level") or "").strip().lower()
    if opt_level:
        manifest_snapshot["opt_level"] = opt_level
    _canonical_ts_validation_options(manifest_snapshot)

    source_paths = {selected_source}
    secondary_source = job.get("secondary_input_xyz")
    if secondary_source:
        source_paths.add(Path(secondary_source).expanduser().resolve())
    for raw_path in job.get("input_summary", {}).get("candidate_paths", []):
        source_paths.add(Path(str(raw_path)).expanduser().resolve())
    xcontrol = str(manifest_snapshot.get("xcontrol") or "").strip()
    xcontrol_source: Path | None = None
    if xcontrol:
        xcontrol_source = (Path(job_dir) / xcontrol).expanduser().resolve()
        source_paths.add(xcontrol_source)
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    if any(not path.is_relative_to(resolved_job_dir) for path in source_paths):
        raise ValueError("Every xTB submission input must stay inside the job directory")
    snapshot_limit = 4 * MAX_INPUT_SNAPSHOT_BYTES
    aggregate_snapshot_bytes = 0
    counted_snapshot_paths: set[str] = set()

    def snapshot_with_budget(source: str | Path, *, role: str) -> dict[str, Any]:
        nonlocal aggregate_snapshot_bytes
        remaining = snapshot_limit - aggregate_snapshot_bytes
        if remaining < 1:
            raise ValueError("xTB submission inputs exceed the aggregate snapshot size limit")
        descriptor = snapshot_input_file(
            job_dir,
            source,
            role=role,
            max_bytes=remaining,
            namespace=snapshot_namespace,
        )
        snapshot_path = str(descriptor["snapshot_path"])
        if snapshot_path not in counted_snapshot_paths:
            aggregate_snapshot_bytes += int(descriptor["size_bytes"])
            counted_snapshot_paths.add(snapshot_path)
        return descriptor

    input_snapshots: dict[str, dict[str, Any]] = {}
    selected_descriptor = snapshot_with_budget(selected_source, role="selected")
    input_snapshots["selected"] = selected_descriptor
    selected_input_xyz = Path(selected_descriptor["snapshot_path"])
    selected_atoms = load_xyz_atom_sequence(selected_input_xyz)
    charge = _engine_runner.manifest_int(manifest_snapshot, "charge")
    uhf = _engine_runner.manifest_int(manifest_snapshot, "uhf")
    electronic_state = validate_electronic_state(
        selected_input_xyz,
        charge=0 if charge is None else charge,
        uhf=0 if uhf is None else uhf,
    )
    manifest_snapshot["_orca_auto_electronic_state"] = electronic_state

    secondary_input_xyz: Path | None = None
    if secondary_source:
        secondary_descriptor = snapshot_with_budget(secondary_source, role="secondary")
        input_snapshots["secondary"] = secondary_descriptor
        secondary_input_xyz = Path(secondary_descriptor["snapshot_path"])
        if load_xyz_atom_sequence(secondary_input_xyz) != selected_atoms:
            raise ValueError(
                "xTB path-search endpoint snapshots must have identical atom order and elements"
            )

    input_summary = dict(job["input_summary"])
    if job["job_type"] == "path_search":
        input_summary["reactant_xyz"] = str(selected_input_xyz)
        input_summary["product_xyz"] = str(secondary_input_xyz or "")
    elif job["job_type"] == "ranking":
        candidate_paths: list[str] = []
        seen_candidate_digests: set[str] = set()
        for index, raw_path in enumerate(input_summary.get("candidate_paths", [])):
            candidate_source = Path(str(raw_path)).expanduser().resolve()
            if candidate_source == selected_source:
                candidate_path = selected_input_xyz
                descriptor = selected_descriptor
            else:
                role = f"candidate_{index:06d}"
                descriptor = snapshot_with_budget(candidate_source, role=role)
                candidate_path = Path(descriptor["snapshot_path"])
            if load_xyz_atom_sequence(candidate_path) != selected_atoms:
                raise ValueError(
                    "xTB ranking candidate snapshots must have identical atom order and elements"
                )
            candidate_digest = str(descriptor["sha256"])
            if candidate_digest in seen_candidate_digests:
                continue
            seen_candidate_digests.add(candidate_digest)
            if candidate_source != selected_source:
                input_snapshots[role] = descriptor
            candidate_paths.append(str(candidate_path))
        input_summary["candidate_paths"] = candidate_paths
        input_summary["candidate_count"] = len(candidate_paths)
        input_summary["candidates_dir"] = str(selected_input_xyz.parent)
    else:
        input_summary["input_xyz"] = str(selected_input_xyz)

    if xcontrol:
        if str(job.get("job_type") or "") != "path_search":
            raise ValueError("xTB $path xcontrol is supported only for path_search jobs")
        assert xcontrol_source is not None
        descriptor = snapshot_with_budget(xcontrol_source, role="xcontrol")
        input_snapshots["xcontrol"] = descriptor
        manifest_snapshot["xcontrol"] = descriptor["snapshot_path"]
        _validate_canonical_xcontrol(Path(descriptor["snapshot_path"]))

    from .runner import _build_command

    _build_command(
        cfg,
        manifest=manifest_snapshot,
        selected_input_xyz=selected_input_xyz,
        secondary_input_xyz=secondary_input_xyz,
        job_type=str(job["job_type"]),
        resource_request=resource_request,
    )

    manifest_payload = json.dumps(
        manifest_snapshot,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(manifest_payload) > snapshot_limit - aggregate_snapshot_bytes:
        raise ValueError("xTB submission inputs exceed the aggregate snapshot size limit")
    manifest_descriptor = snapshot_input_payload(
        job_dir,
        manifest_payload,
        role="manifest",
        suffix=".json",
        source_path=Path(job_dir) / XTB_JOB_MANIFEST_FILE,
        namespace=snapshot_namespace,
    )
    input_snapshots["manifest"] = manifest_descriptor

    execution_snapshot = {
        "version": 1,
        "snapshot_namespace": snapshot_namespace,
        SNAPSHOT_INTENT_TOKEN_KEY: snapshot_namespace,
        SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(index_root_for_path(cfg, job_dir)),
        "manifest": manifest_snapshot,
        "input_snapshots": input_snapshots,
        "selected_input_xyz": str(selected_input_xyz),
        "secondary_input_xyz": str(secondary_input_xyz or ""),
        "job_type": str(job["job_type"]),
        "reaction_key": str(job["reaction_key"]),
        "input_summary": input_summary,
        "resource_request": resource_request,
        "manifest_path": manifest_descriptor["snapshot_path"],
        "executable_identities": {"xtb": executable_identity},
        "runtime_identity": runtime_identity,
    }
    return build_engine_run_dir_submission_from_spec(
        spec=EngineSubmissionSpec(
            queue_root=index_root_for_path(cfg, job_dir),
            app_name="orca_auto_xtb",
            task_id=job_id,
            task_kind=f"xtb_{job['job_type']}",
            engine="xtb",
            metadata={
                "job_dir": str(job_dir),
                "selected_input_xyz": str(selected_input_xyz),
                "secondary_input_xyz": str(secondary_input_xyz or ""),
                "job_type": str(job["job_type"]),
                "reaction_key": str(job["reaction_key"]),
                "input_summary": input_summary,
                "candidate_paths": list(input_summary.get("candidate_paths", [])),
                "execution_snapshot": execution_snapshot,
            },
            context={
                "job": job,
                "job_dir": job_dir,
                "input_summary": input_summary,
            },
        ),
        args=args,
        manifest=manifest,
        resource_request=resource_request,
    )


def _build_submission(
    cfg: Any,
    job_dir: Any,
    manifest: dict[str, Any],
    args: Any,
) -> EngineRunDirSubmission:
    return build_reserved_input_snapshot_submission(
        cfg,
        job_dir,
        manifest,
        args,
        new_job_id_fn=new_job_id,
        queue_root_for_path_fn=index_root_for_path,
        build_submission_fn=_build_submission_impl,
    )


def _queued_record(submission: EngineRunDirSubmission, _entry: Any) -> EngineQueuedRecord:
    metadata = submission.metadata
    context = submission.context
    context_job = context.get("job")
    if not isinstance(context_job, dict):
        context_job = {}
    job_dir = Path(metadata.get("job_dir") or context["job_dir"]).expanduser().resolve()
    input_summary = metadata.get("input_summary")
    if not isinstance(input_summary, dict):
        input_summary = context.get("input_summary")
    if not isinstance(input_summary, dict):
        input_summary = {}
    selected_input_xyz = metadata.get("selected_input_xyz") or context_job.get("selected_input_xyz")
    if not isinstance(selected_input_xyz, (str, Path)):
        raise ValueError("xTB queued record is missing selected_input_xyz metadata")
    secondary_input_xyz = metadata.get("secondary_input_xyz") or context_job.get(
        "secondary_input_xyz"
    )
    selected_input_path = Path(selected_input_xyz).expanduser().resolve()
    secondary_input_path = (
        Path(secondary_input_xyz).expanduser().resolve()
        if isinstance(secondary_input_xyz, (str, Path)) and str(secondary_input_xyz).strip()
        else None
    )
    job: dict[str, Any] = {
        "job_type": str(metadata.get("job_type") or context_job.get("job_type") or ""),
        "reaction_key": str(metadata.get("reaction_key") or context_job.get("reaction_key") or ""),
        "selected_input_xyz": selected_input_path,
        "secondary_input_xyz": secondary_input_path,
    }
    resource_request = metadata.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = context["resource_request"]
    return build_engine_queued_record(
        submission=submission,
        state_payload=queued_state_payload(
            job_id=submission.task_id,
            job_dir=job_dir,
            selected_input_xyz=job["selected_input_xyz"],
            job_type=str(job["job_type"]),
            reaction_key=str(job["reaction_key"]),
            input_summary=input_summary,
            resource_request=resource_request,
        ),
        index_fields={
            "job_type": str(job["job_type"]),
            "selected_input_xyz": str(job["selected_input_xyz"]),
            "reaction_key": str(job["reaction_key"]),
        },
        notification_fields={
            "job_type": str(job["job_type"]),
            "reaction_key": str(job["reaction_key"]),
            "selected_xyz": job["selected_input_xyz"],
        },
    )


_record_queued = engine_run_dir_queued_recorder_from_callbacks(
    EngineQueuedRecordCallbacks(
        build_record=lambda submission, entry: _queued_record(submission, entry),
        write_state=lambda job_dir, payload: write_state(job_dir, payload),
        upsert_job_record=lambda *args, **kwargs: upsert_job_record(*args, **kwargs),
        notify_job_queued=lambda *args, **kwargs: notify_job_queued(*args, **kwargs),
    ),
    module_name=__name__,
)
