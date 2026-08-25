from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import open_confined_log
from orca_auto.core.statuses import STATUS_CANCELLED, normalize_status

LOGGER = logging.getLogger(__name__)


def start_background_process(
    command: Sequence[str],
    *,
    log_path: str | Path | None = None,
) -> subprocess.Popen[str]:
    if log_path is None:
        return subprocess.Popen(
            list(command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )

    resolved_log_path = Path(log_path).expanduser()
    resolved_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open_confined_log(
        resolved_log_path.parent,
        resolved_log_path,
        label="background worker log",
        append=True,
    ) as log_handle:
        return subprocess.Popen(
            list(command),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )


def live_queue_slot_keys_for_slots(
    admission_root: str | Path,
    *,
    list_slots_fn: Callable[[str | Path], list[Any]],
) -> tuple[set[tuple[str, str]], set[str]]:
    scoped_keys: set[tuple[str, str]] = set()
    unscoped_ids: set[str] = set()
    for slot in list_slots_fn(admission_root):
        queue_id = str(getattr(slot, "queue_id", "")).strip()
        if not queue_id:
            continue
        work_dir = _normalized_work_dir(getattr(slot, "work_dir", ""))
        if work_dir:
            scoped_keys.add((queue_id, work_dir))
        else:
            unscoped_ids.add(queue_id)
    return scoped_keys, unscoped_ids


def _normalized_work_dir(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())


def _queue_entry_work_dir(entry: Any) -> str:
    metadata = getattr(entry, "metadata", {})
    if isinstance(metadata, dict):
        for key in ("job_dir", "reaction_dir", "work_dir"):
            work_dir = _normalized_work_dir(metadata.get(key))
            if work_dir:
                return work_dir
    for attr in ("work_dir", "reaction_dir", "job_dir"):
        work_dir = _normalized_work_dir(getattr(entry, attr, ""))
        if work_dir:
            return work_dir
    return ""


def _entry_has_live_slot(
    entry: Any,
    *,
    queue_id: str,
    scoped_live_slots: set[tuple[str, str]],
    unscoped_live_queue_ids: set[str],
) -> bool:
    work_dir = _queue_entry_work_dir(entry)
    if not work_dir:
        return any(live_queue_id == queue_id for live_queue_id, _ in scoped_live_slots) or (
            queue_id in unscoped_live_queue_ids
        )
    if (queue_id, work_dir) in scoped_live_slots:
        return True
    scoped_ids = {live_queue_id for live_queue_id, _ in scoped_live_slots}
    return queue_id in unscoped_live_queue_ids and queue_id not in scoped_ids


def requeue_result_is_cancelled(result: Any) -> bool:
    """True when a requeue attempt reports the entry as already cancelled.

    Shared by every requeue-on-recovery path (orphan reconcile, worker
    shutdown, child exit): a cancelled entry must not be marked
    recovery-pending, or it would be resurrected on the next worker start.
    """
    status = getattr(result, "status", None)
    status = getattr(status, "value", status)
    return normalize_status(status) == STATUS_CANCELLED


def status_matches(value: Any, expected: Any) -> bool:
    actual_value = getattr(value, "value", value)
    expected_value = getattr(expected, "value", expected)
    return str(actual_value).strip().lower() == str(expected_value).strip().lower()


def reconcile_orphaned_child_queue_entries(
    cfg: Any,
    *,
    admission_root: str | Path,
    queue_roots_fn: Callable[[Any], tuple[Path, ...]],
    list_queue_fn: Callable[[str | Path], list[Any]],
    list_slots_fn: Callable[[str | Path], list[Any]],
    reconcile_stale_slots_fn: Callable[[str | Path], object],
    running_status: Any,
    mark_cancelled_fn: Callable[..., object],
    requeue_running_entry_fn: Callable[..., object],
    mark_recovery_pending_fn: Callable[[Any, Any], object],
) -> None:
    reconcile_stale_slots_fn(admission_root)
    scoped_live_slots, unscoped_live_queue_ids = live_queue_slot_keys_for_slots(
        admission_root,
        list_slots_fn=list_slots_fn,
    )

    for queue_root in queue_roots_fn(cfg):
        for entry in list_queue_fn(queue_root):
            if not status_matches(getattr(entry, "status", None), running_status):
                continue
            queue_id = str(getattr(entry, "queue_id", ""))
            if _entry_has_live_slot(
                entry,
                queue_id=queue_id,
                scoped_live_slots=scoped_live_slots,
                unscoped_live_queue_ids=unscoped_live_queue_ids,
            ):
                continue
            generation_kwargs = {"expected_entry": entry}
            task_id = str(getattr(entry, "task_id", "") or "").strip()
            if task_id:
                generation_kwargs["expected_task_id"] = task_id
            if getattr(entry, "cancel_requested", False):
                mark_cancelled_fn(
                    queue_root,
                    queue_id,
                    error="cancel_requested",
                    **generation_kwargs,
                )
            else:
                updated = requeue_running_entry_fn(
                    queue_root,
                    queue_id,
                    **generation_kwargs,
                )
                if updated is None or requeue_result_is_cancelled(updated):
                    continue
                mark_recovery_pending_fn(cfg, entry)


def shutdown_child_process_with_grace(
    job: Any,
    *,
    finalize_child_exit_fn: Callable[[Any, int], object],
    grace_seconds: float,
    sleep_fn: Callable[[float], None],
) -> bool:
    # The child may be deliberately retaining admission ownership while it
    # cleans up an engine process in a separate session.  Only a plain
    # ``terminate`` is sent here and no force-kill escalation exists: killing
    # the owner can orphan that engine and make the parent release the only
    # durable ownership record.
    if job.process.poll() is None:
        try:
            job.process.terminate()
        except Exception:  # noqa: BLE001
            LOGGER.debug("failed to terminate child worker process", exc_info=True)

    deadline = time.monotonic() + grace_seconds
    while job.process.poll() is None and time.monotonic() < deadline:
        sleep_fn(0.1)

    returncode = job.process.poll()
    if returncode is None:
        LOGGER.error(
            "Child worker still owns a live engine cleanup after shutdown grace; "
            "retaining queue state and admission ownership (child_pid=%s)",
            getattr(job.process, "pid", "unknown"),
        )
        return False

    finalize_child_exit_fn(job, int(returncode))
    return True


__all__ = [
    "live_queue_slot_keys_for_slots",
    "reconcile_orphaned_child_queue_entries",
    "requeue_result_is_cancelled",
    "shutdown_child_process_with_grace",
    "start_background_process",
    "status_matches",
]
