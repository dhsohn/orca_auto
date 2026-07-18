from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    activate_reserved_slot,
    get_slot,
    list_slots,
    reconcile_stale_slots,
    recover_orphaned_engine_slots,
    recover_slot_engine_process,
    release_slot,
    reserve_slot,
)
from orca_auto.core.config.engines import (
    default_shared_config_path as default_config_path,
)
from orca_auto.core.config.engines import load_xtb_md_config as load_config
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QueueStatus,
    list_queue,
    mark_cancelled,
    mark_failed,
    queue_record_sync_is_stale,
    queue_record_sync_state,
)
from orca_auto.core.queue.enqueue_publication import (
    REPAIRABLE_SYNC_STATES,
    repair_enqueue_publication,
)
from orca_auto.core.queue.generation import queue_entries_same_generation
from orca_auto.core.queue.worker import (
    resolve_admission_root,
    start_background_process,
    terminate_process_group,
)
from orca_auto.core.utils.coercion import normalize_text

from .engine import ENGINE_DEFINITION, build_worker_child_command
from .job_locations import runtime_roots_for_cfg
from .records import build_job_artifact, persist_failed_job, persist_job_artifact
from .submission import publish_queued_record

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
WORKER_SHUTDOWN_GRACE_SECONDS = 15.0
_ENGINE_RUNTIME = ENGINE_DEFINITION.build_queue_runtime()


def _find_entry(queue_root: Path, queue_id: str) -> Any | None:
    matches = [
        entry
        for entry in list_queue(queue_root)
        if str(getattr(entry, "queue_id", "") or "") == queue_id
        and entry_matches_engine_identity(entry, "xtb_md")
    ]
    return matches[0] if len(matches) == 1 else None


def _status_text(entry: Any) -> str:
    raw = getattr(entry, "status", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _persist_cancelled(cfg: Any, entry: Any, *, reason: str) -> None:
    payload = build_job_artifact(
        entry,
        status="cancelled",
        reason=reason,
        exit_code=None,
    )
    persist_job_artifact(cfg, entry, payload)


def _terminalize_abandoned(
    cfg: Any,
    queue_root: Path,
    entry: Any,
    *,
    reason: str,
) -> None:
    generation_kwargs = {
        "expected_entry": entry,
        "expected_task_id": entry.task_id,
    }
    if bool(getattr(entry, "cancel_requested", False)):
        try:
            _persist_cancelled(cfg, entry, reason="cancel_requested")
        except Exception:  # noqa: BLE001
            pass
        mark_cancelled(
            queue_root,
            entry.queue_id,
            error="cancel_requested",
            require_cancel_requested=True,
            **generation_kwargs,
        )
        return
    try:
        persist_failed_job(cfg, entry, reason=reason)
    except Exception:  # noqa: BLE001
        pass
    updated = mark_failed(
        queue_root,
        entry.queue_id,
        error=reason,
        **generation_kwargs,
    )
    if updated is not None:
        return
    current = _find_entry(queue_root, entry.queue_id)
    if (
        current is None
        or not queue_entries_same_generation(current, entry)
        or not bool(getattr(current, "cancel_requested", False))
    ):
        return
    try:
        _persist_cancelled(cfg, current, reason="cancel_requested")
    except Exception:  # noqa: BLE001
        pass
    mark_cancelled(
        queue_root,
        current.queue_id,
        error="cancel_requested",
        require_cancel_requested=True,
        expected_entry=current,
        expected_task_id=current.task_id,
    )


def _recover_job_engine_process(worker: Any, job: Any) -> None:
    slot = get_slot(worker.admission_root, job.admission_token)
    if slot is None:
        return
    recover_slot_engine_process(worker.admission_root, job.admission_token)


def _finalize_child_exit(worker: Any, job: Any, *, rc: int) -> None:
    _recover_job_engine_process(worker, job)
    current = _find_entry(job.queue_root, job.entry.queue_id)
    try:
        if current is not None and _status_text(current) == "running":
            reason = (
                "worker_shutdown_no_retry"
                if bool(getattr(worker, "_shutdown_requested", False))
                else f"worker_child_exit_code={rc}"
            )
            _terminalize_abandoned(
                worker.cfg,
                job.queue_root,
                current,
                reason=reason,
            )
    finally:
        worker._release_admission_slot(job.admission_token)


def _finalize_completed_job(worker: Any, _queue_id: str, job: Any, rc: int) -> None:
    _finalize_child_exit(worker, job, rc=rc)


def _handle_worker_start_error(
    worker: Any,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
) -> None:
    reason = f"worker_start_error:{exc}"[:1000]
    try:
        persist_failed_job(worker.cfg, entry, reason=reason)
    except Exception:  # noqa: BLE001
        pass
    worker._mark_entry_failed_and_release(
        queue_root,
        entry,
        admission_token,
        error=reason,
        mark_failed_fn=mark_failed,
    )


def _live_slot_queue_ids(admission_root: str | Path) -> set[str]:
    return {
        str(getattr(slot, "queue_id", "") or "").strip()
        for slot in list_slots(admission_root)
        if str(getattr(slot, "queue_id", "") or "").strip()
    }


def _reconcile_worker_state(worker: Any) -> None:
    recover_orphaned_engine_slots(worker.admission_root, strict=False)
    reconcile_stale_slots(worker.admission_root)
    live_queue_ids = _live_slot_queue_ids(worker.admission_root)
    for queue_root in runtime_roots_for_cfg(worker.cfg):
        for entry in list_queue(queue_root):
            if not entry_matches_engine_identity(entry, "xtb_md"):
                continue
            if _status_text(entry) != "running" or entry.queue_id in live_queue_ids:
                continue
            _terminalize_abandoned(
                worker.cfg,
                queue_root,
                entry,
                reason="orphaned_worker_no_retry",
            )


def _repair_queued_publication(cfg: Any, queue_root: Path, entry: Any) -> bool:
    """Re-publish one committed xTB-MD row through the shared repair driver."""

    return repair_enqueue_publication(
        queue_root,
        entry,
        publish=lambda current: publish_queued_record(cfg, current),
        label="xTB-MD",
    )


def _repair_xtb_md_queue_publications(worker: Any) -> bool:
    """Repair every xTB-MD row whose enqueue committed but publication did not.

    A publisher killed between the durable enqueue commit and the queued-record
    publication leaves a PREPARING lease that eventually goes stale and would
    otherwise be claimed and run without any published record. Fresh
    PREPARING/REPAIRING leases with a live owner are left alone: they are not
    claimable, and the live publisher keeps ownership.
    """
    repaired_all = True
    for queue_root, entry in queue_entries_with_roots(worker.cfg):
        if not entry_matches_engine_identity(entry, "xtb_md"):
            continue
        if entry.status != QueueStatus.PENDING or entry.cancel_requested:
            continue
        sync_state = queue_record_sync_state(entry)
        if not sync_state or sync_state == QUEUE_RECORD_SYNC_COMPLETE:
            continue
        if sync_state not in REPAIRABLE_SYNC_STATES:
            logger.error(
                "Cannot repair xTB-MD queue publication with invalid state %r: queue_id=%s",
                sync_state,
                entry.queue_id,
            )
            repaired_all = False
            continue
        if sync_state != QUEUE_RECORD_SYNC_REPAIR_PENDING and not queue_record_sync_is_stale(entry):
            # A live publisher still owns this lease; the row is unclaimable
            # until the lease goes stale, so there is nothing to repair yet.
            continue
        raw_job_dir = normalize_text(entry.metadata.get("job_dir"))
        try:
            job_dir_contained = bool(raw_job_dir) and (
                Path(raw_job_dir)
                .expanduser()
                .resolve()
                .is_relative_to(queue_root.expanduser().resolve())
            )
        except (OSError, RuntimeError):
            job_dir_contained = False
        if not job_dir_contained:
            logger.error(
                "Cannot repair xTB-MD publication outside its queue root: queue_id=%s job_dir=%s",
                entry.queue_id,
                raw_job_dir,
            )
            repaired_all = False
            continue
        try:
            repaired = _repair_queued_publication(worker.cfg, queue_root, entry)
        except Exception:  # noqa: BLE001
            logger.warning(
                "xTB-MD queued record repair raised: queue_id=%s",
                entry.queue_id,
                exc_info=True,
            )
            repaired = False
        if not repaired:
            repaired_all = False
    return repaired_all


def _publication_repair_gate(worker: Any) -> tuple[str, Any | None] | None:
    if not _repair_xtb_md_queue_publications(worker):
        logger.warning(
            "xTB-MD queue admission paused: a queued-record publication could not be repaired"
        )
        return ("blocked", None)
    return None


def _activate_slot(
    admission_root: str | Path,
    admission_token: str,
    **metadata: Any,
) -> object | None:
    return activate_reserved_slot(admission_root, admission_token, **metadata)


def _try_reserve_admission_slot(cfg: Any) -> str | None:
    return _ENGINE_RUNTIME.reserve_admission_slot(
        cfg,
        engine=ENGINE_DEFINITION.engine,
        reserve_slot_fn=reserve_slot,
    )


def _start_background_job_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: Any,
    admission_root: str | Path,
    admission_token: str,
) -> Any:
    return _ENGINE_RUNTIME.start_child_process(
        config_path=config_path,
        queue_root=queue_root,
        entry=entry,
        admission_root=admission_root,
        admission_token=admission_token,
        start_background_process_fn=start_background_process,
        build_worker_child_command_fn=build_worker_child_command,
        include_admission_root=False,
    )


def _queue_worker_deps() -> Any:
    return _ENGINE_RUNTIME.child_worker_deps(
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        time_module=time,
        release_slot_fn=release_slot,
        start_background_job_process_fn=_start_background_job_process,
        try_reserve_admission_slot_fn=_try_reserve_admission_slot,
    )


def _queue_worker_hooks() -> Any:
    return _ENGINE_RUNTIME.child_worker_hooks(
        engine=ENGINE_DEFINITION.engine,
        handle_worker_start_error_fn=_handle_worker_start_error,
        finalize_completed_job_fn=_finalize_completed_job,
        finalize_child_exit_fn=_finalize_child_exit,
        reconcile_worker_state_fn=_reconcile_worker_state,
        activate_reserved_slot_fn=_activate_slot,
        terminate_process_fn=terminate_process_group,
        mark_failed_fn=lambda root, queue_id, **kwargs: mark_failed(root, queue_id, **kwargs),
        shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
        sleep_fn=time.sleep,
    )


queue_roots = _ENGINE_RUNTIME.queue_roots
queue_entries_with_roots = _ENGINE_RUNTIME.queue_entries_with_roots
dequeue_next_entry = _ENGINE_RUNTIME.dequeue_next_entry


def QueueWorker(
    cfg: Any,
    config_path: str | None = None,
    *,
    max_concurrent: int | None = None,
) -> EngineQueueWorker:
    return build_runtime_engine_queue_worker(
        cfg,
        config_path=config_path,
        default_config_path=default_config_path,
        engine=ENGINE_DEFINITION.engine,
        max_concurrent=max_concurrent,
        deps=_queue_worker_deps(),
        hooks=_queue_worker_hooks(),
        worker_pid_file_name=_ENGINE_RUNTIME.worker_pid_file_name,
        admission_root=resolve_admission_root(cfg),
        finalize_child_exit=_finalize_child_exit,
        reconcile_orphaned_running=_reconcile_worker_state,
        reserve_gate=_publication_repair_gate,
        normalize_max_concurrent=True,
        worker_builder=build_engine_queue_worker,
    )


def cmd_queue_worker(args: Any) -> int:
    return _ENGINE_RUNTIME.run_pidfile_worker_command(
        args,
        config_path_fn=lambda parsed: str(parsed.config),
        load_config_fn=load_config,
        read_worker_pid_fn=_ENGINE_RUNTIME.read_worker_pid,
        worker_factory=lambda cfg, config_path, **kwargs: QueueWorker(
            cfg,
            config_path,
            **kwargs,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m orca_auto.xtb_md.queue_runtime")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    return cmd_queue_worker(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "QueueWorker",
    "build_parser",
    "cmd_queue_worker",
    "dequeue_next_entry",
    "main",
    "queue_entries_with_roots",
    "queue_roots",
]
