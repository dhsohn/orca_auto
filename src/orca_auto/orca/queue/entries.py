"""ORCA queue rows: identity, generation identity, normalization and lookup helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from orca_auto.core.queue import store as _core_queue
from orca_auto.core.queue.deferral import ADMISSION_DEFERRAL_METADATA_KEY
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.statuses import ACTIVE_STATUSES as ACTIVE_STATUSES
from orca_auto.core.statuses import TERMINAL_STATUSES as TERMINAL_STATUSES
from orca_auto.core.utils import normalize_bool as _shared_normalize_bool
from orca_auto.core.utils import normalize_text as _shared_normalize_text

from ..app_ids import ORCA_AUTO_ORCA_APP_NAME, ORCA_ENGINE, ORCA_TASK_KIND

QUEUED_NOTIFICATION_PENDING_KEY = "orca_queued_notification_pending"
TERMINAL_REPLAY_METADATA_KEY = "orca_terminal_replay"
TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY = "orca_terminal_replay_fence_only"

# Metadata the server changes during one generation's life: the admission
# deferral, the run id, the terminal replay marker and fence, the queued
# notification claim and the publication lease. Every other key, including an
# unknown or newly added one, is generation identity, so it fails closed.
LIFECYCLE_METADATA_KEYS = frozenset(
    {
        ADMISSION_DEFERRAL_METADATA_KEY,
        "run_id",
        TERMINAL_REPLAY_METADATA_KEY,
        TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
        QUEUED_NOTIFICATION_PENDING_KEY,
        QUEUE_RECORD_SYNC_KEY,
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
        QUEUE_RECORD_SYNC_OWNER_PID_KEY,
        QUEUE_RECORD_SYNC_OWNER_START_KEY,
        QUEUE_RECORD_SYNC_TOKEN_KEY,
        QUEUE_RECORD_SYNC_BLOCKED_KEY,
    }
)

_QueueEntryT = TypeVar("_QueueEntryT", bound=QueueEntry)


def _entry_text(entry: Any, field: str) -> str:
    value = entry.get(field) if isinstance(entry, Mapping) else getattr(entry, field, "")
    return str(value or "").strip()


def is_orca_queue_entry(entry: Any) -> bool:
    """Whether a row carries the complete ORCA identity; no ORCA path reads or writes another."""
    return bool(
        _entry_text(entry, "app_name") == ORCA_AUTO_ORCA_APP_NAME
        and _entry_text(entry, "engine") == ORCA_ENGINE
        and _entry_text(entry, "queue_id")
        and _entry_text(entry, "task_id")
        and _entry_text(entry, "task_kind") == ORCA_TASK_KIND
    )


def generation_identity(entry: QueueEntry) -> dict[str, Any]:
    """What makes a row one queue generation: every writer's fence compares this."""
    return {
        "queue_id": entry.queue_id,
        "app_name": entry.app_name,
        "task_id": entry.task_id,
        "task_kind": entry.task_kind,
        "engine": entry.engine,
        "priority": entry.priority,
        "enqueued_at": entry.enqueued_at,
        "metadata": {
            key: value
            for key, value in entry.metadata.items()
            if key not in LIFECYCLE_METADATA_KEYS
        },
    }


def same_generation(current: QueueEntry, expected: QueueEntry) -> bool:
    return generation_identity(current) == generation_identity(expected)


def queue_entry_generation_token(entry: QueueEntry) -> str:
    """The persisted ``queue_generation``: an opaque digest of ``generation_identity``."""
    encoded = json.dumps(
        generation_identity(entry),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_text(value: object | None) -> str:
    if isinstance(value, QueueStatus):
        return value.value
    return _shared_normalize_text(value)


def normalize_bool(value: object) -> bool:
    if not isinstance(value, (bool, str)) and value is not None:
        return bool(value)
    return _shared_normalize_bool(value)


def normalize_optional_text(value: object | None) -> str | None:
    text = normalize_text(value)
    if not text or text.lower() == "none":
        return None
    return text


def normalize_metadata(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


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


def queue_entry_reaction_dir(entry: QueueEntry) -> str:
    return normalize_text(entry.metadata.get("reaction_dir"))


def queue_entry_force(entry: QueueEntry) -> bool:
    return normalize_bool(entry.metadata.get("force", False))


def find_active_entry(entries: Sequence[_QueueEntryT], reaction_dir: str) -> _QueueEntryT | None:
    return _core_queue.find_entry_by_key(
        entries,
        reaction_dir,
        key_fn=queue_entry_reaction_dir,
        statuses=ACTIVE_STATUSES,
    )
