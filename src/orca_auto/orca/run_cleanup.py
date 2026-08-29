from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from orca_auto.core.paths import should_exclude_from_production_runs_scan
from orca_auto.core.queue import store as _queue_store
from orca_auto.core.statuses import STATUS_CANCELLED
from orca_auto.core.utils.lock import file_lock_at
from orca_auto.core.utils.process_tracking import run_lock_is_held

from .queue.adapter import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    clear_terminal,
    is_orca_queue_entry,
    list_queue,
    queue_entry_reaction_dir,
    queue_entry_status,
)
from .queue.entries import load_entries as _load_queue_entries
from .queue.terminal_replay import (
    TerminalReplayMarkerKind,
    terminal_replay_marker_kind,
)
from .run_snapshot import (
    RunSnapshot,
    _load_pinned_state,
    _state_publication_identity,
    collect_run_snapshots,
)
from .state import STATE_MUTATION_LOCK_FILE_NAME
from .state_reading import STATE_FILE_NAME
from .statuses import RunStatus

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, STATUS_CANCELLED}
)
_STALE_ACTIVE_RUN_STATUSES = frozenset({RunStatus.RUNNING.value, RunStatus.RETRYING.value})


def _resolved_path_text(path_text: str) -> str:
    text = str(path_text).strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def _queue_cleanup_reaction_dirs(
    allowed_root: Path,
) -> tuple[set[str], set[str], set[str]]:
    active_dirs: set[str] = set()
    terminal_dirs: set[str] = set()
    pending_replay_dirs: set[str] = set()
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
            if terminal_replay_marker_kind(entry) is not TerminalReplayMarkerKind.ABSENT:
                # Queue terminalization and state/report/index/notification are
                # a cross-store publication.  Deleting state while a valid (or
                # unsupported/corrupt) replay claim remains can make a fresh
                # worker classify that generation as superseded and lose the
                # unfinished side effects permanently.
                pending_replay_dirs.add(reaction_dir)
    return active_dirs, terminal_dirs - active_dirs - pending_replay_dirs, pending_replay_dirs


def _snapshot_has_live_run_lock(snapshot: RunSnapshot) -> bool:
    return run_lock_is_held(snapshot.reaction_dir, logger=logger)


def _queue_generation_blocks_state_cleanup(
    allowed_root: Path,
    reaction_dir: str,
) -> bool:
    for entry in _load_queue_entries(allowed_root):
        if not is_orca_queue_entry(entry):
            continue
        if _resolved_path_text(queue_entry_reaction_dir(entry)) != reaction_dir:
            continue
        status = queue_entry_status(entry)
        if status in ACTIVE_STATUSES:
            return True
        if (
            status in TERMINAL_STATUSES
            and terminal_replay_marker_kind(entry) is not TerminalReplayMarkerKind.ABSENT
        ):
            return True
    return False


def _should_clear_snapshot(
    snapshot: RunSnapshot,
    *,
    active_queue_reaction_dirs: set[str],
    terminal_queue_reaction_dirs: set[str],
    pending_replay_reaction_dirs: set[str],
) -> bool:
    status = snapshot.status
    reaction_dir = _resolved_path_text(str(snapshot.reaction_dir))
    if reaction_dir in active_queue_reaction_dirs or reaction_dir in pending_replay_reaction_dirs:
        return False
    if status in _TERMINAL_RUN_STATUSES:
        return True
    if status not in _STALE_ACTIVE_RUN_STATUSES:
        return False
    if reaction_dir not in terminal_queue_reaction_dirs:
        return False
    return not _snapshot_has_live_run_lock(snapshot)


def _open_stable_reaction_dir(
    reaction_dir: Path,
    allowed_root: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> int | None:
    """Open a no-follow directory handle after a final production-boundary check."""

    if expected_identity is None or should_exclude_from_production_runs_scan(
        reaction_dir, allowed_root
    ):
        return None

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(reaction_dir, flags)
    except OSError:
        return None

    try:
        opened_stat = os.fstat(directory_fd)
        if (opened_stat.st_dev, opened_stat.st_ino) != expected_identity:
            raise OSError("reaction directory no longer matches the collected snapshot")
        current_stat = reaction_dir.lstat()
        if not stat.S_ISDIR(current_stat.st_mode):
            raise OSError("reaction directory was replaced with a non-directory")
        if (current_stat.st_dev, current_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise OSError("reaction directory changed while it was opened")
        if should_exclude_from_production_runs_scan(reaction_dir, allowed_root):
            raise OSError("reaction directory is inside a reserved or unsafe scan tree")
    except OSError:
        os.close(directory_fd)
        return None
    return directory_fd


def _snapshot_state_is_current(snapshot: RunSnapshot, directory_fd: int) -> bool:
    if snapshot.state_file_identity is None:
        return False
    loaded_state = _load_pinned_state(directory_fd)
    if loaded_state is None:
        return False
    state_payload, state_file_identity = loaded_state
    if state_file_identity != snapshot.state_file_identity:
        return False
    run_identity, generation_identity = _state_publication_identity(state_payload)
    return bool(
        run_identity == snapshot.state_run_identity
        and generation_identity == snapshot.state_generation_identity
    )


def _snapshot_directory_is_current(
    snapshot: RunSnapshot,
    allowed_root: Path,
    directory_fd: int,
) -> bool:
    if snapshot.reaction_dir_identity is None:
        return False
    try:
        opened_stat = os.fstat(directory_fd)
        path_stat = snapshot.reaction_dir.lstat()
    except OSError:
        return False
    opened_identity = (opened_stat.st_dev, opened_stat.st_ino)
    return bool(
        stat.S_ISDIR(path_stat.st_mode)
        and opened_identity == snapshot.reaction_dir_identity
        and (path_stat.st_dev, path_stat.st_ino) == opened_identity
        and not should_exclude_from_production_runs_scan(
            snapshot.reaction_dir,
            allowed_root,
        )
    )


def clear_terminal_run_states(allowed_root: Path) -> int:
    (
        active_queue_reaction_dirs,
        terminal_queue_reaction_dirs,
        pending_replay_reaction_dirs,
    ) = _queue_cleanup_reaction_dirs(allowed_root)
    cleared_state_paths: set[str] = set()
    run_count = 0

    for snapshot in collect_run_snapshots(allowed_root):
        if not _should_clear_snapshot(
            snapshot,
            active_queue_reaction_dirs=active_queue_reaction_dirs,
            terminal_queue_reaction_dirs=terminal_queue_reaction_dirs,
            pending_replay_reaction_dirs=pending_replay_reaction_dirs,
        ):
            continue

        state_path = snapshot.reaction_dir / STATE_FILE_NAME
        state_key = _resolved_path_text(str(state_path))
        if state_key in cleared_state_paths:
            continue
        cleared_state_paths.add(state_key)

        directory_fd = _open_stable_reaction_dir(
            snapshot.reaction_dir,
            allowed_root,
            expected_identity=snapshot.reaction_dir_identity,
        )
        if directory_fd is None:
            continue

        try:
            # Serialize the final eligibility check and state unlink against a
            # terminal writer's queue mutation.  The queue -> state lock order
            # ensures a writer either fingerprints the post-cleanup absence or
            # leaves a marker/active row that prevents cleanup; it cannot save a
            # pre-delete fingerprint and then lose that state underneath it.
            with _queue_store.queue_lock(allowed_root):
                reaction_dir = _resolved_path_text(str(snapshot.reaction_dir))
                if _queue_generation_blocks_state_cleanup(allowed_root, reaction_dir):
                    continue
                with file_lock_at(
                    directory_fd,
                    STATE_MUTATION_LOCK_FILE_NAME,
                    display_path=snapshot.reaction_dir / STATE_MUTATION_LOCK_FILE_NAME,
                ):
                    if not _snapshot_directory_is_current(
                        snapshot,
                        allowed_root,
                        directory_fd,
                    ):
                        continue
                    if not _snapshot_state_is_current(snapshot, directory_fd):
                        continue
                    os.unlink(STATE_FILE_NAME, dir_fd=directory_fd)
                    run_count += 1
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            logger.warning("Failed to remove %s: %s", state_path, exc)
        finally:
            os.close(directory_fd)

    return run_count


def clear_terminal_entries(allowed_root: Path) -> tuple[int, int]:
    run_count = clear_terminal_run_states(allowed_root)
    queue_count = clear_terminal(allowed_root)
    return queue_count, run_count
