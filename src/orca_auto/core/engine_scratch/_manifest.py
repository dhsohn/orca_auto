"""The per-workspace ownership manifest and the per-entry classification built
on it: ``_write_workspace_manifest`` binds a workspace to this boot and
process, ``_read_workspace_manifest_at`` / ``_classify_workspace_manifest``
turn a manifest into a ``SCRATCH_STATE_*`` value with the sweep's refusal
message, and ``_inspect_workspace_entry`` produces the
``ScratchWorkspaceReport`` used by both the launch sweep and ``scratch list``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils import stable_fs

from ._constants import (
    _INSPECT_SIZE_WALK_MAX_ENTRIES,
    _MANIFEST_MAX_BYTES,
    _WORKSPACE_MANIFEST_SCHEMA_VERSION,
    SCRATCH_MANIFEST_FILE_NAME,
    SCRATCH_STATE_INVALID_MANIFEST,
    SCRATCH_STATE_LIVE,
    SCRATCH_STATE_STALE,
    SCRATCH_STATE_UNSAFE,
    SCRATCH_STATE_UNVERIFIABLE,
)
from ._errors import EngineScratchError
from ._fs import (
    _atomic_write_bytes_at,
    _open_pinned_directory_at,
    _read_stable_regular_file_at,
)
from ._reports import ScratchWorkspaceReport


def _write_workspace_manifest(
    durable_dir: Path,
    *,
    workspace_dir_fd: int,
    max_task_memory_bytes: int,
) -> None:
    boot_id = process_utils.linux_boot_id(proc_root=Path("/proc"))
    owner_ticks = process_utils.current_process_start_ticks()
    if not boot_id or owner_ticks is None:
        raise EngineScratchError("Cannot bind engine scratch ownership to this boot and process")
    payload = {
        "schema_version": _WORKSPACE_MANIFEST_SCHEMA_VERSION,
        "owner_pid": os.getpid(),
        "owner_process_start_ticks": owner_ticks,
        "owner_boot_id": boot_id,
        "durable_dir": str(durable_dir.resolve()),
        "max_task_memory_bytes": max_task_memory_bytes,
    }
    _atomic_write_bytes_at(
        workspace_dir_fd,
        SCRATCH_MANIFEST_FILE_NAME,
        (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _manifest_owner_state(payload: dict[str, Any]) -> str:
    """Classify a workspace owner as "live", "stale" or "unknown".

    Only a proven-live owner may be counted toward the launch guard; an
    unknown owner must block, because admitting it would let an unverifiable
    workspace pin tmpfs forever.
    """
    owner_pid = payload.get("owner_pid")
    owner_ticks = payload.get("owner_process_start_ticks")
    owner_boot = payload.get("owner_boot_id")
    if type(owner_pid) is not int or owner_pid <= 0:
        return "unknown"
    if type(owner_ticks) is not int or owner_ticks <= 0 or not isinstance(owner_boot, str):
        return "unknown"
    current_boot = process_utils.linux_boot_id(proc_root=Path("/proc"))
    if not current_boot:
        return "unknown"
    if current_boot != owner_boot:
        return "stale"
    if not process_utils.is_process_alive(owner_pid):
        return "stale"
    observed_ticks = process_utils.process_start_ticks(owner_pid)
    if observed_ticks is None:
        return "unknown"
    return "live" if observed_ticks == owner_ticks else "stale"


def _read_workspace_manifest_at(workspace_fd: int, workspace: Path) -> dict[str, Any] | None:
    """Return the parsed manifest mapping, or ``None`` when it is missing or invalid."""

    try:
        raw_payload, _mode = _read_stable_regular_file_at(
            workspace_fd,
            SCRATCH_MANIFEST_FILE_NAME,
            display_path=workspace / SCRATCH_MANIFEST_FILE_NAME,
            max_bytes=_MANIFEST_MAX_BYTES,
        )
        parsed = json.loads(raw_payload.decode("utf-8", errors="strict"))
    except (OSError, ValueError, EngineScratchError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _classify_workspace_manifest(
    payload: dict[str, Any] | None,
    workspace: Path,
) -> tuple[str, str | None]:
    """Return ``(state, detail)`` for a workspace manifest.

    ``detail`` is the sweep's refusal message and is ``None`` only for a live
    workspace. The message texts are part of the operator-facing contract.
    """

    task_memory = payload.get("max_task_memory_bytes") if payload is not None else None
    if (
        payload is None
        or payload.get("schema_version") != _WORKSPACE_MANIFEST_SCHEMA_VERSION
        or type(task_memory) is not int
        or task_memory < 1
    ):
        return (
            SCRATCH_STATE_INVALID_MANIFEST,
            f"engine scratch contains an unresolved workspace without valid ownership: {workspace}",
        )
    owner_state = _manifest_owner_state(payload)
    if owner_state == "stale":
        return (
            SCRATCH_STATE_STALE,
            "engine scratch contains a stale workspace with uncertain child ownership; "
            f"preserving it for inspection: {workspace}",
        )
    if owner_state != "live":
        return (
            SCRATCH_STATE_UNVERIFIABLE,
            "engine scratch contains a workspace whose owner cannot be verified; "
            f"preserving it for inspection: {workspace}",
        )
    return SCRATCH_STATE_LIVE, None


def _manifest_int(payload: dict[str, Any] | None, key: str) -> int | None:
    value = payload.get(key) if payload is not None else None
    return value if type(value) is int else None


def _manifest_str(payload: dict[str, Any] | None, key: str) -> str | None:
    value = payload.get(key) if payload is not None else None
    return value if isinstance(value, str) else None


def _workspace_size_bytes(directory_fd: int, *, max_entries: int) -> tuple[int, bool]:
    """Sum regular-file sizes below ``directory_fd`` without following symlinks.

    The walk stops once ``max_entries`` directory entries were visited and
    reports ``truncated``; an unreadable subtree also reports ``truncated``
    rather than failing the inspection.
    """

    total = 0
    visited = 0
    truncated = False
    pending: list[int] = [os.dup(directory_fd)]
    try:
        while pending and visited <= max_entries:
            current_fd = pending.pop()
            try:
                with os.scandir(current_fd) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > max_entries:
                            truncated = True
                            break
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            truncated = True
                            continue
                        if stat.S_ISREG(info.st_mode):
                            total += int(info.st_size)
                        elif stat.S_ISDIR(info.st_mode):
                            try:
                                pending.append(
                                    os.open(
                                        entry.name,
                                        stable_fs.directory_open_flags(),
                                        dir_fd=current_fd,
                                    )
                                )
                            except OSError:
                                truncated = True
            except OSError:
                truncated = True
            finally:
                os.close(current_fd)
    finally:
        for descriptor in pending:
            os.close(descriptor)
    return total, truncated


def _inspect_workspace_entry(
    root: Path,
    root_fd: int,
    name: str,
    *,
    measure_size: bool,
    max_size_entries: int = _INSPECT_SIZE_WALK_MAX_ENTRIES,
) -> tuple[ScratchWorkspaceReport, tuple[int, int] | None]:
    """Classify one ``attempt-*`` entry; return its report and pinned identity."""

    candidate = root / name
    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        return (
            ScratchWorkspaceReport(
                path=candidate,
                name=name,
                state=SCRATCH_STATE_UNSAFE,
                manifest_valid=False,
                owner_pid=None,
                owner_process_start_ticks=None,
                owner_boot_id=None,
                durable_dir=None,
                max_task_memory_bytes=None,
                size_bytes=int(info.st_size) if stat.S_ISREG(info.st_mode) else 0,
                size_walk_truncated=False,
                blocks_launch=True,
                detail=f"engine scratch root contains an unsafe entry: {candidate}",
            ),
            None,
        )
    workspace_fd, workspace_identity = _open_pinned_directory_at(
        root_fd,
        name,
        display_path=candidate,
        label="engine scratch workspace",
    )
    try:
        payload = _read_workspace_manifest_at(workspace_fd, candidate)
        state, detail = _classify_workspace_manifest(payload, candidate)
        size_bytes, size_truncated = (
            _workspace_size_bytes(workspace_fd, max_entries=max_size_entries)
            if measure_size
            else (0, True)
        )
    finally:
        os.close(workspace_fd)
    return (
        ScratchWorkspaceReport(
            path=candidate,
            name=name,
            state=state,
            manifest_valid=state != SCRATCH_STATE_INVALID_MANIFEST,
            owner_pid=_manifest_int(payload, "owner_pid"),
            owner_process_start_ticks=_manifest_int(payload, "owner_process_start_ticks"),
            owner_boot_id=_manifest_str(payload, "owner_boot_id"),
            durable_dir=_manifest_str(payload, "durable_dir"),
            max_task_memory_bytes=_manifest_int(payload, "max_task_memory_bytes"),
            size_bytes=size_bytes,
            size_walk_truncated=size_truncated,
            blocks_launch=state != SCRATCH_STATE_LIVE,
            detail=detail,
        ),
        workspace_identity,
    )
