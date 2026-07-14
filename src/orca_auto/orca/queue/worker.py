"""Queue worker foreground loop for queue execution under an external supervisor.

This engine worker is launched by the unified orca_auto worker service under
systemd. Each job is spawned in a dedicated child process so locking, state
management, and signal handling remain centralized.
"""

from __future__ import annotations

import copy
import logging
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    activate_reserved_slot,
    list_slots,
    reconcile_stale_slots,
    recover_orphaned_engine_slots,
    recover_slot_engine_process,
    release_slot,
    reserve_slot,
    update_slot_metadata,
)
from orca_auto.core.config.schema import resolved_admission_limit
from orca_auto.core.engine_catalog import get_engine_catalog_entry
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.paths import should_exclude_from_production_runs_scan
from orca_auto.core.queue.engine.execution import coerce_resource_request
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueModule,
    InternalEngineSpec,
    entry_matches_engine_identity,
)
from orca_auto.core.queue.lifecycle import (
    EngineQueueProcessLifecycleHooks,
    cancel_running_process_job,
    mark_terminal_process_queue_entry_with_result,
)
from orca_auto.core.queue.lifecycle import (
    job_queue_root as _lifecycle_job_queue_root,
)
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    queue_record_publication_lock,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.queue.worker import (
    EngineRunningJob as _RunningJob,
)
from orca_auto.core.queue.worker import (
    ManagedProcess as _ManagedProcess,
)
from orca_auto.core.queue.worker import (
    live_queue_slot_keys_for_slots,
    start_background_process,
    terminate_process_group,
)
from orca_auto.core.statuses import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from orca_auto.orca.worker_execution import (
    WORKER_JOB_MODULE,
    BackgroundRunJobProcess,
    build_worker_child_command,
)

from ..attempt.reporting import (
    build_final_result,
    build_run_finished_notification,
    finished_notification_already_sent,
    last_out_path_from_state,
    mark_finished_notification_sent,
)
from ..config import AppConfig, load_config
from ..engine import ENGINE_DEFINITION
from ..inp_rewriter import read_resource_request_from_input
from ..input_artifacts import selected_input_artifacts
from ..job_locations import (
    record_from_artifacts,
    resolve_job_metadata,
    resource_dict,
    upsert_job_record,
)
from ..notifications import notify_run_finished_event
from ..runtime.run_lock import acquire_run_lock
from ..state import (
    finalize_state,
    load_report_json,
    load_state,
    new_state,
    report_json_path,
    state_path,
)
from ..statuses import AnalyzerStatus
from ..types import RunState
from . import worker_lifecycle as _lifecycle_helpers
from . import worker_runtime as _runtime_helpers
from . import worker_tracking as _tracking_helpers
from .adapter import (
    get_cancel_requested,
    get_entry_by_id,
    list_queue,
    mark_cancelled,
    mark_completed,
    mark_failed,
    queue_entry_app_name,
    queue_entry_id,
    queue_entry_metadata,
    queue_entry_reaction_dir,
    queue_entry_task_id,
    reconcile_orphaned_running_entries,
    requeue_running_entry,
    transition_queue_record_publication,
    update_terminal,
    worker_log_path,
)
from .adapter import (
    update_metadata as update_queue_metadata,
)
from .terminal_replay import (
    TERMINAL_REPLAY_METADATA_KEY,
    TerminalReplayMarkerKind,
    terminal_replay_is_fence_only,
    terminal_replay_marker_kind,
)
from .terminal_replay import (
    StateGenerationFingerprint as _StateGenerationFingerprint,
)
from .terminal_replay import (
    load_state_generation_fingerprint as _load_state_generation_fingerprint,
)
from .terminal_replay import (
    state_fingerprint_from_payload as _state_fingerprint_from_payload,
)
from .terminal_replay import (
    terminal_replay_marker_from_entry as _terminal_replay_marker_from_entry,
)
from .terminal_replay import (
    terminal_status_from_run_state as _terminal_status_from_run_state,
)
from .worker_deps import (
    OrcaQueueWorkerFacadeBindings,
    build_late_bound_orca_runtime_facade_deps,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
POLL_INTERVAL_SECONDS = 5
WORKER_SHUTDOWN_GRACE_SECONDS = 10.0

# PID file for the daemon
WORKER_PID_FILE = "queue_worker.pid"
_ENGINE_SPEC = InternalEngineSpec(
    engine="orca",
    worker_job_module=WORKER_JOB_MODULE,
    worker_pid_file_name=WORKER_PID_FILE,
)
_ENGINE_ADMISSION = _ENGINE_SPEC.admission()

_ACTIVE_QUEUE_STATUSES = frozenset({"pending", "running"})
_TERMINAL_QUEUE_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED})
_TERMINAL_REPLAY_METADATA_KEY = TERMINAL_REPLAY_METADATA_KEY
_TERMINAL_FINALIZE_RETRY_ATTR = "_orca_terminal_finalize_retry_pending"


class _TerminalReplaySupersededError(RuntimeError):
    pass


@dataclass(frozen=True)
class _ReactionGenerationRow:
    owner: tuple[str, str]
    task_id: str
    status: str
    transitioned_from_active: bool = False
    pending_replay: bool = False

    @property
    def active(self) -> bool:
        return self.status in _ACTIVE_QUEUE_STATUSES


@dataclass(frozen=True)
class _ArtifactGeneration:
    readable: bool
    state_job_id: str = ""
    report_job_id: str = ""


@dataclass(frozen=True)
class _TerminalReplayWorkItem:
    queue_root: Path
    queue_id: str
    reaction_dir: str
    reaction_key: str
    task_id: str
    observed_status: str
    selected_inp: str
    error: str
    recorded_run_id: str = ""
    resolved_status: str = ""
    run_id: str | None = None
    state_prepared: bool = False
    observed_state: _StateGenerationFingerprint | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (str(self.queue_root), self.queue_id)


def _default_config_path() -> str:
    return ""


def config_path_for_worker(args: Any, *, default_config_path_fn: Any) -> str:
    return str(getattr(args, "config", "") or default_config_path_fn())


def _list_queue_for_runtime(root: str | Path) -> list[QueueEntry]:
    return list_queue(Path(root))


def _mark_recovery_pending_entry(*_args: Any, **_kwargs: Any) -> None:
    return None


def _runtime_facade_deps() -> Any:
    return build_late_bound_orca_runtime_facade_deps(
        OrcaQueueWorkerFacadeBindings(
            release_slot=lambda: release_slot,
            reserve_slot=lambda: _reserve_orca_worker_slot,
            start_background_process=lambda: start_background_process,
            build_worker_child_command=lambda: build_worker_child_command,
            config_path_for_worker=lambda: config_path_for_worker,
            default_config_path=lambda: _default_config_path,
            activate_reserved_slot=lambda: activate_reserved_slot,
            terminate_process=lambda: _terminate_process,
            mark_failed=lambda: mark_failed,
            handle_worker_start_error=lambda: _handle_worker_start_error,
            finalize_completed_job=lambda: _finalize_completed_job,
            finalize_child_exit=lambda: _finalize_child_exit,
            reconcile_worker_state=lambda: _reconcile_worker_state,
            list_queue=lambda: _list_queue_for_runtime,
            list_slots=lambda: list_slots,
            reconcile_stale_slots=lambda: reconcile_stale_slots,
            mark_cancelled=lambda: mark_cancelled,
            requeue_running_entry=lambda: requeue_running_entry,
            mark_recovery_pending=lambda: _mark_recovery_pending_entry,
            try_reserve_admission_slot=lambda: _try_reserve_admission_slot,
            start_background_job_process=lambda: _start_background_job_process,
            load_config=lambda: load_config,
            read_worker_pid=lambda: read_worker_pid,
            worker_class=lambda: QueueWorker,
            on_worker_process_started=lambda: _on_worker_process_started,
            shutdown_running_job=lambda: _shutdown_running_job,
            before_shutdown_all=lambda: _before_shutdown_all,
        ),
        time_module=time,
    )


_queue_module = InternalEngineQueueModule.create_from_definition(
    definition=ENGINE_DEFINITION,
    spec=_ENGINE_SPEC,
    poll_interval_seconds=POLL_INTERVAL_SECONDS,
    shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
    deps=_runtime_facade_deps(),
)
_engine_runtime = _queue_module.runtime


def queue_roots(cfg: AppConfig) -> tuple[Path, ...]:
    return _queue_module.queue_roots(cfg)


def queue_entries_with_roots(cfg: AppConfig) -> list[tuple[Path, Any]]:
    return _queue_module.queue_entries_with_roots(cfg)


def _queue_worker_deps() -> Any:
    return _queue_module.queue_worker_deps()


def _admission_root_for_cfg(cfg: AppConfig) -> str:
    return _queue_module.admission_root(cfg)


def _reserve_orca_worker_slot(root: str | Path, limit: int, **kwargs: Any) -> str | None:
    catalog_entry = get_engine_catalog_entry("orca")
    slot_kwargs = dict(kwargs)
    slot_kwargs["source"] = catalog_entry.admission_source
    slot_kwargs["app_name"] = catalog_entry.app_id
    slot_kwargs["state"] = "reserved"
    return reserve_slot(
        Path(root),
        limit,
        **slot_kwargs,
    )


def _try_reserve_admission_slot(cfg: AppConfig) -> str | None:
    admission_token = _queue_module.try_reserve_admission_slot(cfg)
    if admission_token is None:
        logger.debug(
            "Queue worker admission paused: admission slots are full (admission_limit=%d)",
            cfg.runtime.resolved_admission_limit,
        )
    return admission_token


def _start_background_job_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: QueueEntry,
    admission_root: Any,
    admission_token: str,
) -> BackgroundRunJobProcess:
    del admission_root
    log_path = str(worker_log_path(queue_root, queue_entry_id(entry)))
    return start_background_process(
        build_worker_child_command(
            config_path=config_path,
            queue_root=queue_root,
            queue_id=queue_entry_id(entry),
            admission_token=admission_token,
        ),
        log_path=log_path,
    )


def _terminate_process(proc: _ManagedProcess) -> bool:
    """Terminate the background run process and escalate if it does not stop."""
    return terminate_process_group(proc)


def _tracking_callbacks() -> _tracking_helpers.OrcaQueueWorkerTrackingCallbacks:
    return _tracking_helpers.OrcaQueueWorkerTrackingCallbacks(
        build_run_finished_notification=build_run_finished_notification,
        coerce_resource_request=coerce_resource_request,
        finished_notification_already_sent=finished_notification_already_sent,
        load_report_json=load_report_json,
        load_state=load_state,
        mark_finished_notification_sent=mark_finished_notification_sent,
        notify_run_finished_event=notify_run_finished_event,
        queue_entry_metadata=queue_entry_metadata,
        queue_entry_reaction_dir=queue_entry_reaction_dir,
        queue_entry_task_id=queue_entry_task_id,
        read_resource_request_from_input=read_resource_request_from_input,
        record_from_artifacts=record_from_artifacts,
        resolve_job_metadata=resolve_job_metadata,
        resource_dict=resource_dict,
        selected_input_artifacts=selected_input_artifacts,
        upsert_job_record=upsert_job_record,
    )


def _get_run_id_from_state(
    reaction_dir: str,
    *,
    expected_job_id: str | None = None,
) -> str | None:
    return _tracking_helpers.get_run_id_from_state(
        reaction_dir,
        expected_job_id=expected_job_id,
        callbacks=_tracking_callbacks(),
    )


def _upsert_running_job_record(cfg: AppConfig, entry: QueueEntry) -> None:
    _tracking_helpers.upsert_running_job_record(
        cfg,
        entry,
        callbacks=_tracking_callbacks(),
    )


def _upsert_queued_job_record(cfg: AppConfig, entry: QueueEntry) -> None:
    _tracking_helpers.upsert_queued_job_record(
        cfg,
        entry,
        callbacks=_tracking_callbacks(),
    )


def _orca_publication_job_dir_issue(queue_root: Path, entry: QueueEntry) -> str:
    """Return why a new snapshot no longer names its submission directory."""

    reaction_dir_text = queue_entry_reaction_dir(entry)
    if not reaction_dir_text:
        return "reaction_dir_missing"
    try:
        lexical_reaction_dir = Path(reaction_dir_text).expanduser().absolute()
        resolved_queue_root = queue_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return "queue_or_reaction_path_invalid"
    if should_exclude_from_production_runs_scan(lexical_reaction_dir, resolved_queue_root):
        return "reaction_dir_reserved_or_unsafe"

    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, Mapping):
        # Legacy rows predate the bound directory identity. Keep their existing
        # repair behavior; their immutable input verification remains the final
        # execution boundary.
        return ""
    identity = snapshot.get("job_dir_identity")
    if identity is None:
        return ""
    if not isinstance(identity, Mapping):
        return "job_dir_identity_invalid"
    try:
        expected_identity = (
            int(identity.get("device", -1)),
            int(identity.get("inode", -1)),
        )
        resolved_reaction_dir = lexical_reaction_dir.resolve(strict=True)
        named_status = lexical_reaction_dir.lstat()
    except (OSError, RuntimeError, TypeError, ValueError):
        return "job_dir_identity_unverifiable"
    if (
        lexical_reaction_dir != resolved_reaction_dir
        or not stat.S_ISDIR(named_status.st_mode)
        or (int(named_status.st_dev), int(named_status.st_ino)) != expected_identity
    ):
        return "job_dir_namespace_or_identity_changed"
    if not resolved_reaction_dir.is_relative_to(resolved_queue_root):
        return "reaction_dir_outside_queue_root"
    if should_exclude_from_production_runs_scan(resolved_reaction_dir, resolved_queue_root):
        return "reaction_dir_reserved_or_unsafe"
    return ""


def _fence_invalid_orca_publication(
    queue_root: Path,
    entry: QueueEntry,
    *,
    issue: str,
) -> bool:
    """Terminally fence an exact pending row whose bound job path changed."""

    metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
    token = str(metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY) or "").strip()
    try:
        fenced = mark_failed(
            queue_root,
            entry.queue_id,
            error=f"queue_publication_job_dir_invalid:{issue}",
            publish_terminal_side_effects=False,
            metadata_update=queue_record_sync_metadata(
                QUEUE_RECORD_SYNC_ABORTED,
                token=token,
                owner_pid=0,
            ),
            expected_entry=entry,
            expected_task_id=entry.task_id,
        )
    except Exception:  # noqa: BLE001 - retain the row unclaimable on persistence failure
        logger.exception(
            "Failed to fence ORCA publication with changed job path: queue_id=%s issue=%s",
            entry.queue_id,
            issue,
        )
        return False
    if fenced:
        logger.error(
            "Fenced ORCA publication whose bound job path changed: queue_id=%s issue=%s",
            entry.queue_id,
            issue,
        )
        return True
    current = get_entry_by_id(queue_root, entry.queue_id)
    return bool(
        current is None or current.status != QueueStatus.PENDING or current.cancel_requested
    )


def _repair_orca_queue_publication(
    cfg: AppConfig,
    queue_root: Path,
    entry: QueueEntry,
) -> bool:
    """Repair one queued-index publication before the row can be claimed."""
    if not entry_matches_engine_identity(entry, "orca"):
        return True
    if entry.status != QueueStatus.PENDING or entry.cancel_requested:
        return True
    job_dir_issue = _orca_publication_job_dir_issue(queue_root, entry)
    if job_dir_issue:
        return _fence_invalid_orca_publication(
            queue_root,
            entry,
            issue=job_dir_issue,
        )
    reaction_dir_text = queue_entry_reaction_dir(entry)
    try:
        reaction_dir = Path(reaction_dir_text).expanduser().resolve()
        resolved_queue_root = queue_root.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    if not reaction_dir_text or not reaction_dir.is_relative_to(resolved_queue_root):
        return False
    for key in ("selected_inp", "selected_input_path", "selected_input_xyz"):
        selected_input_text = str(entry.metadata.get(key) or "").strip()
        if not selected_input_text:
            continue
        try:
            selected_input = Path(selected_input_text).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        if not selected_input.is_relative_to(reaction_dir):
            return False
    state = queue_record_sync_state(entry)
    if not state or state == QUEUE_RECORD_SYNC_COMPLETE:
        return True
    if state not in {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }:
        logger.error(
            "Cannot repair ORCA queue publication with invalid state %r: %s",
            state,
            queue_entry_id(entry),
        )
        return False
    queue_id = queue_entry_id(entry)
    with queue_record_publication_lock(queue_root, queue_id):
        current = get_entry_by_id(queue_root, queue_id)
        if current is None or current.status != QueueStatus.PENDING or current.cancel_requested:
            return True
        if not entry_matches_engine_identity(current, "orca"):
            return False
        current_reaction_dir_text = queue_entry_reaction_dir(current)
        try:
            current_reaction_dir = Path(current_reaction_dir_text).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        if (
            not current_reaction_dir_text
            or not current_reaction_dir.is_relative_to(resolved_queue_root)
            or current_reaction_dir != reaction_dir
            or queue_entry_task_id(current) != queue_entry_task_id(entry)
        ):
            return False
        state = queue_record_sync_state(current)
        if state == QUEUE_RECORD_SYNC_COMPLETE:
            return True
        if state not in {
            QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_REPAIR_PENDING,
            QUEUE_RECORD_SYNC_REPAIRING,
        }:
            logger.error(
                "Cannot repair ORCA queue publication after marker changed to invalid state %r: %s",
                state,
                queue_id,
            )
            return False
        # Acquiring the entry-scoped publication lock proves that no publisher
        # still owns this scope, even when a cleanup write failed and left the
        # long-lived publisher PID in PREPARING/REPAIRING metadata. Reclaim the
        # durable lease here instead of trusting process liveness forever.
        token = str(current.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY, "")).strip()
        if not token:
            logger.error("Cannot repair ORCA queue publication without a token: %s", queue_id)
            return False

        repairing, transitioned = transition_queue_record_publication(
            queue_root,
            queue_id,
            expected_token=token,
            expected_state=state,
            target_state=QUEUE_RECORD_SYNC_REPAIRING,
            owner_pid=os.getpid(),
            expected_entry=entry,
        )
        if not transitioned or repairing is None:
            return queue_record_sync_state(repairing) == QUEUE_RECORD_SYNC_COMPLETE
        try:
            _upsert_queued_job_record(cfg, repairing)
            completed, transitioned = transition_queue_record_publication(
                queue_root,
                queue_id,
                expected_token=token,
                expected_state=QUEUE_RECORD_SYNC_REPAIRING,
                target_state=QUEUE_RECORD_SYNC_COMPLETE,
                owner_pid=0,
                expected_entry=repairing,
            )
            return bool(
                completed is not None
                and (
                    transitioned or queue_record_sync_state(completed) == QUEUE_RECORD_SYNC_COMPLETE
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to repair ORCA queued publication: queue_id=%s", queue_id)
            try:
                transition_queue_record_publication(
                    queue_root,
                    queue_id,
                    expected_token=token,
                    expected_state=QUEUE_RECORD_SYNC_REPAIRING,
                    target_state=QUEUE_RECORD_SYNC_REPAIR_PENDING,
                    owner_pid=0,
                    expected_entry=repairing,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to return ORCA publication to repair_pending: queue_id=%s",
                    queue_id,
                )
            return False


def _repair_orca_queue_publications(worker: EngineQueueWorker) -> bool:
    repaired_all = True
    for queue_root in queue_roots(worker.cfg):
        try:
            entries = list_queue(queue_root)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to inspect ORCA publication repairs: %s", queue_root)
            repaired_all = False
            continue
        for entry in entries:
            if not _repair_orca_queue_publication(worker.cfg, queue_root, entry):
                repaired_all = False
    return repaired_all


def _upsert_terminal_job_record(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    fallback_job_id: str | None = None,
    expected_job_id: str | None = None,
) -> bool:
    callbacks = _tracking_callbacks()
    expected = str(expected_job_id or fallback_job_id or "").strip()
    if expected:
        load_state_fn = callbacks.load_state
        load_report_fn = callbacks.load_report_json

        def load_current_state(path: Path) -> Any:
            payload = load_state_fn(path)
            return (
                payload
                if _tracking_helpers.payload_matches_expected_job_id(payload, expected)
                else None
            )

        def load_current_report(path: Path) -> Any:
            payload = load_report_fn(path)
            return (
                payload
                if _tracking_helpers.payload_matches_expected_job_id(payload, expected)
                else None
            )

        # Cancellation can synthesize a new state while an old generation's
        # report still exists in the shared job directory.  Filter both artifact
        # sources independently so indexing cannot prefer that stale report.
        callbacks = replace(
            callbacks,
            load_state=load_current_state,
            load_report_json=load_current_report,
        )
    return _tracking_helpers.upsert_terminal_job_record(
        cfg,
        reaction_dir,
        fallback_job_id=fallback_job_id or "",
        callbacks=callbacks,
    )


def _notify_terminal_job_from_state(
    cfg: AppConfig,
    reaction_dir: str,
    *,
    expected_job_id: str | None = None,
) -> bool:
    return _tracking_helpers.notify_terminal_job_from_state(
        cfg,
        reaction_dir,
        expected_job_id=expected_job_id,
        callbacks=_tracking_callbacks(),
    )


def _worker_admission_limit(cfg: AppConfig, fallback_max_concurrent: int) -> int:
    raw_limit = cfg.runtime.admission_limit
    if raw_limit in (None, "", 0):
        raw_limit = fallback_max_concurrent
    return resolved_admission_limit(raw_limit, fallback_max_concurrent)


def _worker_config_with_effective_concurrency(
    cfg: AppConfig,
    configured_max: int,
) -> AppConfig:
    if cfg.runtime.admission_limit not in (None, "", 0):
        return cfg
    worker_cfg = copy.copy(cfg)
    worker_cfg.runtime = copy.copy(cfg.runtime)
    worker_cfg.runtime.max_concurrent = configured_max
    return worker_cfg


def _terminal_replay_blocks_new_generation(worker: Any) -> bool:
    pending_replays = getattr(worker, "_orca_pending_terminal_replays", None)
    if isinstance(pending_replays, dict) and any(
        isinstance(item, _TerminalReplayWorkItem) for item in pending_replays.values()
    ):
        return True
    for _queue_id, job in worker._running_jobs():
        if isinstance(getattr(job, "_orca_terminal_replay_item", None), _TerminalReplayWorkItem):
            return True
        if bool(getattr(job, _TERMINAL_FINALIZE_RETRY_ATTR, False)):
            return True
    return False


def _queue_entry_by_id(queue_root: Any, target_queue_id: str) -> QueueEntry | None:
    for entry in list_queue(Path(queue_root)):
        if queue_entry_id(entry) == target_queue_id and entry_matches_engine_identity(
            entry, "orca"
        ):
            return entry
    return None


def _reaction_generation_key(entry: Any) -> str | None:
    reaction_dir = queue_entry_reaction_dir(entry)
    if not reaction_dir:
        return None
    return str(Path(reaction_dir).expanduser().resolve())


def _expected_queue_entry(queue_root: Any, queue_id: str) -> QueueEntry | None:
    return _queue_entry_by_id(queue_root, queue_id)


def _mark_failed_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and mark_failed(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _mark_cancelled_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and mark_cancelled(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _mark_completed_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and mark_completed(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _cancel_requested_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and get_cancel_requested(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _requeue_running_expected(queue_root: Any, queue_id: str, **kwargs: Any) -> bool:
    expected = kwargs.pop("expected_entry", None) or _expected_queue_entry(queue_root, queue_id)
    return bool(
        expected is not None
        and requeue_running_entry(queue_root, queue_id, expected_entry=expected, **kwargs)
    )


def _lifecycle_callbacks() -> _lifecycle_helpers.OrcaQueueWorkerLifecycleCallbacks:
    return _lifecycle_helpers.OrcaQueueWorkerLifecycleCallbacks(
        queue_entry_id=queue_entry_id,
        queue_entry_app_name=queue_entry_app_name,
        queue_entry_task_id=queue_entry_task_id,
        update_slot_metadata=update_slot_metadata,
        terminate_process=_terminate_process,
        mark_failed=_mark_failed_expected,
        upsert_running_job_record=_upsert_running_job_record,
        get_run_id_from_state=_get_run_id_from_state,
        get_cancel_requested=_cancel_requested_expected,
        mark_cancelled=_mark_cancelled_expected,
        mark_completed=_mark_completed_expected,
        upsert_terminal_job_record=_upsert_terminal_job_record,
        notify_terminal_job_from_state=_notify_terminal_job_from_state,
        find_queue_entry=_queue_entry_by_id,
        on_completed=None,
        queue_roots=queue_roots,
        reconcile_stale_slots=reconcile_stale_slots,
        reconcile_orphaned_running_entries=reconcile_orphaned_running_entries,
        requeue_running_entry=_requeue_running_expected,
    )


def _orca_worker_lifecycle_hooks() -> EngineQueueProcessLifecycleHooks:
    return _lifecycle_helpers.build_orca_worker_lifecycle_hooks(_lifecycle_callbacks())


def _job_queue_root(worker: Any, job: Any) -> Path:
    return _lifecycle_job_queue_root(worker, job)


def _handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
) -> None:
    queue_id = queue_entry_id(entry)
    logger.error("Failed to start job %s: %s", queue_id, exc)
    worker._mark_entry_failed_and_release(
        queue_root,
        entry,
        admission_token,
        error=str(exc),
        mark_failed_fn=mark_failed,
    )


def _on_worker_process_started(
    worker: Any,
    queue_root: Path,
    entry: Any,
    process: BackgroundRunJobProcess,
    admission_token: str,
) -> bool:
    return _ENGINE_ADMISSION.attach_started_process_metadata(
        worker=worker,
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        hooks=_orca_worker_lifecycle_hooks(),
    )


def _finalize_finished_job(worker: Any, queue_id: str, job: _RunningJob, *, rc: int) -> None:
    # A child can exit while its engine process is still recorded as active.  Do
    # not publish a terminal queue state (or make the capacity reusable) until
    # that identity has been recovered.  Raising here deliberately leaves the
    # completed job in ``_running`` so the worker retries the whole finalization.
    job.__dict__[_TERMINAL_FINALIZE_RETRY_ATTR] = True
    recover_slot_engine_process(worker.admission_root, job.admission_token)
    pending_item = getattr(job, "_orca_terminal_replay_item", None)
    if isinstance(pending_item, _TerminalReplayWorkItem):
        release_slot_after_finalize = False
        try:
            _strictly_finish_terminal_replay(worker, job, pending_item)
            release_slot_after_finalize = True
        finally:
            if release_slot_after_finalize:
                worker._release_admission_slot(job.admission_token)
                job.__dict__.pop("_orca_terminal_replay_item", None)
                job.__dict__.pop(_TERMINAL_FINALIZE_RETRY_ATTR, None)
        return

    mark_result = mark_terminal_process_queue_entry_with_result(
        worker,
        queue_id,
        job,
        rc=rc,
        hooks=_orca_worker_lifecycle_hooks(),
    )
    release_slot_after_finalize = False
    try:
        # A no-op is benign only when another actor already moved or removed the
        # queue row. The mark result carries a pre-mark snapshot, so re-read before
        # deciding: an actually RUNNING row has no durable terminal owner and must
        # retain the job and slot for the supervised completion retry.
        current_after_mark = _queue_entry_by_id(mark_result.queue_root, queue_id)
        if _normalized_entry_status(current_after_mark) == STATUS_RUNNING:
            raise RuntimeError(
                "terminal queue mark did not update the running entry; "
                f"retaining retry ownership for {queue_id}"
            )
        marker = (
            _terminal_replay_marker_from_entry(current_after_mark)
            if current_after_mark is not None
            else None
        )
        if marker is not None:
            assert current_after_mark is not None
            reaction_dir = queue_entry_reaction_dir(current_after_mark)
            reaction_key = _reaction_generation_key(current_after_mark)
            if not reaction_dir or not reaction_key:
                raise RuntimeError(
                    f"terminal replay marker has no durable reaction identity: {queue_id}"
                )
            item = _new_terminal_replay_work_item(
                mark_result.queue_root,
                current_after_mark,
                reaction_dir=reaction_dir,
                reaction_key=reaction_key,
            )
            _strictly_finish_terminal_replay(worker, job, item)
        elif mark_result.marked:
            logger.info(
                "Terminal queue generation was already closed before finalizer replay: %s",
                queue_id,
            )
        release_slot_after_finalize = True
    finally:
        if release_slot_after_finalize:
            worker._release_admission_slot(job.admission_token)
            job.__dict__.pop("_orca_terminal_replay_item", None)
            job.__dict__.pop(_TERMINAL_FINALIZE_RETRY_ATTR, None)


def _finalize_completed_job(worker: Any, queue_id: str, job: Any, rc: int) -> None:
    _finalize_finished_job(worker, queue_id, job, rc=rc)


def _finalize_child_exit(worker: Any, job: _RunningJob, *, rc: int) -> None:
    _finalize_finished_job(worker, job.queue_id, job, rc=rc)


def _normalized_entry_status(entry: Any) -> str:
    raw_status = getattr(entry, "status", None)
    return str(getattr(raw_status, "value", raw_status) or "").strip().lower()


def _clear_terminal_replay_marker(item: _TerminalReplayWorkItem) -> bool:
    return update_queue_metadata(
        item.queue_root,
        item.queue_id,
        {_TERMINAL_REPLAY_METADATA_KEY: None},
    )


def _load_artifact_generation(reaction_key: str) -> _ArtifactGeneration:
    reaction_dir = Path(reaction_key)
    state_file = state_path(reaction_dir)
    state_existed = state_file.exists()
    try:
        state = load_state(reaction_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read ORCA state generation for %s: %s", reaction_dir, exc)
        return _ArtifactGeneration(readable=False)
    if (state_existed or state_file.exists()) and state is None:
        logger.warning("Failing closed on unreadable ORCA state generation: %s", state_file)
        return _ArtifactGeneration(readable=False)

    state_job_id = _tracking_helpers.payload_job_id(state)
    if state_job_id:
        # A new generation writes state before it writes a report.  The report can
        # therefore legitimately still belong to the previous generation.
        return _ArtifactGeneration(readable=True, state_job_id=state_job_id)

    report_file = report_json_path(reaction_dir)
    report_existed = report_file.exists()
    try:
        report = load_report_json(reaction_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read ORCA report generation for %s: %s", reaction_dir, exc)
        return _ArtifactGeneration(readable=False)
    if (report_existed or report_file.exists()) and report is None:
        logger.warning("Failing closed on unreadable ORCA report generation: %s", report_file)
        return _ArtifactGeneration(readable=False)
    return _ArtifactGeneration(
        readable=True,
        report_job_id=_tracking_helpers.payload_job_id(report),
    )


def _select_generation_owner(
    rows: list[_ReactionGenerationRow],
    *,
    previous_owner: tuple[str, str] | None,
    previous_owner_was_active: bool,
    artifacts: _ArtifactGeneration,
) -> tuple[str, str] | None:
    def choose(candidates: list[_ReactionGenerationRow]) -> tuple[str, str] | None:
        if len(candidates) == 1:
            return candidates[0].owner
        if previous_owner is not None and any(row.owner == previous_owner for row in candidates):
            return previous_owner
        task_ids = {row.task_id for row in candidates}
        if len(task_ids) == 1 and "" not in task_ids:
            return min(row.owner for row in candidates)
        return None

    # Any live generation fences every terminal replay for this reaction dir.
    # Multiple live entries are ambiguous: timestamps and queue ordering cannot
    # prove which child owns the shared artifacts.
    active_rows = [row for row in rows if row.active]
    if active_rows:
        return choose(active_rows) if len(active_rows) == 1 else None

    # A transition observed in this poll (or for the prior selected owner) is
    # stronger evidence than a stale state left by the preceding generation.
    transition_rows = [
        row
        for row in rows
        if row.transitioned_from_active
        or (row.owner == previous_owner and previous_owner_was_active)
    ]
    if transition_rows:
        if artifacts.state_job_id:
            matching_transition = [
                row for row in transition_rows if row.task_id == artifacts.state_job_id
            ]
            selected = choose(matching_transition)
            if selected is not None:
                return selected
        return choose(transition_rows)

    if not artifacts.readable:
        return None

    # State is authoritative.  A mismatching explicit identity means the visible
    # terminal entries do not own the current reaction-dir generation.
    if artifacts.state_job_id:
        return choose([row for row in rows if row.task_id == artifacts.state_job_id])

    pending_rows = [row for row in rows if row.pending_replay]
    if pending_rows:
        selected = choose(pending_rows)
        if selected is not None:
            return selected

    # A report can safely identify a completed generation because that replay
    # does not synthesize shared state.  Failed/cancelled rows still require
    # state or transition evidence: their report may be a previous generation's
    # leftover and selecting it would overwrite an identity-less current state.
    if artifacts.report_job_id:
        completed_report_rows = [
            row
            for row in rows
            if row.task_id == artifacts.report_job_id and row.status == STATUS_COMPLETED
        ]
        selected = choose(completed_report_rows)
        if selected is not None:
            return selected

    # Otherwise report identity is only a conflict check when one generation is
    # visible.
    if len(rows) == 1:
        only = rows[0]
        if artifacts.report_job_id and artifacts.report_job_id != only.task_id:
            return None
        return only.owner

    if len({row.task_id for row in rows}) == 1 and rows[0].task_id:
        if artifacts.report_job_id and artifacts.report_job_id != rows[0].task_id:
            return None
        return choose(rows)
    return None


def _new_terminal_replay_work_item(
    queue_root: Any,
    entry: Any,
    *,
    reaction_dir: str,
    reaction_key: str,
) -> _TerminalReplayWorkItem:
    metadata = queue_entry_metadata(entry)
    marker = _terminal_replay_marker_from_entry(entry)
    selected_inp = str(
        (marker or {}).get("selected_inp")
        or metadata.get("selected_inp")
        or metadata.get("selected_input_path")
        or ""
    ).strip()
    observed_state = _state_fingerprint_from_payload((marker or {}).get("observed_state"))
    if observed_state is None:
        observed_state = _load_state_generation_fingerprint(
            Path(reaction_dir).expanduser().resolve()
        )
    return _TerminalReplayWorkItem(
        queue_root=Path(queue_root).expanduser().resolve(),
        queue_id=queue_entry_id(entry),
        reaction_dir=reaction_dir,
        reaction_key=reaction_key,
        task_id=str((marker or {}).get("task_id") or queue_entry_task_id(entry) or "").strip(),
        observed_status=_normalized_entry_status(entry),
        selected_inp=selected_inp,
        error=str((marker or {}).get("error") or getattr(entry, "error", "") or "queue_failed"),
        recorded_run_id=str(metadata.get("run_id") or "").strip(),
        observed_state=observed_state,
    )


def _prepare_terminal_replay_work_item(
    item: _TerminalReplayWorkItem,
) -> _TerminalReplayWorkItem:
    run_id: str | None = None
    terminal_status: str | None = item.observed_status
    reaction_text = str(item.reaction_dir or "").strip()
    if not reaction_text:
        raise RuntimeError("terminal replay has no reaction directory")
    reaction_dir = Path(reaction_text).expanduser().resolve()
    if item.observed_status == STATUS_FAILED:
        run_id, terminal_status = _record_failed_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            reason=item.error,
            observed_state=item.observed_state,
        )
    elif item.observed_status == STATUS_CANCELLED:
        run_id, terminal_status = _record_cancelled_run_state(
            reaction_dir,
            fallback_job_id=item.task_id,
            selected_inp=item.selected_inp,
            observed_state=item.observed_state,
        )
    resolved_status = (
        terminal_status
        if terminal_status in (STATUS_COMPLETED, STATUS_CANCELLED)
        else STATUS_FAILED
    )
    return replace(
        item,
        resolved_status=resolved_status,
        run_id=run_id,
        state_prepared=True,
    )


def _run_terminal_replay_side_effects(
    worker: Any,
    item: _TerminalReplayWorkItem,
) -> None:
    if not str(item.reaction_dir or "").strip():
        raise RuntimeError("terminal replay has no reaction directory")
    record_upserted = _upsert_terminal_job_record(
        worker.cfg,
        item.reaction_dir,
        fallback_job_id=item.task_id,
        expected_job_id=item.task_id,
    )
    if not record_upserted:
        raise RuntimeError("terminal job record artifacts are not ready")
    _notify_terminal_job_from_state(
        worker.cfg,
        item.reaction_dir,
        expected_job_id=item.task_id,
    )
    if worker.cfg.messenger.enabled:
        replayed_state = load_state(Path(item.reaction_dir).expanduser().resolve())
        if (
            not replayed_state
            or not _tracking_helpers.payload_matches_expected_job_id(
                replayed_state,
                item.task_id,
            )
            or not finished_notification_already_sent(replayed_state)
        ):
            raise RuntimeError("terminal notification was not durably recorded")


def _clear_terminal_replay_marker_or_confirm_absent(item: _TerminalReplayWorkItem) -> None:
    if _clear_terminal_replay_marker(item):
        return
    current = _queue_entry_by_id(item.queue_root, item.queue_id)
    if current is None or _terminal_replay_marker_from_entry(current) is None:
        return
    raise RuntimeError(
        f"terminal replay marker remained after successful side effects: queue_id={item.queue_id}"
    )


def _strictly_finish_terminal_replay(
    worker: Any,
    job: _RunningJob,
    item: _TerminalReplayWorkItem,
) -> None:
    """Finish one durable terminal generation before making its slot reusable."""

    job.__dict__["_orca_terminal_replay_item"] = item
    if _pending_replay_state_is_superseded(item):
        _clear_terminal_replay_marker_or_confirm_absent(item)
        return

    prepared_item = item if item.state_prepared else _prepare_terminal_replay_work_item(item)
    job.__dict__["_orca_terminal_replay_item"] = prepared_item
    if prepared_item.resolved_status != item.observed_status or (
        prepared_item.run_id and prepared_item.recorded_run_id != prepared_item.run_id
    ):
        updated = update_terminal(
            prepared_item.queue_root,
            prepared_item.queue_id,
            prepared_item.resolved_status,
            run_id=prepared_item.run_id,
            expected_task_id=prepared_item.task_id,
        )
        if not updated:
            logger.info(
                "Queue entry disappeared after terminal state preparation; "
                "continuing side effects: queue_id=%s",
                prepared_item.queue_id,
            )
        prepared_item = replace(
            prepared_item,
            recorded_run_id=prepared_item.run_id or prepared_item.recorded_run_id,
        )
        job.__dict__["_orca_terminal_replay_item"] = prepared_item

    _run_terminal_replay_side_effects(worker, prepared_item)
    _clear_terminal_replay_marker_or_confirm_absent(prepared_item)


def _pending_replay_state_is_superseded(item: _TerminalReplayWorkItem) -> bool:
    if not str(item.reaction_dir or "").strip():
        return True
    if not item.task_id:
        return True
    current = _load_state_generation_fingerprint(Path(item.reaction_dir).expanduser().resolve())
    if not current.readable:
        return False
    if current.readable and current.job_id == item.task_id:
        if (
            item.observed_state is not None
            and item.observed_state.readable
            and item.observed_state.job_id
            and item.observed_state.job_id != item.task_id
            and not current.terminal_status
        ):
            return True
        expected_run_id = str(item.run_id or item.recorded_run_id or "").strip()
        if (
            not expected_run_id
            and item.observed_state is not None
            and item.observed_state.job_id == item.task_id
        ):
            expected_run_id = item.observed_state.run_id
        return bool(expected_run_id and current.run_id and current.run_id != expected_run_id)
    if item.observed_state is not None:
        if not item.observed_state.readable:
            return False
        if current != item.observed_state:
            return True
        return bool(
            current.readable
            and current.job_id
            and current.job_id != item.task_id
            and not current.terminal_status
        )
    return bool(current.job_id and current.job_id != item.task_id)


def _reconcile_orphaned_running(worker: Any) -> None:
    recover_orphaned_engine_slots(worker.admission_root, strict=False)
    before_entries = queue_entries_with_roots(worker.cfg)
    before_by_key = {
        (str(Path(root).expanduser().resolve()), queue_entry_id(entry)): entry
        for root, entry in before_entries
    }
    previous_statuses = getattr(worker, "_orca_reconcile_statuses", None)
    if not isinstance(previous_statuses, dict):
        # Process startup has no observed status edge.  Treat the first queue
        # snapshot as the replay cursor instead of inventing RUNNING origins for
        # historical terminal rows.  A terminal row that really has unfinished
        # side effects remains replayable through its durable marker below, while
        # lifecycle reconciliation can still expose a real active -> terminal edge
        # between ``before_entries`` and ``after_entries`` in this same poll.
        previous_statuses = {
            key: _normalized_entry_status(entry) for key, entry in before_by_key.items()
        }
    protected_queue_keys, protected_queue_ids = live_queue_slot_keys_for_slots(
        worker.admission_root,
        list_slots_fn=list_slots,
    )
    _lifecycle_helpers.reconcile_orphaned_running(
        worker,
        callbacks=_lifecycle_callbacks(),
        protected_queue_keys=protected_queue_keys,
        protected_queue_ids=protected_queue_ids,
    )
    # Reconciliation can terminalize a job whose original parent died, and an
    # old child can also honor cancellation directly. Replay the normal
    # terminal side effects idempotently so job-location records and one-shot
    # notifications are not lost with the parent process.
    after_entries = queue_entries_with_roots(worker.cfg)
    pending_replays = getattr(worker, "_orca_pending_terminal_replays", None)
    if not isinstance(pending_replays, dict):
        pending_replays = {}
    pending_replays = {
        key: item
        for key, item in pending_replays.items()
        if isinstance(key, tuple) and isinstance(item, _TerminalReplayWorkItem)
    }
    raw_blocked_markers: object = getattr(worker, "_orca_invalid_terminal_replay_markers", set())
    previously_blocked_markers: set[tuple[str, str]] = (
        {
            key
            for key in raw_blocked_markers
            if isinstance(key, tuple)
            and len(key) == 2
            and all(isinstance(part, str) for part in key)
        }
        if isinstance(raw_blocked_markers, set)
        else set()
    )
    blocked_marker_keys: set[tuple[str, str]] = set()
    for queue_root, entry in after_entries:
        if _normalized_entry_status(entry) not in _TERMINAL_QUEUE_STATUSES:
            continue
        resolved_root = str(Path(queue_root).expanduser().resolve())
        key = (resolved_root, queue_entry_id(entry))
        marker_kind = terminal_replay_marker_kind(entry)
        if marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED:
            blocked_marker_keys.add(key)
            if key not in previously_blocked_markers:
                logger.error(
                    "ORCA terminal replay is repair-blocked by an invalid or unsupported "
                    "durable marker; retaining the queue generation from clear/force: "
                    "queue_id=%s queue_root=%s",
                    queue_entry_id(entry),
                    resolved_root,
                )
            continue
        if marker_kind is not TerminalReplayMarkerKind.VALID:
            continue
        reaction_key = _reaction_generation_key(entry)
        if reaction_key is None:
            continue
        durable_item = _new_terminal_replay_work_item(
            queue_root,
            entry,
            reaction_dir=queue_entry_reaction_dir(entry),
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(key)
        if not (
            isinstance(existing_item, _TerminalReplayWorkItem)
            and existing_item.task_id == durable_item.task_id
            and existing_item.reaction_key == durable_item.reaction_key
        ):
            pending_replays[key] = durable_item
    worker._orca_invalid_terminal_replay_markers = blocked_marker_keys

    superseded_replay_keys: set[tuple[str, str]] = set()
    for key, item in list(pending_replays.items()):
        if _pending_replay_state_is_superseded(item):
            # Revalidate prepared snapshots before owner selection even while the
            # terminal queue row still exists.  Otherwise a newer state identity
            # can make selection return ``None`` and strand the old snapshot in
            # the pending map forever.
            try:
                _clear_terminal_replay_marker(item)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to clear superseded ORCA terminal replay marker: %s",
                    item.queue_id,
                )
                continue
            pending_replays.pop(key, None)
            superseded_replay_keys.add(key)

    generation_rows: dict[str, list[_ReactionGenerationRow]] = {}
    current_generation_keys: set[tuple[str, str]] = set()
    for queue_root, entry in after_entries:
        reaction_key = _reaction_generation_key(entry)
        if reaction_key is None:
            continue
        resolved_root = str(Path(queue_root).expanduser().resolve())
        queue_id = queue_entry_id(entry)
        owner = (resolved_root, queue_id)
        before_entry = before_by_key.get(owner)
        before_status = _normalized_entry_status(before_entry)
        pending_item = pending_replays.get(owner)
        pending_replay = isinstance(pending_item, _TerminalReplayWorkItem)
        current_generation_keys.add(owner)
        marker_kind = terminal_replay_marker_kind(entry)
        if _normalized_entry_status(entry) in _TERMINAL_QUEUE_STATUSES and (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            # Administrative fences own no run artifact generation.  An
            # invalid/unsupported marker is likewise excluded so neither an
            # observed edge nor a stale in-memory snapshot can bypass its
            # repair-blocked boundary.
            pending_replays.pop(owner, None)
            continue
        generation_rows.setdefault(reaction_key, []).append(
            _ReactionGenerationRow(
                owner=owner,
                task_id=str(queue_entry_task_id(entry) or "").strip(),
                status=_normalized_entry_status(entry),
                # If state preparation failed after this generation was selected,
                # keep the observed active -> terminal edge with its immutable
                # snapshot.  The next poll otherwise sees a terminal -> terminal
                # row and can incorrectly hand ownership back to stale artifacts.
                # Once preparation succeeds, state identity becomes authoritative
                # and a mismatch correctly supersedes this pending replay.
                transitioned_from_active=(
                    before_status in _ACTIVE_QUEUE_STATUSES
                    or (
                        isinstance(pending_item, _TerminalReplayWorkItem)
                        and not pending_item.state_prepared
                    )
                ),
                pending_replay=pending_replay,
            )
        )
    for key, item in pending_replays.items():
        if key in current_generation_keys:
            continue
        generation_rows.setdefault(item.reaction_key, []).append(
            _ReactionGenerationRow(
                owner=key,
                task_id=item.task_id,
                status=item.resolved_status or item.observed_status,
                pending_replay=True,
            )
        )

    previous_owners = getattr(worker, "_orca_generation_owners", None)
    if not isinstance(previous_owners, dict):
        previous_owners = {}
    previous_owner_active = getattr(worker, "_orca_generation_owner_active", None)
    if not isinstance(previous_owner_active, dict):
        previous_owner_active = {}
    latest_generation_by_reaction: dict[str, tuple[str, str]] = {}
    latest_owner_active: dict[str, bool] = {}
    for reaction_key, rows in generation_rows.items():
        previous_owner = previous_owners.get(reaction_key)
        selected_owner = _select_generation_owner(
            rows,
            previous_owner=previous_owner if isinstance(previous_owner, tuple) else None,
            previous_owner_was_active=bool(previous_owner_active.get(reaction_key, False)),
            artifacts=_load_artifact_generation(reaction_key),
        )
        if selected_owner is None:
            continue
        latest_generation_by_reaction[reaction_key] = selected_owner
        selected_row = next(row for row in rows if row.owner == selected_owner)
        latest_owner_active[reaction_key] = selected_row.active
    worker._orca_generation_owners = latest_generation_by_reaction
    worker._orca_generation_owner_active = latest_owner_active
    worker._orca_generation_seen_keys = current_generation_keys

    after_statuses: dict[tuple[str, str], str] = {}
    for queue_root, entry in after_entries:
        queue_id = queue_entry_id(entry)
        key = (str(Path(queue_root).expanduser().resolve()), queue_id)
        status = _normalized_entry_status(entry)
        after_statuses[key] = status
        if status not in _TERMINAL_QUEUE_STATUSES:
            pending_replays.pop(key, None)
            continue
        marker_kind = terminal_replay_marker_kind(entry)
        if (
            terminal_replay_is_fence_only(entry)
            or marker_kind is TerminalReplayMarkerKind.INVALID_OR_UNSUPPORTED
        ):
            pending_replays.pop(key, None)
            continue
        if key in superseded_replay_keys:
            # The newer generation identity is definitive.  Advance the cursor
            # to this row's terminal status so it is not reconsidered forever.
            after_statuses[key] = status
            continue
        before_entry = before_by_key.get(key)
        before_status = _normalized_entry_status(before_entry)
        # Replay requires positive evidence: either a durable replay marker (or
        # an in-memory retry snapshot), an active row terminalized during this
        # reconciliation, or an active status observed by the prior poll.  A
        # terminal row first seen after startup is closed history, not proof of a
        # transition; replaying it can rewrite its state/run identity and resend
        # an old notification.
        observed_active_transition = (
            before_status in _ACTIVE_QUEUE_STATUSES
            or previous_statuses.get(key) in _ACTIVE_QUEUE_STATUSES
        )
        if key not in pending_replays and not observed_active_transition:
            continue
        reaction_dir = queue_entry_reaction_dir(entry)
        if not reaction_dir:
            continue
        reaction_key = _reaction_generation_key(entry) or ""
        if latest_generation_by_reaction.get(reaction_key) != key:
            logger.debug(
                "Skipping terminal replay for superseded or ambiguous ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                queue_id,
                reaction_dir,
            )
            # Ambiguity is retryable: state/report identity may become durable on
            # the next poll without another queue status transition.
            after_statuses[key] = "running"
            if reaction_key in latest_generation_by_reaction:
                pending_replays.pop(key, None)
            continue

        new_item = _new_terminal_replay_work_item(
            queue_root,
            entry,
            reaction_dir=reaction_dir,
            reaction_key=reaction_key,
        )
        existing_item = pending_replays.get(key)
        if isinstance(
            existing_item, _TerminalReplayWorkItem
        ) and _pending_replay_state_is_superseded(existing_item):
            _clear_terminal_replay_marker(existing_item)
            pending_replays.pop(key, None)
            after_statuses[key] = status
            continue
        item = (
            existing_item
            if isinstance(existing_item, _TerminalReplayWorkItem)
            and existing_item.reaction_key == new_item.reaction_key
            and existing_item.task_id == new_item.task_id
            else new_item
        )
        pending_replays[key] = item
        try:
            if not item.state_prepared:
                item = _prepare_terminal_replay_work_item(item)
                pending_replays[key] = item
            if item.resolved_status != status or (
                item.run_id and item.recorded_run_id != item.run_id
            ):
                updated = update_terminal(
                    item.queue_root,
                    item.queue_id,
                    item.resolved_status,
                    run_id=item.run_id,
                    expected_task_id=item.task_id,
                )
                if not updated:
                    logger.info(
                        "Queue entry disappeared after terminal state preparation; "
                        "continuing side effects: queue_id=%s",
                        item.queue_id,
                    )
            _run_terminal_replay_side_effects(worker, item)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to replay terminal side effects for reconciled ORCA job %s",
                queue_id,
            )
            # Keep this transition pending so the next periodic reconcile
            # retries the idempotent terminal side effects.
            after_statuses[key] = "running"
        else:
            try:
                _clear_terminal_replay_marker(item)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to clear completed ORCA terminal replay marker: %s",
                    item.queue_id,
                )
                after_statuses[key] = "running"
            else:
                pending_replays.pop(key, None)
                after_statuses[key] = item.resolved_status

    # A queue clear can remove the entry after state synthesis but before an
    # upsert/notification succeeds.  Retry from the immutable snapshot, including
    # a preparation that failed before the entry disappeared, but only after the
    # current owner and artifact generation are revalidated on every attempt.
    for key, item in list(pending_replays.items()):
        if key in current_generation_keys:
            continue
        if _pending_replay_state_is_superseded(item):
            logger.info(
                "Dropping terminal replay superseded by a newer ORCA generation: "
                "queue_id=%s reaction_dir=%s",
                item.queue_id,
                item.reaction_dir,
            )
            pending_replays.pop(key, None)
            continue
        selected_owner = latest_generation_by_reaction.get(item.reaction_key)
        if selected_owner != key:
            if selected_owner is not None and selected_owner != key:
                pending_replays.pop(key, None)
            continue
        try:
            if not item.state_prepared:
                item = _prepare_terminal_replay_work_item(item)
                pending_replays[key] = item
            _run_terminal_replay_side_effects(worker, item)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to retry terminal side effects after queue entry disappeared: %s",
                item.queue_id,
            )
        else:
            pending_replays.pop(key, None)

    worker._orca_pending_terminal_replays = pending_replays
    worker._orca_reconcile_statuses = after_statuses


def _reconcile_worker_state(worker: Any) -> None:
    _reconcile_orphaned_running(worker)


def _shutdown_running_job(worker: Any, queue_id: str, job: Any) -> None:
    if get_cancel_requested(
        _job_queue_root(worker, job),
        queue_id,
        expected_task_id=job.task_id,
    ):
        # A cancel landed before the worker loop could process it proactively. The
        # shared requeue chokepoint would still honor it (mark the entry cancelled
        # instead of requeuing for resume) but skip the terminal side effects --
        # the cancelled run state and the notification. Route it through the same
        # finalize path as a proactive cancel so the user is told it stopped and no
        # stale "running" run state lingers.
        _cancel_orca_running_job(worker, queue_id, job)
        return
    callbacks = _lifecycle_callbacks()

    def terminate_child_and_recover(process: Any) -> bool:
        terminated = callbacks.terminate_process(process)
        if terminated is True and process.poll() is not None:
            recover_slot_engine_process(worker.admission_root, job.admission_token)
        return terminated

    _lifecycle_helpers.shutdown_running_job(
        worker,
        queue_id,
        job,
        callbacks=replace(callbacks, terminate_process=terminate_child_and_recover),
    )


def _before_shutdown_all(_worker: Any, running_count: int) -> None:
    logger.info("Shutting down %d running job(s)...", running_count)


def _queue_worker_hooks() -> Any:
    return _queue_module.queue_worker_hooks()


def _after_orca_worker_init(worker: EngineQueueWorker) -> None:
    worker.admission_limit = _worker_admission_limit(worker.cfg, worker.max_concurrent)
    reserve_next_entry = worker._reserve_next_entry

    def reserve_next_after_terminal_replay() -> tuple[str, Any | None]:
        if _terminal_replay_blocks_new_generation(worker):
            logger.warning("Queue admission paused until durable ORCA terminal replay completes")
            return "blocked", None
        if not _repair_orca_queue_publications(worker):
            logger.warning("Queue admission paused until ORCA queued publication repair succeeds")
            return "blocked", None
        return reserve_next_entry()

    # A failed terminal side effect retains its completed job and durable marker.
    # Gate admission itself (not merely slot release): max_concurrent > 1 and
    # multi-root queues could otherwise start a forced successor in another slot.
    worker.__dict__["_reserve_next_entry"] = reserve_next_after_terminal_replay


def _before_orca_worker_run(worker: EngineQueueWorker) -> None:
    _runtime_helpers.before_worker_run(worker)


def _after_orca_worker_run(_worker: EngineQueueWorker) -> None:
    _runtime_helpers.after_worker_run(_worker)


def _log_orca_worker_interrupt(_worker: EngineQueueWorker) -> None:
    _runtime_helpers.log_worker_interrupt(_worker)


def _make_orca_running_job(
    _worker: EngineQueueWorker,
    *,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
) -> _RunningJob:
    return _runtime_helpers.make_running_job(
        queue_root=queue_root,
        entry=entry,
        process=process,
        admission_token=admission_token,
        queue_entry_id_fn=queue_entry_id,
        queue_entry_reaction_dir_fn=queue_entry_reaction_dir,
        queue_entry_task_id_fn=queue_entry_task_id,
        running_job_cls=_RunningJob,
    )


def _check_orca_cancel_requests(worker: EngineQueueWorker) -> None:
    _runtime_helpers.check_cancel_requests(
        worker,
        get_cancel_requested_fn=get_cancel_requested,
        job_queue_root_fn=_job_queue_root,
        cancel_running_job_fn=_cancel_orca_running_job,
    )


def _load_state_for_terminal_generation(
    job_dir: Path,
    *,
    expected_job_id: str,
    observed_state: _StateGenerationFingerprint | None = None,
) -> RunState | None:
    state_file = state_path(job_dir)
    state_existed = state_file.exists()
    state = load_state(job_dir)
    if (state_existed or state_file.exists()) and state is None:
        raise RuntimeError(f"ORCA run state is unreadable: {state_file}")
    if state is None:
        current_fingerprint = _StateGenerationFingerprint(present=False, readable=True)
        if observed_state is not None and current_fingerprint != observed_state:
            raise _TerminalReplaySupersededError(
                "ORCA terminal replay state disappeared after its queue mark: "
                f"job_dir={job_dir} expected_job_id={expected_job_id}"
            )
        return None
    if not expected_job_id:
        return state

    state_job_id = _tracking_helpers.payload_job_id(state)
    existing_terminal_status = _terminal_status_from_run_state(state)
    if state_job_id == expected_job_id:
        if (
            observed_state is not None
            and observed_state.readable
            and observed_state.job_id
            and observed_state.job_id != expected_job_id
            and existing_terminal_status is None
        ):
            raise _TerminalReplaySupersededError(
                "ORCA terminal replay observed a new run for the expected task "
                "after marking a different state generation: "
                f"job_dir={job_dir} expected_job_id={expected_job_id} "
                f"observed_job_id={observed_state.job_id}"
            )
        if (
            observed_state is not None
            and observed_state.readable
            and observed_state.job_id == expected_job_id
            and observed_state.run_id
            and str(state.get("run_id") or "").strip()
            and str(state.get("run_id") or "").strip() != observed_state.run_id
        ):
            raise _TerminalReplaySupersededError(
                "ORCA terminal replay observed a newer run for the same task: "
                f"job_dir={job_dir} expected_job_id={expected_job_id}"
            )
        return state

    if observed_state is not None:
        current_fingerprint = _StateGenerationFingerprint(
            present=True,
            readable=True,
            job_id=state_job_id,
            run_id=str(state.get("run_id") or "").strip(),
            terminal_status=existing_terminal_status or "",
        )
        if current_fingerprint != observed_state:
            raise _TerminalReplaySupersededError(
                "ORCA terminal replay was superseded by another state generation: "
                f"job_dir={job_dir} expected_job_id={expected_job_id} "
                f"state_job_id={state_job_id or '<missing>'}"
            )
        if state_job_id and state_job_id != expected_job_id and not existing_terminal_status:
            raise _TerminalReplaySupersededError(
                "ORCA terminal replay observed another active state generation: "
                f"job_dir={job_dir} expected_job_id={expected_job_id} "
                f"state_job_id={state_job_id}"
            )
    if existing_terminal_status is not None:
        # A forced submission can reuse a reaction directory before the new
        # child writes state.  A complete terminal result is durable evidence
        # that this is the previous generation, so synthesize a fresh state for
        # the expected queue task instead of relabeling the old result.
        logger.info(
            "Ignoring previous-generation terminal ORCA state: "
            "job_dir=%s expected_job_id=%s state_job_id=%s",
            job_dir,
            expected_job_id,
            state_job_id,
        )
        return None

    # A nonterminal mismatch can be the current active generation.  Failing
    # closed under the run lock is essential: overwriting it would make an old
    # finalizer publish a terminal result for a newer child.
    raise RuntimeError(
        "ORCA run state belongs to a different active generation: "
        f"job_dir={job_dir} expected_job_id={expected_job_id} "
        f"state_job_id={state_job_id or '<missing>'}"
    )


def _record_cancelled_run_state(
    job_dir: Path,
    *,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    observed_state: _StateGenerationFingerprint | None = None,
) -> tuple[str | None, str | None]:
    """Write a terminal "cancelled" run state for an interrupted run.

    A cancelled run is stopped by a signal and never writes its own terminal
    result, so the run state lingers as ``running``. That leaves a stale run
    snapshot in the activity list and starves the terminal Telegram notification
    (which requires ``final_result``). Persist a cancelled outcome here.

    Returns ``(run_id, terminal_status)``: the run_id when known (so the queue
    entry can be matched to this snapshot) and the terminal status now recorded
    in the run state -- "cancelled" when we wrote it, or a pre-existing terminal
    status we refused to clobber. When no state exists yet, a minimal cancelled
    state is created from the queue identity so indexing cannot fall back to
    ``unknown``.
    """
    expected_job_id = str(fallback_job_id or "").strip()
    with acquire_run_lock(job_dir):
        state = _load_state_for_terminal_generation(
            job_dir,
            expected_job_id=expected_job_id,
            observed_state=observed_state,
        )
        if state is None:
            selected_text = str(selected_inp or "").strip()
            selected_path = Path(selected_text).expanduser() if selected_text else job_dir / "-"
            if not selected_path.is_absolute():
                selected_path = job_dir / selected_path
            state = new_state(job_dir, selected_path, max_retries=0)
            if expected_job_id:
                state["job_id"] = expected_job_id
        run_id = str(state.get("run_id") or "").strip() or None
        final_result = state.get("final_result")
        if isinstance(final_result, dict):
            existing_status = str(final_result.get("status") or "").strip()
            if existing_status in {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED}:
                # A real terminal outcome was already recorded (e.g. the run finished
                # just before cancellation landed); do not clobber it, and report the
                # real status so the queue entry is reconciled to what actually
                # happened instead of being mislabeled "cancelled".
                if str(state.get("status") or "").strip() != existing_status:
                    finalize_state(
                        job_dir,
                        state,
                        status=existing_status,
                        final_result=final_result,
                    )
                return run_id, existing_status
        cancelled_result = build_final_result(
            status=STATUS_CANCELLED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason="cancel_requested",
            last_out_path=last_out_path_from_state(state),
        )
        finalize_state(job_dir, state, status=STATUS_CANCELLED, final_result=cancelled_result)
        return run_id, STATUS_CANCELLED


def _record_failed_run_state(
    job_dir: Path,
    *,
    fallback_job_id: str | None = None,
    selected_inp: str | None = None,
    reason: str,
    observed_state: _StateGenerationFingerprint | None = None,
) -> tuple[str | None, str | None]:
    """Ensure an exited child has a terminal state for its queue generation."""

    expected_job_id = str(fallback_job_id or "").strip()
    with acquire_run_lock(job_dir):
        state = _load_state_for_terminal_generation(
            job_dir,
            expected_job_id=expected_job_id,
            observed_state=observed_state,
        )
        if state is None:
            selected_text = str(selected_inp or "").strip()
            selected_path = Path(selected_text).expanduser() if selected_text else job_dir / "-"
            if not selected_path.is_absolute():
                selected_path = job_dir / selected_path
            state = new_state(job_dir, selected_path, max_retries=0)
            if expected_job_id:
                state["job_id"] = expected_job_id
        run_id = str(state.get("run_id") or "").strip() or None
        final_result = state.get("final_result")
        if isinstance(final_result, dict):
            existing_status = str(final_result.get("status") or "").strip()
            if existing_status in {STATUS_COMPLETED, STATUS_CANCELLED, STATUS_FAILED}:
                if str(state.get("status") or "").strip() != existing_status:
                    finalize_state(
                        job_dir,
                        state,
                        status=existing_status,
                        final_result=final_result,
                    )
                return run_id, existing_status
        failed_result = build_final_result(
            status=STATUS_FAILED,
            analyzer_status=AnalyzerStatus.INCOMPLETE,
            reason=reason,
            last_out_path=last_out_path_from_state(state),
        )
        finalize_state(job_dir, state, status=STATUS_FAILED, final_result=failed_result)
        return run_id, STATUS_FAILED


def _finalize_cancelled_run(
    worker: EngineQueueWorker,
    job: _RunningJob,
    *,
    entry_snapshot: Any | None = None,
    observed_state: _StateGenerationFingerprint | None = None,
) -> bool:
    """Record the cancelled outcome and notify, mirroring terminal finalization.

    The proactive cancel path stops the child and marks the queue entry cancelled
    but, unlike a natural exit, never runs the terminal side effects. Do them here
    so the cancelled job leaves the active queue list and the user is told it
    stopped.
    """
    reaction_dir = job.reaction_dir.strip()
    if not reaction_dir:
        return False
    queue_root = _job_queue_root(worker, job)
    entry = (
        entry_snapshot
        if entry_snapshot is not None
        else _queue_entry_by_id(queue_root, job.queue_id)
    )
    metadata = queue_entry_metadata(entry) if entry is not None else {}
    selected_inp = str(
        metadata.get("selected_inp") or metadata.get("selected_input_path") or ""
    ).strip()
    task_id = (queue_entry_task_id(entry) if entry is not None else None) or job.task_id
    try:
        record_kwargs: dict[str, Any] = {
            "fallback_job_id": task_id,
            "selected_inp": selected_inp,
        }
        if observed_state is not None:
            record_kwargs["observed_state"] = observed_state
        run_id, terminal_status = _record_cancelled_run_state(
            Path(reaction_dir).expanduser().resolve(),
            **record_kwargs,
        )
        if terminal_status:
            # Reconcile the queue entry to the outcome actually recorded in the run
            # state and stamp run_id so the activity view matches the snapshot to
            # this record instead of listing it twice. cancel_running_process_job
            # already marked the entry "cancelled"; if the run had in fact finished
            # just before the cancel landed, correct it to the real terminal status
            # so the entry, snapshot, and notification all agree.
            queue_status = (
                terminal_status
                if terminal_status in (STATUS_COMPLETED, STATUS_CANCELLED)
                else STATUS_FAILED
            )
            updated = update_terminal(
                queue_root,
                job.queue_id,
                queue_status,
                run_id=run_id,
                expected_task_id=job.task_id,
            )
            if not updated:
                logger.info(
                    "Queue entry disappeared after cancelled state preparation; "
                    "continuing side effects: queue_id=%s",
                    job.queue_id,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to finalize cancelled run state for %s: %s", reaction_dir, exc)
        return False

    return True


def _cancel_orca_running_job(worker: EngineQueueWorker, queue_id: str, job: _RunningJob) -> bool:
    hooks = _orca_worker_lifecycle_hooks()
    queue_root = _job_queue_root(worker, job)

    def terminate_child_and_recover(process: Any) -> bool:
        terminated = hooks.terminate_process_fn(process)
        if terminated is True and process.poll() is not None:
            job.__dict__[_TERMINAL_FINALIZE_RETRY_ATTR] = True
            recover_slot_engine_process(worker.admission_root, job.admission_token)
        return terminated

    try:
        cancelled = cancel_running_process_job(
            worker,
            queue_id,
            job,
            hooks=replace(
                hooks,
                terminate_process_fn=terminate_child_and_recover,
            ),
            release_admission_slot=False,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to durably mark cancelled ORCA job %s; retaining retry ownership",
            queue_id,
        )
        return False
    if not cancelled:
        return False
    terminal_entry = _queue_entry_by_id(queue_root, queue_id)
    if _normalized_entry_status(terminal_entry) == STATUS_RUNNING:
        logger.error(
            "Cancellation returned without a durable terminal queue transition: %s",
            queue_id,
        )
        return False
    marker = (
        _terminal_replay_marker_from_entry(terminal_entry) if terminal_entry is not None else None
    )
    if marker is None:
        # Another owner may already have completed and cleared this exact
        # generation while the stale cancellation snapshot was in flight.
        worker._release_admission_slot(job.admission_token)
        job.__dict__.pop("_orca_terminal_replay_item", None)
        job.__dict__.pop(_TERMINAL_FINALIZE_RETRY_ATTR, None)
        return True
    assert terminal_entry is not None
    reaction_dir = queue_entry_reaction_dir(terminal_entry)
    reaction_key = _reaction_generation_key(terminal_entry)
    if not reaction_dir or not reaction_key:
        logger.error("Durable cancellation marker has no reaction identity: %s", queue_id)
        return False
    replay_item = _new_terminal_replay_work_item(
        queue_root,
        terminal_entry,
        reaction_dir=reaction_dir,
        reaction_key=reaction_key,
    )
    release_slot = False
    try:
        _strictly_finish_terminal_replay(worker, job, replay_item)
        release_slot = True
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to finish durable cancellation replay for %s; retaining retry ownership",
            queue_id,
        )
        return False
    finally:
        if release_slot:
            worker._release_admission_slot(job.admission_token)
            job.__dict__.pop("_orca_terminal_replay_item", None)
            job.__dict__.pop(_TERMINAL_FINALIZE_RETRY_ATTR, None)


def QueueWorker(
    cfg: AppConfig,
    config_path: str,
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> EngineQueueWorker:
    configured_max = max(1, int(max_concurrent))
    worker_cfg = _worker_config_with_effective_concurrency(cfg, configured_max)
    worker = build_runtime_engine_queue_worker(
        worker_cfg,
        config_path=config_path,
        default_config_path=_default_config_path,
        engine="orca",
        max_concurrent=max_concurrent,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=WORKER_PID_FILE,
        admission_root=_admission_root_for_cfg(worker_cfg),
        after_init=_after_orca_worker_init,
        before_run=_before_orca_worker_run,
        after_run=_after_orca_worker_run,
        keyboard_interrupt=_log_orca_worker_interrupt,
        running_queue_id=queue_entry_id,
        running_job_factory=_make_orca_running_job,
        finalize_finished_job=_finalize_finished_job,
        reconcile_orphaned_running=_reconcile_orphaned_running,
        check_cancel_requests=_check_orca_cancel_requests,
        normalize_max_concurrent=True,
        worker_builder=build_engine_queue_worker,
    )
    _runtime_helpers.install_worker_runtime_methods(
        worker,
        cancel_running_job_fn=_cancel_orca_running_job,
    )
    return worker


def read_worker_pid(allowed_root: Path) -> int | None:
    """Read the worker PID file. Returns None if not found or stale."""
    return _queue_module.read_worker_pid(allowed_root)
