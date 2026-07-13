from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from orca_auto.core import engine_runner as _engine_runner
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
    load_crest_config as load_config,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.notifications import engines as _notification_engines
from orca_auto.core.queue import enqueue
from orca_auto.core.queue.engine.input_snapshot import (
    cleanup_unowned_input_snapshot_namespace,
    reserve_input_snapshot_namespace,
    snapshot_input_file,
    snapshot_input_payload,
)
from orca_auto.core.queue.engine.snapshot_intent import (
    SNAPSHOT_INTENT_QUEUE_ROOT_KEY,
    SNAPSHOT_INTENT_TOKEN_KEY,
    create_snapshot_intent,
    discard_snapshot_intent_if_generations_absent,
)
from orca_auto.flow.xyz_utils import load_xyz_frames, validate_electronic_state

from . import job_locations as _job_locations
from . import state as _state
from .job_inputs import (
    job_mode,
    load_job_manifest,
    new_job_id,
    queued_state_payload,
    resolve_job_dir,
    select_input_xyz,
)
from .job_locations import index_root_for_path, molecule_key_from_selected_xyz

notify_job_queued = _notification_engines.notify_crest_job_queued
upsert_job_record = _job_locations.upsert_job_record
write_state = _state.write_state

__all__ = [
    "enqueue",
    "load_config",
    "load_job_manifest",
    "resolve_job_dir",
]

_CREST_MANIFEST_KEYS = {
    "input_xyz",
    "mode",
    "molecule_key",
    "reaction_key",
    "resources",
    "speed",
    "dry_run",
    "keepdir",
    "no_preopt",
    "noreftopo",
    "no_reftopo",
    "notopo",
    "no_topo",
    "nocbonds",
    "no_cbonds",
    "gfn",
    "charge",
    "uhf",
    "solvent_model",
    "solvent",
    "rthr",
    "ewin",
    "ethr",
    "bthr",
    "cluster",
    "mdlen",
    "len",
    "wscal",
    "tstep",
    "allow_high_tstep",
    "max_md_steps",
    "allow_high_cost_md",
    "mddump",
    "max_dump_frames",
    "allow_high_volume_md",
    "shake",
    "norotmd",
    "cross",
    "nocross",
    "esort",
}


def _build_submission_impl(
    cfg: Any,
    job_dir: Any,
    manifest: dict[str, Any],
    args: Any,
    *,
    job_id: str,
    snapshot_namespace: str,
) -> EngineRunDirSubmission:
    unknown_manifest_keys = set(manifest) - _CREST_MANIFEST_KEYS
    if unknown_manifest_keys:
        raise ValueError(f"Unknown CREST manifest fields: {sorted(unknown_manifest_keys)}")
    selected_xyz = select_input_xyz(job_dir, manifest)
    if len(load_xyz_frames(selected_xyz)) != 1:
        raise ValueError("CREST input must contain exactly one valid finite XYZ frame")
    mode = job_mode(manifest)
    molecule_key = molecule_key_from_selected_xyz(str(selected_xyz), job_dir)
    resource_request = resource_request_from_manifest(cfg, manifest)
    manifest_snapshot = json.loads(json.dumps(manifest, allow_nan=False))
    if not isinstance(manifest_snapshot, dict):
        raise ValueError("CREST manifest must serialize to an object")
    manifest_snapshot["resources"] = dict(resource_request)
    manifest_snapshot.setdefault("charge", 0)
    manifest_snapshot.setdefault("uhf", 0)
    crest_executable = _engine_runner.resolve_configured_executable(
        cfg,
        path_attr="crest_executable",
        executable_name="crest",
        display_name="CREST",
    )
    xtb_executable = _engine_runner.resolve_configured_executable(
        cfg,
        path_attr="xtb_executable",
        executable_name="xtb",
        display_name="xTB",
    )
    executable_identities = {
        "crest": _engine_runner.executable_identity(crest_executable),
        "xtb": _engine_runner.executable_identity(xtb_executable),
    }
    manifest_snapshot["_orca_auto_crest_executable"] = executable_identities["crest"]["path"]
    manifest_snapshot["_orca_auto_xtb_executable"] = executable_identities["xtb"]["path"]
    runtime_identity = _engine_runner.engine_runtime_identity(job_dir)
    manifest_snapshot["_orca_auto_runtime_identity"] = runtime_identity
    for canonical_key, alias_key in (
        ("noreftopo", "no_reftopo"),
        ("notopo", "no_topo"),
        ("nocbonds", "no_cbonds"),
    ):
        canonical_present = canonical_key in manifest_snapshot
        alias_present = alias_key in manifest_snapshot
        if canonical_present and alias_present:
            if _engine_runner.bool_flag(
                manifest_snapshot, canonical_key
            ) != _engine_runner.bool_flag(manifest_snapshot, alias_key):
                raise ValueError(f"CREST aliases {canonical_key!r} and {alias_key!r} must match")
        if alias_present:
            manifest_snapshot[canonical_key] = _engine_runner.bool_flag(
                manifest_snapshot, alias_key
            )
        manifest_snapshot.pop(alias_key, None)
    selected_descriptor = snapshot_input_file(
        job_dir,
        selected_xyz,
        role="selected",
        namespace=snapshot_namespace,
    )
    selected_xyz = Path(selected_descriptor["snapshot_path"])
    if len(load_xyz_frames(selected_xyz)) != 1:
        raise ValueError("CREST input snapshot must contain exactly one valid finite XYZ frame")
    charge = _engine_runner.manifest_int(manifest_snapshot, "charge")
    uhf = _engine_runner.manifest_int(manifest_snapshot, "uhf")
    manifest_snapshot["_orca_auto_electronic_state"] = validate_electronic_state(
        selected_xyz,
        charge=0 if charge is None else charge,
        uhf=0 if uhf is None else uhf,
    )
    from .runner import _build_command

    _build_command(
        cfg,
        job_dir=Path(job_dir).expanduser().resolve(),
        selected_xyz=selected_xyz,
        manifest=manifest_snapshot,
        resource_request=resource_request,
    )
    manifest_descriptor = snapshot_input_payload(
        job_dir,
        json.dumps(
            manifest_snapshot,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        role="manifest",
        suffix=".json",
        source_path=Path(job_dir) / "crest_job.yaml",
        namespace=snapshot_namespace,
    )
    execution_snapshot = {
        "version": 1,
        "snapshot_namespace": snapshot_namespace,
        SNAPSHOT_INTENT_TOKEN_KEY: snapshot_namespace,
        SNAPSHOT_INTENT_QUEUE_ROOT_KEY: str(index_root_for_path(cfg, job_dir)),
        "manifest": manifest_snapshot,
        "input_snapshots": {"selected": selected_descriptor, "manifest": manifest_descriptor},
        "selected_input_xyz": str(selected_xyz),
        "mode": mode,
        "molecule_key": molecule_key,
        "resource_request": resource_request,
        "manifest_path": manifest_descriptor["snapshot_path"],
        "executable_identities": executable_identities,
        "runtime_identity": runtime_identity,
    }
    return build_engine_run_dir_submission_from_spec(
        spec=EngineSubmissionSpec(
            queue_root=index_root_for_path(cfg, job_dir),
            app_name="orca_auto_crest",
            task_id=job_id,
            task_kind="crest_conformer_search",
            engine="crest",
            metadata={
                "job_dir": str(job_dir),
                "selected_input_xyz": str(selected_xyz),
                "mode": mode,
                "molecule_key": molecule_key,
                "execution_snapshot": execution_snapshot,
            },
            context={
                "job_dir": job_dir,
                "selected_xyz": selected_xyz,
                "mode": mode,
                "molecule_key": molecule_key,
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
    job_id = new_job_id()
    snapshot_namespace = f"snapshot-{secrets.token_hex(16)}"
    resolved_job_dir = Path(job_dir).expanduser().resolve()
    queue_root = (
        index_root_for_path(cfg, resolved_job_dir)
        if getattr(cfg, "runtime", None) is not None
        else resolved_job_dir
    )
    generation_path = resolved_job_dir / ".orca_auto_input_snapshots" / snapshot_namespace
    reserved = False
    intent_created = False
    try:
        create_snapshot_intent(
            queue_root,
            token=snapshot_namespace,
            kind="input_snapshot_namespace",
            generation_paths=[generation_path],
        )
        intent_created = True
        reserve_input_snapshot_namespace(job_dir, snapshot_namespace)
        reserved = True
        return _build_submission_impl(
            cfg,
            job_dir,
            manifest,
            args,
            job_id=job_id,
            snapshot_namespace=snapshot_namespace,
        )
    except BaseException:
        if reserved:
            cleanup_unowned_input_snapshot_namespace(job_dir, snapshot_namespace)
        if intent_created:
            discard_snapshot_intent_if_generations_absent(queue_root, snapshot_namespace)
        raise


def _queued_record(submission: EngineRunDirSubmission, _entry: Any) -> EngineQueuedRecord:
    metadata = submission.metadata
    context = submission.context
    job_dir = Path(metadata.get("job_dir") or context["job_dir"]).expanduser().resolve()
    selected_xyz = (
        Path(metadata.get("selected_input_xyz") or context["selected_xyz"]).expanduser().resolve()
    )
    mode = str(metadata.get("mode") or context["mode"])
    molecule_key = str(metadata.get("molecule_key") or context["molecule_key"])
    resource_request = metadata.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = context["resource_request"]
    return build_engine_queued_record(
        submission=submission,
        state_payload=queued_state_payload(
            job_id=submission.task_id,
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            mode=mode,
            molecule_key=molecule_key,
            resource_request=resource_request,
        ),
        index_fields={
            "mode": mode,
            "selected_input_xyz": str(selected_xyz),
            "molecule_key": molecule_key,
        },
        notification_fields={
            "mode": mode,
            "selected_xyz": selected_xyz,
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
