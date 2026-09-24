"""Operator inspection and removal used by ``orca_auto scratch``:
``inspect_scratch_root`` classifies every entry without taking the lock,
``durable_publication_journal_status`` reports an interrupted journal
without touching it, and ``remove_scratch_workspace`` removes one non-live
workspace under the lock and cleans the publication leftovers its manifest
names when that is provably safe.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from orca_auto.core.utils.lock import FileLockTimeoutError, file_lock_at
from orca_auto.core.utils.stable_fs import unlink_at_if_present as _unlink_at_if_present

from ._constants import (
    _CLEANUP_TOMBSTONE_NAME_RE,
    _INSPECT_SIZE_WALK_MAX_ENTRIES,
    _PUBLICATION_JOURNAL_FILE_NAME,
    _PUBLICATION_TEMP_NAME_RE,
    _SCRATCH_ROOT_LOCK_FILE_NAME,
    _SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS,
    SCRATCH_REMOVABLE_STATES,
    SCRATCH_STATE_LIVE,
    SCRATCH_STATE_TOMBSTONE,
    SCRATCH_WORKSPACE_PREFIX,
)
from ._errors import EngineScratchError
from ._fs import (
    _entry_exists_at,
    _open_pinned_directory,
)
from ._manifest import _inspect_workspace_entry
from ._publication import _load_publication_journal
from ._reports import (
    PublicationJournalStatus,
    ScratchWorkspaceRemoval,
    ScratchWorkspaceReport,
)
from ._workspace import _remove_owned_workspace_at


def durable_publication_journal_status(durable_dir: Path) -> PublicationJournalStatus | None:
    """Report an interrupted publication journal in ``durable_dir`` without touching it.

    Returns ``None`` when the directory has no journal or cannot be opened.
    Replay and cleanup stay with ``EngineScratchWorkspace.create``.
    """

    try:
        durable_dir_fd, _identity = _open_pinned_directory(
            durable_dir,
            label="engine durable generation",
        )
    except (OSError, EngineScratchError):
        return None
    try:
        return _publication_journal_status_at(durable_dir_fd, durable_dir)
    finally:
        os.close(durable_dir_fd)


def _publication_journal_status_at(
    durable_dir_fd: int,
    durable_dir: Path,
) -> PublicationJournalStatus | None:
    journal_path = durable_dir / _PUBLICATION_JOURNAL_FILE_NAME
    try:
        loaded = _load_publication_journal(durable_dir_fd, durable_dir)
    except EngineScratchError as exc:
        return PublicationJournalStatus(
            path=journal_path,
            phase=None,
            item_count=0,
            corrupt=True,
            detail=str(exc),
        )
    except OSError as exc:
        return PublicationJournalStatus(
            path=journal_path,
            phase=None,
            item_count=0,
            corrupt=True,
            detail=f"engine scratch publication journal is unreadable: {exc}",
        )
    if loaded is None:
        return None
    phase, items = loaded
    return PublicationJournalStatus(
        path=journal_path,
        phase=phase,
        item_count=len(items),
        corrupt=False,
        detail=None,
    )


def inspect_scratch_root(
    root: Path,
    *,
    max_size_entries: int = _INSPECT_SIZE_WALK_MAX_ENTRIES,
) -> list[ScratchWorkspaceReport]:
    """Classify every workspace and tombstone under ``root`` without modifying it.

    A missing root yields an empty list; an unsafe or unreadable root raises
    ``EngineScratchError`` or ``OSError``. The root lock is not taken: the
    listing is advisory and must never wait behind a staging peer.
    """

    if root.is_symlink():
        raise EngineScratchError(f"engine scratch root is a symlink: {root}")
    if not root.exists():
        return []
    root_fd, _identity = _open_pinned_directory(root, label="engine scratch root")
    reports: list[ScratchWorkspaceReport] = []
    try:
        for name in sorted(os.listdir(root_fd)):
            if _CLEANUP_TOMBSTONE_NAME_RE.fullmatch(name):
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                reports.append(
                    ScratchWorkspaceReport(
                        path=root / name,
                        name=name,
                        state=SCRATCH_STATE_TOMBSTONE,
                        manifest_valid=False,
                        owner_pid=None,
                        owner_process_start_ticks=None,
                        owner_boot_id=None,
                        durable_dir=None,
                        max_task_memory_bytes=None,
                        size_bytes=0,
                        size_walk_truncated=True,
                        blocks_launch=not stat.S_ISDIR(info.st_mode),
                        detail=(
                            None
                            if stat.S_ISDIR(info.st_mode)
                            else f"engine scratch root contains an unsafe entry: {root / name}"
                        ),
                    )
                )
                continue
            if not name.startswith(SCRATCH_WORKSPACE_PREFIX):
                continue
            report, _workspace_identity = _inspect_workspace_entry(
                root,
                root_fd,
                name,
                measure_size=True,
                max_size_entries=max_size_entries,
            )
            if report.durable_dir is not None:
                report = dataclasses.replace(
                    report,
                    publication_journal=durable_publication_journal_status(
                        Path(report.durable_dir)
                    ),
                )
            reports.append(report)
    finally:
        os.close(root_fd)
    return reports


def remove_scratch_workspace(
    root: Path,
    workspace_name: str,
    *,
    allow_states: Sequence[str] = SCRATCH_REMOVABLE_STATES,
    durable_root: Path | None = None,
) -> ScratchWorkspaceRemoval:
    """Remove one non-live workspace under the scratch-root lock.

    The workspace is re-classified under the lock and refused (``EngineScratchError``)
    unless its state is in ``allow_states``; ``live`` is never removable through
    this path. After the fd-pinned removal, publication temp files in the
    durable generation the manifest names are unlinked when no journal claims
    them, and a ``committed`` journal is unlinked when none of its temporary or
    backup entries remain. That follow-up only runs when the manifest was valid
    and the named generation lies inside ``durable_root`` (the runs root): an
    invalid or foreign manifest string must never steer a delete outside the
    scratch root. It is also skipped while another live workspace targets the
    same durable generation.
    """

    if SCRATCH_STATE_LIVE in allow_states:
        raise ValueError("live engine scratch workspaces cannot be removed by operators")
    if (
        not workspace_name
        or Path(workspace_name).name != workspace_name
        or not workspace_name.startswith(SCRATCH_WORKSPACE_PREFIX)
    ):
        raise EngineScratchError(f"engine scratch workspace name is unsafe: {workspace_name!r}")
    if root.is_symlink():
        raise EngineScratchError(f"engine scratch root is a symlink: {root}")
    root_fd, _root_identity = _open_pinned_directory(root, label="engine scratch root")
    try:
        try:
            lock = file_lock_at(
                root_fd,
                _SCRATCH_ROOT_LOCK_FILE_NAME,
                display_path=root / _SCRATCH_ROOT_LOCK_FILE_NAME,
                timeout_seconds=_SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS,
            )
        except FileLockTimeoutError as exc:
            raise EngineScratchError(
                "engine scratch root stayed busy with a peer workspace for "
                f"{_SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS:.0f} s"
            ) from exc
        with lock:
            try:
                report, identity = _inspect_workspace_entry(
                    root,
                    root_fd,
                    workspace_name,
                    measure_size=True,
                )
            except FileNotFoundError as exc:
                raise EngineScratchError(
                    f"engine scratch workspace does not exist: {root / workspace_name}"
                ) from exc
            if report.state not in allow_states or identity is None:
                raise EngineScratchError(
                    f"refusing to remove {report.state} engine scratch workspace: {report.path}"
                )
            _remove_owned_workspace_at(root_fd, workspace_name, identity)
            if report.durable_dir is None:
                return ScratchWorkspaceRemoval(
                    report=report,
                    removed_durable_entries=(),
                    publication_journal_removed=False,
                    durable_note="manifest names no durable generation; nothing else to clean",
                )
            if not report.manifest_valid:
                return ScratchWorkspaceRemoval(
                    report=report,
                    removed_durable_entries=(),
                    publication_journal_removed=False,
                    durable_note=(
                        "manifest is invalid; its durable generation path is not trusted "
                        "and was left alone"
                    ),
                )
            if not _durable_dir_within_root(report.durable_dir, durable_root):
                return ScratchWorkspaceRemoval(
                    report=report,
                    removed_durable_entries=(),
                    publication_journal_removed=False,
                    durable_note=(
                        "durable generation lies outside the configured runs root; "
                        "its publication files were left alone"
                    ),
                )
            if _other_live_workspace_targets(root, root_fd, workspace_name, report.durable_dir):
                return ScratchWorkspaceRemoval(
                    report=report,
                    removed_durable_entries=(),
                    publication_journal_removed=False,
                    durable_note=(
                        "a live workspace still publishes into this durable generation; "
                        "its publication files were left alone"
                    ),
                )
            removed, journal_removed, note = _clean_durable_publication_leftovers(
                Path(report.durable_dir)
            )
            return ScratchWorkspaceRemoval(
                report=report,
                removed_durable_entries=removed,
                publication_journal_removed=journal_removed,
                durable_note=note,
            )
    finally:
        os.close(root_fd)


def _durable_dir_within_root(durable_dir: str, durable_root: Path | None) -> bool:
    """Whether a manifest's durable generation may be cleaned: only under the runs root."""

    if durable_root is None:
        return False
    try:
        resolved_root = durable_root.expanduser().resolve(strict=True)
        resolved_dir = Path(durable_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved_dir != resolved_root and resolved_dir.is_relative_to(resolved_root)


def _other_live_workspace_targets(
    root: Path,
    root_fd: int,
    removed_name: str,
    durable_dir: str,
) -> bool:
    for name in os.listdir(root_fd):
        if name == removed_name or not name.startswith(SCRATCH_WORKSPACE_PREFIX):
            continue
        try:
            report, _identity = _inspect_workspace_entry(root, root_fd, name, measure_size=False)
        except (OSError, EngineScratchError):
            continue
        if report.state == SCRATCH_STATE_LIVE and report.durable_dir == durable_dir:
            return True
    return False


def _clean_durable_publication_leftovers(
    durable_dir: Path,
) -> tuple[tuple[str, ...], bool, str | None]:
    try:
        durable_dir_fd, _identity = _open_pinned_directory(
            durable_dir,
            label="engine durable generation",
        )
    except (OSError, EngineScratchError) as exc:
        return (), False, f"durable generation could not be opened: {exc}"
    try:
        try:
            loaded = _load_publication_journal(durable_dir_fd, durable_dir)
        except (OSError, EngineScratchError) as exc:
            return (), False, f"publication journal left in place: {exc}"
        journaled: set[str] = set()
        if loaded is not None:
            for item in loaded[1]:
                journaled.add(item.temporary_name)
                if item.backup_name is not None:
                    journaled.add(item.backup_name)
        removed: list[str] = []
        for name in sorted(os.listdir(durable_dir_fd)):
            if _PUBLICATION_TEMP_NAME_RE.fullmatch(name) is None or name in journaled:
                continue
            info = os.stat(name, dir_fd=durable_dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            os.unlink(name, dir_fd=durable_dir_fd)
            removed.append(name)
        journal_removed = False
        note: str | None = None
        if loaded is not None:
            phase, _items = loaded
            if phase == "committed" and not any(
                _entry_exists_at(durable_dir_fd, name) for name in journaled
            ):
                _unlink_at_if_present(durable_dir_fd, _PUBLICATION_JOURNAL_FILE_NAME)
                journal_removed = True
            else:
                note = (
                    f"publication journal ({phase}) left in place for replay by the next "
                    "scratch launch into this generation"
                )
        if removed or journal_removed:
            os.fsync(durable_dir_fd)
        return tuple(removed), journal_removed, note
    finally:
        os.close(durable_dir_fd)
