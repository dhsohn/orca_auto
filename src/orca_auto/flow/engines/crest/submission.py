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
    load_crest_config as load_config,
)
from orca_auto.core.config.engines import (
    resource_request_from_manifest,
)
from orca_auto.core.notifications import engines as _notification_engines
from orca_auto.core.queue import enqueue

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


def _build_submission(
    cfg: Any,
    job_dir: Any,
    manifest: dict[str, Any],
    args: Any,
) -> EngineRunDirSubmission:
    selected_xyz = select_input_xyz(job_dir, manifest)
    job_id = new_job_id()
    mode = job_mode(manifest)
    molecule_key = molecule_key_from_selected_xyz(str(selected_xyz), job_dir)
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
        resource_request=resource_request_from_manifest(cfg, manifest),
    )


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
