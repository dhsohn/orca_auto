"""ORCA queue entry normalization and lookup helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

from orca_auto.core.queue import store as _core_queue
from orca_auto.core.queue.priority import normalize_queue_priority
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import normalize_bool as _shared_normalize_bool
from orca_auto.core.utils import normalize_text as _shared_normalize_text

from ...core.app_ids import ORCA_AUTO_ORCA_APP_NAME

QUEUE_FILE_NAME = "queue.json"
WORKER_PID_FILE_NAME = "queue_worker.pid"
QUEUE_APP_NAME = ORCA_AUTO_ORCA_APP_NAME
QUEUE_ENGINE = "orca"
QUEUE_TASK_KIND = "orca_run_inp"

TERMINAL_STATUSES = frozenset(
    {
        QueueStatus.COMPLETED.value,
        QueueStatus.FAILED.value,
        QueueStatus.CANCELLED.value,
    }
)
ACTIVE_STATUSES = frozenset(
    {
        QueueStatus.PENDING.value,
        QueueStatus.RUNNING.value,
    }
)

_QueueEntryT = TypeVar("_QueueEntryT", bound=QueueEntry)


def normalize_text(value: object | None) -> str:
    if isinstance(value, QueueStatus):
        return value.value
    return _shared_normalize_text(value)


def normalize_bool(value: object) -> bool:
    if not isinstance(value, (bool, str)) and value is not None:
        return bool(value)
    return _shared_normalize_bool(value)


def normalize_priority(value: object, *, default: int = 10) -> int:
    return normalize_queue_priority(value, default=default)


def normalize_optional_text(value: object | None) -> str | None:
    text = normalize_text(value)
    if not text or text.lower() == "none":
        return None
    return text


def normalize_metadata(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


def normalized_raw(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    metadata = normalize_metadata(normalized.get("metadata"))
    reaction_dir = normalize_text(metadata.get("reaction_dir"))
    force = normalize_bool(metadata.get("force", False))
    run_id = normalize_optional_text(metadata.get("run_id"))

    if reaction_dir:
        metadata["reaction_dir"] = reaction_dir
    metadata["force"] = force
    if run_id is not None:
        metadata["run_id"] = run_id
    else:
        metadata.pop("run_id", None)

    # Durable queue identity is evidence, not a legacy default.  Preserve
    # missing fields as blank so the exact worker identity predicate can
    # quarantine incomplete or foreign rows instead of promoting them to ORCA.
    normalized["app_name"] = normalize_text(normalized.get("app_name"))
    normalized["task_id"] = normalize_text(normalized.get("task_id"))
    normalized["task_kind"] = normalize_text(normalized.get("task_kind"))
    normalized["engine"] = normalize_text(normalized.get("engine"))
    normalized["priority"] = normalize_priority(normalized.get("priority"), default=10)
    normalized["status"] = (
        normalize_text(normalized.get("status")).lower() or QueueStatus.PENDING.value
    )
    normalized["started_at"] = normalize_optional_text(normalized.get("started_at")) or ""
    normalized["finished_at"] = normalize_optional_text(normalized.get("finished_at")) or ""
    normalized["error"] = normalize_optional_text(normalized.get("error")) or ""
    normalized["cancel_requested"] = normalize_bool(normalized.get("cancel_requested", False))
    normalized["metadata"] = metadata
    return normalized


def entry_from_json_payload(raw: dict[str, Any]) -> QueueEntry:
    return _core_queue.entry_from_dict(normalized_raw(raw))


def load_entries(allowed_root: Path) -> list[QueueEntry]:
    return _core_queue.load_entries(
        allowed_root,
        entry_from_dict_fn=entry_from_json_payload,
        corrupt_error=_core_queue.QueueStoreCorruptError,
    )


def entry_metadata(
    *,
    reaction_dir: str,
    force: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = normalize_metadata(extra)
    metadata.setdefault("reaction_dir", reaction_dir)
    metadata.setdefault("force", force)
    return metadata


def queue_entry_metadata(entry: QueueEntry) -> dict[str, Any]:
    return dict(entry.metadata)


def queue_entry_run_id(entry: QueueEntry) -> str | None:
    return normalize_optional_text(entry.metadata.get("run_id"))


def queue_entry_id(entry: QueueEntry) -> str:
    return normalize_text(entry.queue_id)


def queue_entry_task_id(entry: QueueEntry) -> str | None:
    task_id = normalize_text(entry.task_id)
    return task_id or None


def queue_entry_status(entry: QueueEntry) -> str:
    if isinstance(entry.status, QueueStatus):
        return entry.status.value
    return normalize_text(entry.status).lower()


def queue_entry_reaction_dir(entry: QueueEntry) -> str:
    return normalize_text(entry.metadata.get("reaction_dir"))


def queue_entry_force(entry: QueueEntry) -> bool:
    return normalize_bool(entry.metadata.get("force", False))


def queue_entry_priority(entry: QueueEntry) -> int:
    return int(entry.priority)


def queue_entry_app_name(entry: QueueEntry) -> str:
    return normalize_text(entry.app_name)


def _resolved_path_text(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        return ""


def queue_entry_target_aliases(entry: QueueEntry) -> set[str]:
    aliases = {
        queue_entry_id(entry),
        queue_entry_task_id(entry) or "",
        queue_entry_run_id(entry) or "",
        queue_entry_reaction_dir(entry),
    }
    reaction_dir_path = _resolved_path_text(queue_entry_reaction_dir(entry))
    if reaction_dir_path:
        aliases.add(reaction_dir_path)
    return {alias for alias in aliases if alias}


def queue_entry_matches_target(entry: QueueEntry, target: str) -> bool:
    normalized_target = normalize_text(target)
    if not normalized_target:
        return False
    aliases = queue_entry_target_aliases(entry)
    if normalized_target in aliases:
        return True
    resolved_target = _resolved_path_text(normalized_target)
    return bool(resolved_target and resolved_target in aliases)


def find_active_entry(entries: Sequence[_QueueEntryT], reaction_dir: str) -> _QueueEntryT | None:
    return _core_queue.find_entry_by_key(
        entries,
        reaction_dir,
        key_fn=queue_entry_reaction_dir,
        statuses=ACTIVE_STATUSES,
    )
