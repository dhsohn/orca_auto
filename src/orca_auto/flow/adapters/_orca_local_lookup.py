from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.engines import entry_matches_engine_identity
from orca_auto.core.queue.metadata import (
    mapping_metadata as queue_entry_metadata_impl,
)
from orca_auto.core.queue.metadata import (
    mapping_metadata_value as queue_entry_metadata_value_impl,
)
from orca_auto.core.utils.coercion import normalize_text
from orca_auto.core.utils.persistence import load_json_mapping_file, load_json_mapping_list_file

from ._orca_path_helpers import direct_dir_target_impl, resolve_candidate_path_impl

QUEUE_FILE_NAME = "queue.json"

JsonPayload = dict[str, Any]
JsonPayloadList = list[JsonPayload]


def load_json_dict_impl(path: Path) -> JsonPayload:
    return load_json_mapping_file(path) or {}


def load_json_list_impl(path: Path) -> JsonPayloadList:
    return load_json_mapping_list_file(path)


def _queue_entry_matches(
    entry: JsonPayload,
    *,
    target: str,
    queue_id: str,
    run_id: str,
    direct_target: Path | None,
    resolved_reaction_dir: Path | None,
) -> bool:
    entry_queue_id = normalize_text(entry.get("queue_id"))
    entry_task_id = normalize_text(entry.get("task_id"))
    entry_run_id = normalize_text(queue_entry_metadata_value_impl(entry, "run_id"))
    entry_reaction_dir = resolve_candidate_path_impl(
        normalize_text(queue_entry_metadata_value_impl(entry, "reaction_dir"))
    )

    return (
        (bool(queue_id) and entry_queue_id == queue_id)
        or (bool(target) and entry_queue_id == target)
        or (bool(target) and entry_task_id == target)
        or (bool(run_id) and entry_run_id == run_id)
        or (bool(target) and entry_run_id == target)
        or (resolved_reaction_dir is not None and entry_reaction_dir == resolved_reaction_dir)
        or (direct_target is not None and entry_reaction_dir == direct_target)
    )


def find_queue_entry_impl(
    *,
    allowed_root: Path | None,
    target: str,
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

    direct_target = direct_dir_target_impl(target)
    resolved_reaction_dir = resolve_candidate_path_impl(reaction_dir)

    for entry in reversed(entries):
        if _queue_entry_matches(
            entry,
            target=target,
            queue_id=queue_id,
            run_id=run_id,
            direct_target=direct_target,
            resolved_reaction_dir=resolved_reaction_dir,
        ):
            return entry
    return None


__all__ = [
    "find_queue_entry_impl",
    "load_json_dict_impl",
    "load_json_list_impl",
    "queue_entry_metadata_impl",
    "queue_entry_metadata_value_impl",
]
