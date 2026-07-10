from __future__ import annotations

from pathlib import Path
from typing import Any

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
    load_xtb_config as load_config,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.notifications import engines as _notification_engines
from orca_auto.core.queue import enqueue

from . import job_locations as _job_locations
from . import state as _state
from .job_inputs import (
    load_job_manifest,
    new_job_id,
    queued_state_payload,
    resolve_job_dir,
    resolve_job_inputs,
)
from .job_locations import index_root_for_path

notify_job_queued = _notification_engines.notify_xtb_job_queued
upsert_job_record = _job_locations.upsert_job_record
write_state = _state.write_state

__all__ = [
    "enqueue",
    "load_config",
    "load_job_manifest",
    "resolve_job_dir",
]


def _build_submission(
    cfg: Any,
    job_dir: Any,
    manifest: dict[str, Any],
    args: Any,
) -> EngineRunDirSubmission:
    job = resolve_job_inputs(job_dir, manifest)
    job_id = new_job_id()
    input_summary = dict(job["input_summary"])
    return build_engine_run_dir_submission_from_spec(
        spec=EngineSubmissionSpec(
            queue_root=index_root_for_path(cfg, job_dir),
            app_name="orca_auto_xtb",
            task_id=job_id,
            task_kind=f"xtb_{job['job_type']}",
            engine="xtb",
            metadata={
                "job_dir": str(job_dir),
                "selected_input_xyz": str(job["selected_input_xyz"]),
                "secondary_input_xyz": str(job["secondary_input_xyz"] or ""),
                "job_type": str(job["job_type"]),
                "reaction_key": str(job["reaction_key"]),
                "input_summary": input_summary,
                "candidate_paths": list(input_summary.get("candidate_paths", [])),
            },
            context={
                "job": job,
                "job_dir": job_dir,
                "input_summary": input_summary,
            },
        ),
        args=args,
        manifest=manifest,
        resource_request=resource_request_from_manifest(cfg, manifest),
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
