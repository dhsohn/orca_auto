from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orca_auto.core.engine_process import open_confined_log

from ..types import QueueEntry, QueueStatus


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


def status_matches(value: Any, expected: Any) -> bool:
    actual_value = getattr(value, "value", value)
    expected_value = getattr(expected, "value", expected)
    return str(actual_value).strip().lower() == str(expected_value).strip().lower()


def entry_status_is_running(entry: QueueEntry | None) -> bool:
    return status_matches(getattr(entry, "status", None), QueueStatus.RUNNING)


__all__ = [
    "entry_status_is_running",
    "live_queue_slot_keys_for_slots",
    "start_background_process",
    "status_matches",
]
