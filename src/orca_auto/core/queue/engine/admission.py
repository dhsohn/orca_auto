from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..worker import reserve_engine_queue_worker_slot

LOGGER = logging.getLogger(__name__)


def reserve_engine_admission_slot(
    cfg: Any,
    *,
    engine: str,
    reserve_slot_fn: Callable[..., str | None],
) -> str | None:
    return reserve_engine_queue_worker_slot(
        cfg,
        engine=engine,
        reserve_slot_fn=reserve_slot_fn,
    )


def start_engine_child_process(
    *,
    config_path: str,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    start_background_process_fn: Callable[[list[str]], Any],
    build_worker_child_command_fn: Callable[..., list[str]],
) -> Any:
    return start_background_process_fn(
        build_worker_child_command_fn(
            config_path=config_path,
            queue_root=queue_root,
            queue_id=entry.queue_id,
            admission_token=admission_token,
        ),
    )


def mark_worker_start_error(
    *,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
    mark_entry_failed_and_release_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
) -> None:
    mark_entry_failed_and_release_fn(
        queue_root,
        entry,
        admission_token,
        error=str(exc),
        mark_failed_fn=mark_failed_fn,
    )


def queue_entry_work_dir(
    entry: Any,
    *,
    metadata_keys: tuple[str, ...] = ("job_dir", "reaction_dir"),
) -> str | None:
    metadata = getattr(entry, "metadata", {})
    getter = getattr(metadata, "get", None)
    if not callable(getter):
        return None
    for key in metadata_keys:
        text = str(getter(key, "") or "").strip()
        if text:
            return text
    return None


def attach_started_process(
    *,
    admission_root: str | Path,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
    activate_reserved_slot_fn: Callable[..., Any],
    terminate_process_fn: Callable[[Any], Any],
    mark_entry_failed_and_release_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    source: str,
) -> bool:
    work_dir = queue_entry_work_dir(entry)
    attached = activate_reserved_slot_fn(
        admission_root,
        admission_token,
        owner_pid=process.pid,
        source=source,
        queue_id=entry.queue_id,
        work_dir=work_dir,
    )
    if attached is not None:
        return True

    terminate_process_fn(process)
    mark_entry_failed_and_release_fn(
        queue_root,
        entry,
        admission_token,
        error="admission_slot_missing",
        mark_failed_fn=mark_failed_fn,
    )
    return False


def attach_started_process_metadata(
    *,
    admission_root: str | Path,
    queue_root: Path,
    entry: Any,
    process: Any,
    admission_token: str,
    queue_entry_id_fn: Callable[[Any], str],
    queue_entry_app_name_fn: Callable[[Any], str],
    queue_entry_task_id_fn: Callable[[Any], str | None],
    update_slot_metadata_fn: Callable[..., Any],
    terminate_process_fn: Callable[[Any], Any],
    mark_entry_failed_and_release_fn: Callable[..., Any],
    mark_failed_fn: Callable[..., Any],
    cfg: Any | None = None,
    upsert_running_job_record_fn: Callable[[Any, Any], Any] | None = None,
    logger: logging.Logger = LOGGER,
) -> bool:
    queue_id = queue_entry_id_fn(entry)
    work_dir = queue_entry_work_dir(entry)
    attached = update_slot_metadata_fn(
        admission_root,
        admission_token,
        state="active",
        queue_id=queue_id,
        app_name=queue_entry_app_name_fn(entry),
        task_id=queue_entry_task_id_fn(entry),
        owner_pid=process.pid,
        work_dir=work_dir,
    )
    if not attached:
        logger.error(
            "Failed to attach queue identity to admission slot %s for job %s",
            admission_token,
            queue_id,
        )
        terminate_process_fn(process)
        mark_entry_failed_and_release_fn(
            queue_root,
            entry,
            admission_token,
            error="admission_slot_missing",
            mark_failed_fn=mark_failed_fn,
        )
        return False

    if cfg is not None and upsert_running_job_record_fn is not None:
        try:
            upsert_running_job_record_fn(cfg, entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to update running job location for %s: %s", queue_id, exc)
    return True


__all__ = [
    "attach_started_process",
    "attach_started_process_metadata",
    "mark_worker_start_error",
    "queue_entry_work_dir",
    "reserve_engine_admission_slot",
    "start_engine_child_process",
]
