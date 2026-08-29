from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import QueueEntry

VISIBLE_GENERATION_NAME_RE = re.compile(r"\A\d{8}-\d{6}-[0-9a-f]{8}\Z", re.ASCII)


def is_visible_generation_name(value: str) -> bool:
    """Return whether *value* is an exact user-visible execution generation name."""

    return bool(VISIBLE_GENERATION_NAME_RE.fullmatch(str(value)))


def visible_generation_children(job_dir: Path) -> tuple[Path, ...]:
    """Direct generation-named child directories of *job_dir*, newest first.

    Generation names embed a local timestamp, so the reverse lexicographic
    order is the creation order. Symlinked children are excluded so readers
    cannot be steered outside the job directory.
    """

    try:
        entries = list(job_dir.iterdir())
    except OSError:
        return ()
    children = [
        entry
        for entry in entries
        if is_visible_generation_name(entry.name) and entry.is_dir() and not entry.is_symlink()
    ]

    def _recency_key(entry: Path) -> tuple[str, int, str]:
        # Generation names carry only second-resolution timestamps followed by
        # random hex, so the name alone cannot order two generations minted in
        # the same second. Break the tie with the directory's own mtime (the
        # newer generation is created — and written — after the older one
        # stopped changing), keeping the full name as a stable last resort.
        try:
            mtime_ns = entry.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        return (entry.name[:15], mtime_ns, entry.name)

    return tuple(sorted(children, key=_recency_key, reverse=True))


def new_visible_generation_name() -> str:
    """Mint a fresh user-visible generation name (`YYYYMMDD-HHMMSS-<8hex>`).

    The one shared factory behind ORCA execution generations and workflow
    workspace generations, so both always match VISIBLE_GENERATION_NAME_RE.
    """

    local_timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{local_timestamp}-{secrets.token_hex(4)}"


# Metadata is generation identity by default. Only server-owned lifecycle/result
# fields may opt out, so unknown or newly added submission fields fail closed.
_MUTABLE_LIFECYCLE_METADATA_KEYS = frozenset(
    {
        "attempt",
        "candidate_count",
        "execution_dir",
        "orca_terminal_replay",
        "orca_terminal_replay_fence_only",
        "retained_conformer_count",
        "run_id",
        "terminal_artifacts",
        "terminal_repair_blocked_reason",
    }
)


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
    "VISIBLE_GENERATION_NAME_RE",
    "immutable_generation_metadata",
    "is_visible_generation_name",
    "new_visible_generation_name",
    "queue_entries_same_generation",
    "queue_entry_generation_token",
    "visible_generation_children",
]
