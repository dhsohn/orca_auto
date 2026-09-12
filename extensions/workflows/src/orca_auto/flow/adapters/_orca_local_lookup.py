from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import QUEUE_FILE as QUEUE_FILE_NAME
from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue.metadata import (
    mapping_metadata_value as queue_entry_metadata_value_impl,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.persistence import load_json_mapping_list_file

from ._orca_path_helpers import resolve_candidate_path_impl

JsonPayload = dict[str, Any]
JsonPayloadList = list[JsonPayload]


def load_json_list_impl(path: Path) -> JsonPayloadList:
    return load_json_mapping_list_file(path)


def _queue_entry_matches(
    entry: JsonPayload,
    *,
    queue_id: str,
    run_id: str,
    resolved_reaction_dir: Path | None,
) -> bool:
    entry_queue_id = normalize_text(entry.get("queue_id"))
    entry_run_id = normalize_text(queue_entry_metadata_value_impl(entry, "run_id"))
    entry_reaction_dir = resolve_candidate_path_impl(
        normalize_text(queue_entry_metadata_value_impl(entry, "reaction_dir"))
    )

    return (
        (bool(queue_id) and entry_queue_id == queue_id)
        or (bool(run_id) and entry_run_id == run_id)
        or (resolved_reaction_dir is not None and entry_reaction_dir == resolved_reaction_dir)
    )


def find_queue_entry_impl(
    *,
    allowed_root: Path | None,
    queue_id: str,
    run_id: str,
    reaction_dir: str,
) -> JsonPayload | None:
    if allowed_root is None:
        return None
    entries = [
        entry
        for entry in load_json_list_impl(allowed_root / QUEUE_FILE_NAME)
        if entry_matches_engine_identity(entry, "orca")
    ]
    if not entries:
        return None

    resolved_reaction_dir = resolve_candidate_path_impl(reaction_dir)

    for entry in reversed(entries):
        if _queue_entry_matches(
            entry,
            queue_id=queue_id,
            run_id=run_id,
            resolved_reaction_dir=resolved_reaction_dir,
        ):
            return entry
    return None


__all__ = [
    "find_queue_entry_impl",
    "load_json_list_impl",
    "queue_entry_metadata_value_impl",
]
