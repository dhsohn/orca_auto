from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import replace
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
from orca_auto.core.engines.queue_worker import (
    EngineQueueWorker,
    build_engine_queue_worker,
    build_runtime_engine_queue_worker,
)
from orca_auto.core.queue import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_REPAIRING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QueueStatus,
    list_queue,
    mark_cancelled,
    mark_failed,
    queue_record_publication_lock,
    queue_record_sync_is_stale,
    queue_record_sync_metadata,
    queue_record_sync_state,
)
from orca_auto.core.queue.generation import queue_entries_same_generation
from orca_auto.core.queue.internal_engine import (
    InternalEngineQueueModule,
    InternalEngineQueueWorkerDeps,
    InternalEngineSpec,
    entry_matches_engine_identity,
)
from orca_auto.core.queue.store import mutate_entries
from orca_auto.core.queue.worker import (
    config_path_for_worker,
    resolve_admission_root,
    start_background_process,
    terminate_process_group,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.persistence import timestamped_token

from .engine import ENGINE_DEFINITION, build_worker_child_command
from .job_locations import runtime_roots_for_cfg
from .records import build_job_artifact, persist_failed_job, persist_job_artifact
from .submission import publish_queued_record

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
WORKER_SHUTDOWN_GRACE_SECONDS = 15.0
WORKER_PID_FILE = "xtb_md_queue_worker.pid"
_ENGINE_SPEC = InternalEngineSpec(
    engine="xtb_md",
    worker_job_module="orca_auto.xtb_md.execution",
    worker_pid_file_name=WORKER_PID_FILE,
)


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


_REPAIRABLE_SYNC_STATES = frozenset(
    {
        QUEUE_RECORD_SYNC_PREPARING,
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        QUEUE_RECORD_SYNC_REPAIRING,
    }
)


def _mark_repair_pending(queue_root: Path, entry: Any, *, expected_token: str) -> None:
    """Fence a possibly-committed repair lease back to the explicit repair queue."""

    def park(entries: list[Any]) -> tuple[None, bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if (
                current.status != QueueStatus.PENDING
                or queue_record_sync_state(current) != QUEUE_RECORD_SYNC_REPAIRING
                or normalize_text(current.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY))
                != expected_token
            ):
                return None, False
            metadata = dict(current.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIR_PENDING,
                    token=expected_token,
                    owner_pid=0,
                )
            )
            entries[index] = replace(current, metadata=metadata)
            return None, True
        return None, False

    try:
        mutate_entries(queue_root, park)
    except BaseException:  # noqa: BLE001 - never mask the original failure
        logger.warning(
            "failed to park xTB-MD queued record as repair pending: queue_id=%s",
            getattr(entry, "queue_id", ""),
            exc_info=True,
        )


def _repair_queued_publication(cfg: Any, queue_root: Path, entry: Any) -> bool:
    """Re-publish one committed xTB-MD row whose queued record never landed.

    Claims the row under the publication lock with a fresh token (the lock,
    not a live PID in the row, is the authoritative ownership proof), writes
    the queued job artifact, and marks the sync lease COMPLETE. Any failure
    parks the row as REPAIR_PENDING so it stays unclaimable rather than
    running without its published record.
    """
    repair_token = timestamped_token("record_sync", token_bytes=16)

    def claim(entries: list[Any]) -> tuple[tuple[str, Any | None], bool]:
        for index, current in enumerate(entries):
            if current.queue_id != entry.queue_id:
                continue
            if not queue_entries_same_generation(current, entry):
                return ("identity_changed", current), False
            if current.cancel_requested:
                return ("cancelled", current), False
            sync_state = queue_record_sync_state(current)
            if sync_state == QUEUE_RECORD_SYNC_COMPLETE:
                return ("complete", current), False
            if current.status != QueueStatus.PENDING:
                return ("terminal", current), False
            if sync_state not in _REPAIRABLE_SYNC_STATES:
                return ("invalid_state", current), False
            metadata = dict(current.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_REPAIRING,
                    token=repair_token,
                    owner_pid=os.getpid(),
                )
            )
            updated = replace(current, metadata=metadata)
            entries[index] = updated
            return ("claimed", updated), True
        return ("missing", None), False

    def complete(entries: list[Any]) -> tuple[None, bool]:
        for index, row in enumerate(entries):
            if row.queue_id != entry.queue_id:
                continue
            # The COMPLETE transition belongs to this repair lease only: any
            # other sync state or token here (for example a cancel fence
            # written concurrently) must never be overwritten.
            if (
                row.status != QueueStatus.PENDING
                or row.cancel_requested
                or queue_record_sync_state(row) != QUEUE_RECORD_SYNC_REPAIRING
                or normalize_text(row.metadata.get(QUEUE_RECORD_SYNC_TOKEN_KEY)) != repair_token
            ):
                raise RuntimeError(
                    "xTB-MD queued record repair lost publication ownership: "
                    f"queue_id={entry.queue_id}"
                )
            metadata = dict(row.metadata)
            metadata.update(
                queue_record_sync_metadata(
                    QUEUE_RECORD_SYNC_COMPLETE,
                    token=repair_token,
                    owner_pid=0,
                )
            )
            entries[index] = replace(row, metadata=metadata)
            return None, True
        raise RuntimeError(f"xTB-MD queue entry disappeared during repair: {entry.queue_id}")

    claimed = False
    try:
        # One lock acquisition covers claim, publication, and completion, so no
        # cancel fence or foreign publication can interleave between them.
        with queue_record_publication_lock(queue_root, entry.queue_id):
            outcome, current = mutate_entries(queue_root, claim)
            if outcome != "claimed":
                if outcome == "invalid_state":
                    logger.error(
                        "Cannot repair xTB-MD queue publication with invalid state %r: queue_id=%s",
                        queue_record_sync_state(current),
                        entry.queue_id,
                    )
                    return False
                if outcome == "identity_changed":
                    logger.warning(
                        "xTB-MD queued record repair refused a changed queue generation: "
                        "queue_id=%s",
                        entry.queue_id,
                    )
                    return False
                # complete / cancelled / terminal / missing: nothing left to repair.
                return True
            claimed = True
            publish_queued_record(cfg, current)
            mutate_entries(queue_root, complete)
    except BaseException as exc:  # noqa: BLE001
        # Even a failed claim may have committed before its durability barrier
        # reported failure; park the lease so the row cannot strand in
        # REPAIRING (the park CAS is token-gated, so it never touches a row
        # this repair does not own).
        _mark_repair_pending(queue_root, entry, expected_token=repair_token)
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "xTB-MD queued record repair %s: queue_id=%s",
            "failed" if claimed else "claim failed",
            entry.queue_id,
            exc_info=True,
        )
        return False
    logger.info("repaired xTB-MD queued record publication: queue_id=%s", entry.queue_id)
    return True


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
        if sync_state not in _REPAIRABLE_SYNC_STATES:
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


def _forbidden_requeue(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("xTB-MD retry/requeue is not supported")


def _activate_slot(
    admission_root: str | Path,
    admission_token: str,
    **metadata: Any,
) -> object | None:
    return activate_reserved_slot(admission_root, admission_token, **metadata)


def _deps() -> InternalEngineQueueWorkerDeps:
    return InternalEngineQueueWorkerDeps(
        time_module=time,
        release_slot=release_slot,
        reserve_slot=reserve_slot,
        start_background_process=start_background_process,
        build_worker_child_command=build_worker_child_command,
        config_path_for_worker=config_path_for_worker,
        default_config_path=default_config_path,
        activate_reserved_slot=_activate_slot,
        terminate_process=terminate_process_group,
        mark_failed=lambda root, queue_id, **kwargs: mark_failed(root, queue_id, **kwargs),
        handle_worker_start_error=_handle_worker_start_error,
        finalize_completed_job=_finalize_completed_job,
        finalize_child_exit=_finalize_child_exit,
        reconcile_worker_state=_reconcile_worker_state,
        list_queue=list_queue,
        list_slots=list_slots,
        reconcile_stale_slots=reconcile_stale_slots,
        mark_cancelled=lambda root, queue_id, **kwargs: mark_cancelled(root, queue_id, **kwargs),
        requeue_running_entry=_forbidden_requeue,
        find_queue_entry=_find_entry,
        load_config=load_config,
    )


_queue_module = InternalEngineQueueModule.create_from_definition(
    definition=ENGINE_DEFINITION,
    spec=_ENGINE_SPEC,
    poll_interval_seconds=POLL_INTERVAL_SECONDS,
    shutdown_grace_seconds=WORKER_SHUTDOWN_GRACE_SECONDS,
    deps=_deps(),
    runtime_roots_for_cfg=runtime_roots_for_cfg,
)

queue_roots = _queue_module.queue_roots
queue_entries_with_roots = _queue_module.queue_entries_with_roots
dequeue_next_entry = _queue_module.dequeue_next_entry


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
        engine="xtb_md",
        max_concurrent=max_concurrent,
        deps=_queue_module.queue_worker_deps(),
        hooks=_queue_module.queue_worker_hooks(),
        worker_pid_file_name=WORKER_PID_FILE,
        admission_root=resolve_admission_root(cfg),
        finalize_child_exit=_finalize_child_exit,
        reconcile_orphaned_running=_reconcile_worker_state,
        reserve_gate=_publication_repair_gate,
        normalize_max_concurrent=True,
        worker_builder=build_engine_queue_worker,
    )


def cmd_queue_worker(args: Any) -> int:
    return _queue_module.run_pidfile_worker_command(
        args,
        config_path_fn=lambda parsed: str(parsed.config),
        load_config_fn=load_config,
        read_worker_pid_fn=_queue_module.read_worker_pid,
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
