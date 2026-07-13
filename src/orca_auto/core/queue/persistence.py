from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import QUEUE_FILE

from ..utils.persistence import (
    atomic_write_json,
    coerce_bool,
    load_json_list_file,
    resolve_root_path,
)
from .priority import normalize_queue_priority
from .types import QueueEntry, QueueStatus

QUEUE_FILE_NAME = QUEUE_FILE
QUEUE_LOCK_NAME = "queue.lock"


class QueueStoreCorruptError(RuntimeError):
    """Raised when the queue file exists but cannot be safely loaded."""


def _validate_queue_ids(
    entries: Sequence[QueueEntry],
    *,
    corrupt_error: type[Exception] = QueueStoreCorruptError,
) -> None:
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        queue_id = str(getattr(entry, "queue_id", "") or "").strip()
        if not queue_id:
            raise corrupt_error(f"Queue file entry at index {index} has a blank queue_id")
        if queue_id in seen:
            raise corrupt_error(f"Queue file has duplicate queue_id {queue_id!r}")
        seen.add(queue_id)


def queue_path(root: Path) -> Path:
    return root / QUEUE_FILE_NAME


def queue_lock_path(root: Path) -> Path:
    return root / QUEUE_LOCK_NAME


def entry_to_dict(entry: QueueEntry) -> dict[str, Any]:
    data = asdict(entry)
    data["status"] = entry.status.value
    data["priority"] = normalize_queue_priority(entry.priority)
    return data


def _entry_from_dict(raw: dict[str, Any]) -> QueueEntry:
    status_raw = str(raw.get("status", QueueStatus.PENDING.value)).strip().lower()
    try:
        status = QueueStatus(status_raw)
    except ValueError as exc:
        queue_id = str(raw.get("queue_id", "")).strip() or "<missing>"
        raise QueueStoreCorruptError(
            f"Unknown queue status {status_raw!r} in queue entry {queue_id!r}"
        ) from exc

    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    return QueueEntry(
        queue_id=str(raw.get("queue_id", "")).strip(),
        app_name=str(raw.get("app_name", "")).strip(),
        task_id=str(raw.get("task_id", "")).strip(),
        task_kind=str(raw.get("task_kind", "")).strip(),
        engine=str(raw.get("engine", "")).strip(),
        status=status,
        priority=normalize_queue_priority(raw.get("priority"), default=10),
        enqueued_at=str(raw.get("enqueued_at", "")).strip(),
        started_at=str(raw.get("started_at", "")).strip(),
        finished_at=str(raw.get("finished_at", "")).strip(),
        cancel_requested=coerce_bool(raw.get("cancel_requested", False)),
        error=str(raw.get("error", "")).strip(),
        metadata=metadata,
    )


def entry_from_dict(raw: dict[str, Any]) -> QueueEntry:
    return _entry_from_dict(raw)


def load_entries(
    root: str | Path,
    *,
    entry_from_dict_fn: Callable[[dict[str, Any]], QueueEntry] = entry_from_dict,
    corrupt_error: type[Exception] = QueueStoreCorruptError,
) -> list[QueueEntry]:
    resolved_root = resolve_root_path(root)
    raw = load_json_list_file(
        queue_path(resolved_root),
        corrupt_error=corrupt_error,
        description="Queue file",
    )
    entries: list[QueueEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise corrupt_error(f"Queue file entry at index {index} must be a JSON object")
        try:
            entries.append(entry_from_dict_fn(item))
        except QueueStoreCorruptError as exc:
            if isinstance(exc, corrupt_error):
                raise
            raise corrupt_error(str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise corrupt_error(f"Queue file entry at index {index} is invalid: {exc}") from exc
    _validate_queue_ids(entries, corrupt_error=corrupt_error)
    return entries


def save_entries(
    root: str | Path,
    entries: Sequence[QueueEntry],
    *,
    entry_to_dict_fn: Callable[[QueueEntry], dict[str, Any]] = entry_to_dict,
) -> None:
    resolved_root = resolve_root_path(root)
    _validate_queue_ids(entries)
    atomic_write_json(
        queue_path(resolved_root),
        [entry_to_dict_fn(item) for item in entries],
        ensure_ascii=True,
        indent=2,
    )
