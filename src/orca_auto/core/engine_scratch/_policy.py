"""The operator policy for a scratch root and the host probes that gate a launch:
``EngineScratchPolicy`` confines the root below ``_SCRATCH_ROOT_PARENT``
(``/dev/shm``), ``_prepare_scratch_root`` creates and verifies each path
component under that parent, and ``_filesystem_free_bytes`` /
``_linux_available_memory_bytes`` read the tmpfs and RAM headroom that
``EngineScratchWorkspace.create`` compares against the policy.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._errors import EngineScratchError

_SCRATCH_ROOT_PARENT = Path("/dev/shm")


@dataclass(frozen=True)
class EngineScratchPolicy:
    root: Path
    min_free_bytes: int
    max_task_memory_bytes: int
    dependency_names_from_primary: Callable[[bytes], Sequence[str]] | None = None
    normalize_primary_newline: bool = False

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve(strict=False)
        parent = _SCRATCH_ROOT_PARENT.resolve()
        if root == parent or not root.is_relative_to(parent):
            raise ValueError("Engine scratch root must be a dedicated directory below /dev/shm")
        if self.min_free_bytes < 1:
            raise ValueError("Engine scratch minimum free bytes must be positive")
        if self.max_task_memory_bytes < 1:
            raise ValueError("Engine task memory limit must be positive")
        object.__setattr__(self, "root", root)


def _linux_available_memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(":")
            if key == "MemAvailable" and separator:
                amount, unit = value.split()
                if unit != "kB":
                    break
                available = int(amount) * 1024
                if available > 0:
                    return available
                break
    except (OSError, ValueError):
        pass
    raise EngineScratchError("Cannot determine available host memory for engine scratch")


def _prepare_scratch_root(policy: EngineScratchPolicy) -> Path:
    parent = _SCRATCH_ROOT_PARENT
    if parent.is_symlink() or not parent.is_dir():
        raise EngineScratchError("/dev/shm is unavailable or unsafe")
    resolved_parent = parent.resolve()
    if policy.root.parent != resolved_parent and not policy.root.parent.is_relative_to(
        resolved_parent
    ):
        raise EngineScratchError("engine scratch root escaped /dev/shm")
    current = resolved_parent
    for component in policy.root.relative_to(resolved_parent).parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise EngineScratchError(f"engine scratch path component is unsafe: {current}")
        if info.st_uid != os.getuid():
            raise EngineScratchError(
                f"engine scratch path is not owned by the worker user: {current}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise EngineScratchError(
                f"engine scratch path is accessible by another user: {current}"
            )
    if policy.root.stat().st_dev != resolved_parent.stat().st_dev:
        raise EngineScratchError("engine scratch root is not on the /dev/shm filesystem")
    return policy.root


def _filesystem_free_bytes(directory_fd: int) -> int:
    details = os.fstatvfs(directory_fd)
    return int(details.f_bavail) * int(details.f_frsize)
