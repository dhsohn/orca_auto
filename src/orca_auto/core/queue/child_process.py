from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Sequence

LOGGER = logging.getLogger(__name__)


def build_background_worker_command(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    worker_job_module: str,
    admission_root: str | Path | None = None,
    admission_token: str | None = None,
    include_admission_root: bool = True,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        worker_job_module,
        "--config",
        config_path,
        "--queue-root",
        str(queue_root),
        "--queue-id",
        queue_id,
    ]
    if include_admission_root:
        if admission_root is None:
            raise ValueError("admission_root is required when include_admission_root is true")
        command.extend(["--admission-root", str(admission_root)])
    if admission_token:
        command.extend(["--admission-token", admission_token])
    return command


def start_background_job_process(
    *,
    config_path: str,
    queue_root: str | Path,
    entry: Any,
    worker_job_module: str,
    admission_root: str | Path | None = None,
    admission_token: str | None = None,
    include_admission_root: bool = True,
    log_path: str | Path | None = None,
) -> subprocess.Popen[str]:
    command = build_background_worker_command(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=entry.queue_id,
        worker_job_module=worker_job_module,
        admission_root=admission_root,
        admission_token=admission_token,
        include_admission_root=include_admission_root,
    )
    if log_path is None:
        return start_background_process(command)
    return start_background_process(command, log_path=log_path)


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
    with resolved_log_path.open("a", encoding="utf-8") as log_handle:
        return subprocess.Popen(
            list(command),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )


def live_queue_ids_for_slots(
    admission_root: str | Path,
    *,
    list_slots_fn: Callable[[str | Path], list[Any]],
) -> set[str]:
    return {
        str(getattr(slot, "queue_id", "")).strip()
        for slot in list_slots_fn(admission_root)
        if str(getattr(slot, "queue_id", "")).strip()
    }


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
    live_queue_ids = live_queue_ids_for_slots(admission_root, list_slots_fn=list_slots_fn)

    for queue_root in queue_roots_fn(cfg):
        for entry in list_queue_fn(queue_root):
            if not status_matches(getattr(entry, "status", None), running_status):
                continue
            queue_id = str(getattr(entry, "queue_id", ""))
            if queue_id in live_queue_ids:
                continue
            if getattr(entry, "cancel_requested", False):
                mark_cancelled_fn(queue_root, queue_id, error="cancel_requested")
            else:
                requeue_running_entry_fn(queue_root, queue_id)
                mark_recovery_pending_fn(cfg, entry)


def shutdown_child_process_with_grace(
    job: Any,
    *,
    terminate_process_fn: Callable[[Any], object],
    finalize_child_exit_fn: Callable[[Any, int], object],
    grace_seconds: float,
    sleep_fn: Callable[[float], None],
) -> None:
    if job.process.poll() is None:
        try:
            job.process.terminate()
        except Exception:  # noqa: BLE001
            LOGGER.debug("failed to terminate child worker process", exc_info=True)

    deadline = time.monotonic() + grace_seconds
    while job.process.poll() is None and time.monotonic() < deadline:
        sleep_fn(0.1)

    if job.process.poll() is None:
        terminate_process_fn(job.process)

    rc = job.process.poll()
    finalize_child_exit_fn(job, int(rc) if rc is not None else 0)


def request_job_cancellation(
    proc: Any,
    *,
    cancel_signal: int,
    terminate_process_fn: Callable[[Any], None],
) -> None:
    try:
        pid = getattr(proc, "pid", None)
        if pid is not None:
            try:
                os.killpg(pid, cancel_signal)
                return
            except (OSError, ProcessLookupError, PermissionError):
                pass

        _send_process_cancellation_signal(proc, cancel_signal)
    except (OSError, ProcessLookupError, PermissionError):
        terminate_process_fn(proc)


def _send_process_cancellation_signal(proc: Any, cancel_signal: int) -> None:
    send_signal = getattr(proc, "send_signal", None)
    if callable(send_signal):
        send_signal(cancel_signal)
        return
    os.kill(proc.pid, cancel_signal)


__all__ = [
    "build_background_worker_command",
    "live_queue_ids_for_slots",
    "reconcile_orphaned_child_queue_entries",
    "request_job_cancellation",
    "shutdown_child_process_with_grace",
    "start_background_process",
    "start_background_job_process",
    "status_matches",
]
