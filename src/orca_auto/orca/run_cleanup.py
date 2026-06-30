from __future__ import annotations

import logging
from pathlib import Path

from orca_auto.core.utils.process_tracking import active_run_lock_pid

from .queue_adapter import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    clear_terminal,
    list_queue,
    queue_entry_reaction_dir,
    queue_entry_status,
)
from .run_snapshot import RunSnapshot, collect_run_snapshots
from .state import STATE_FILE_NAME
from .statuses import RunStatus

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED.value, RunStatus.FAILED.value})
_STALE_ACTIVE_RUN_STATUSES = frozenset({RunStatus.RUNNING.value, RunStatus.RETRYING.value})


def _resolved_path_text(path_text: str) -> str:
    text = str(path_text).strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def _terminal_queue_reaction_dirs(allowed_root: Path) -> set[str]:
    active_dirs: set[str] = set()
    terminal_dirs: set[str] = set()
    for entry in list_queue(allowed_root):
        status = queue_entry_status(entry)
        if status not in ACTIVE_STATUSES and status not in TERMINAL_STATUSES:
            continue
        reaction_dir = _resolved_path_text(queue_entry_reaction_dir(entry))
        if not reaction_dir:
            continue
        if status in ACTIVE_STATUSES:
            active_dirs.add(reaction_dir)
        elif status in TERMINAL_STATUSES:
            terminal_dirs.add(reaction_dir)
    return terminal_dirs - active_dirs


def _snapshot_has_live_run_lock(snapshot: RunSnapshot) -> bool:
    return active_run_lock_pid(snapshot.reaction_dir, logger=logger) is not None


def _should_clear_snapshot(
    snapshot: RunSnapshot,
    *,
    terminal_queue_reaction_dirs: set[str],
) -> bool:
    status = snapshot.status
    if status in _TERMINAL_RUN_STATUSES:
        return True
    if status not in _STALE_ACTIVE_RUN_STATUSES:
        return False
    reaction_dir = _resolved_path_text(str(snapshot.reaction_dir))
    if reaction_dir not in terminal_queue_reaction_dirs:
        return False
    return not _snapshot_has_live_run_lock(snapshot)


def clear_terminal_run_states(allowed_root: Path) -> int:
    terminal_queue_reaction_dirs = _terminal_queue_reaction_dirs(allowed_root)
    cleared_state_paths: set[str] = set()
    run_count = 0

    for snapshot in collect_run_snapshots(allowed_root):
        if not _should_clear_snapshot(
            snapshot,
            terminal_queue_reaction_dirs=terminal_queue_reaction_dirs,
        ):
            continue

        state_path = snapshot.reaction_dir / STATE_FILE_NAME
        state_key = _resolved_path_text(str(state_path))
        if state_key in cleared_state_paths:
            continue
        cleared_state_paths.add(state_key)

        try:
            state_path.unlink()
            run_count += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Failed to remove %s: %s", state_path, exc)

    return run_count


def clear_terminal_entries(allowed_root: Path) -> tuple[int, int]:
    run_count = clear_terminal_run_states(allowed_root)
    queue_count = clear_terminal(allowed_root)
    return queue_count, run_count
