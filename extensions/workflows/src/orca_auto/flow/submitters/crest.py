from __future__ import annotations

from orca_auto.core import queue as _queue_store
from orca_auto.core.commands import queue as _queue_commands
from orca_auto.core.config.engines import load_crest_config as load_config
from orca_auto.core.engines import own_engine_accept_entry
from orca_auto.flow.engines.crest import queue_runtime as _queue_runtime
from orca_auto.flow.engines.crest import submission as _submission
from orca_auto.flow.engines.crest.execution import publish_pending_cancel as _publish_pending_cancel
from orca_auto.flow.engines.crest.job_inputs import load_job_manifest, resolve_job_dir

from .internal_engine_builder import build_internal_engine_module_submitter
from .internal_engine_models import InternalEngineSubmitterDeps

display_status = _queue_commands.display_status
enqueue = _queue_store.enqueue
request_cancel = _queue_store.request_cancel
build_submission = _submission._build_submission
load_queue_config = _queue_runtime.load_config
queue_entries_with_roots = _queue_runtime.queue_entries_with_roots
record_queued = _submission._record_queued


def _before_pending_cancel(entry: object, *, config_path: str) -> None:
    _publish_pending_cancel(entry, config_path=config_path)


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
            accept_entry_fn=own_engine_accept_entry("crest"),
            **kwargs,
        ),
        display_status_fn=display_status,
        before_pending_cancel_fn=_before_pending_cancel,
    )


submit_job_dir, cancel_target = build_internal_engine_module_submitter(
    engine="crest",
    deps_factory=_submitter_deps,
)


__all__ = [
    "cancel_target",
    "submit_job_dir",
]
