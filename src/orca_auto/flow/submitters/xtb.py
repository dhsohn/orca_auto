from __future__ import annotations

from typing import Any

from orca_auto.core import queue as _queue_store
from orca_auto.core.commands import queue as _queue_commands
from orca_auto.core.engines import own_engine_accept_entry
from orca_auto.core.utils import normalize_text
from orca_auto.flow.engines.xtb import queue_runtime as _queue_runtime
from orca_auto.flow.engines.xtb import submission as _submission

from .internal_engine_builder import build_internal_engine_module_submitter
from .internal_engine_models import InternalEngineSubmitterDeps

display_status = _queue_commands.display_status
enqueue = _queue_store.enqueue
request_cancel = _queue_store.request_cancel
build_submission = _submission._build_submission
load_config = _submission.load_config
load_job_manifest = _submission.load_job_manifest
load_queue_config = _queue_runtime.load_config
queue_entries_with_roots = _queue_runtime.queue_entries_with_roots
record_queued = _submission._record_queued
resolve_job_dir = _submission.resolve_job_dir


def _extra_fields(submission: Any | None, entry: Any | None) -> dict[str, Any]:
    entry_metadata = getattr(entry, "metadata", {}) if entry is not None else {}
    submission_metadata = getattr(submission, "metadata", {}) if submission is not None else {}
    metadata = (
        entry_metadata
        if isinstance(entry_metadata, dict) and entry_metadata
        else submission_metadata
    )
    return {
        "job_type": normalize_text(metadata.get("job_type")),
        "reaction_key": normalize_text(metadata.get("reaction_key")),
    }


def _submitter_deps() -> InternalEngineSubmitterDeps:
    return InternalEngineSubmitterDeps(
        load_config_fn=load_config,
        resolve_job_dir_fn=resolve_job_dir,
        load_manifest_fn=load_job_manifest,
        build_submission_fn=build_submission,
        record_queued_fn=record_queued,
        enqueue_fn=enqueue,
        load_queue_config_fn=load_queue_config,
        queue_entries_with_roots_fn=queue_entries_with_roots,
        request_cancel_fn=lambda queue_root, queue_id, **kwargs: request_cancel(
            queue_root,
            queue_id,
            accept_entry_fn=own_engine_accept_entry("xtb"),
            **kwargs,
        ),
        display_status_fn=display_status,
    )


submit_job_dir, cancel_target = build_internal_engine_module_submitter(
    engine="xtb",
    deps_factory=_submitter_deps,
    extra_fields_fn=_extra_fields,
)


__all__ = [
    "cancel_target",
    "submit_job_dir",
]
