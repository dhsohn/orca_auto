from __future__ import annotations

import hashlib
import json
from typing import Any

from .types import QueueEntry

# Metadata is generation identity by default. Only server-owned lifecycle/result
# fields may opt out, so unknown or newly added submission fields fail closed.
_MUTABLE_LIFECYCLE_METADATA_KEYS = {
    "attempt",
    "candidate_count",
    "execution_dir",
    "retained_conformer_count",
    "terminal_artifacts",
    "terminal_repair_blocked_reason",
}


def immutable_generation_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in metadata.items()
        if key not in _MUTABLE_LIFECYCLE_METADATA_KEYS
        and not str(key).startswith("_orca_auto_queued_record_sync")
    }


def queue_entries_same_generation(current: QueueEntry, expected: QueueEntry) -> bool:
    return bool(
        current.queue_id == expected.queue_id
        and current.app_name == expected.app_name
        and current.task_id == expected.task_id
        and current.task_kind == expected.task_kind
        and current.engine == expected.engine
        and current.priority == expected.priority
        and current.enqueued_at == expected.enqueued_at
        and immutable_generation_metadata(current.metadata)
        == immutable_generation_metadata(expected.metadata)
    )


def queue_entry_generation_token(entry: QueueEntry) -> str:
    identity = {
        "queue_id": entry.queue_id,
        "app_name": entry.app_name,
        "task_id": entry.task_id,
        "task_kind": entry.task_kind,
        "engine": entry.engine,
        "priority": entry.priority,
        "enqueued_at": entry.enqueued_at,
        "metadata": immutable_generation_metadata(entry.metadata),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "immutable_generation_metadata",
    "queue_entries_same_generation",
    "queue_entry_generation_token",
]
