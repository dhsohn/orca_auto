from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from orca_auto.core.queue.cancellable import retain_process_ownership_until_exit
from orca_auto.core.queue.processes import terminate_process_group
from orca_auto.core.utils.persistence import durable_mkdir, fsync_directory, open_pinned_readonly


@dataclass(frozen=True)
class LoggedEngineProcess:
    process: subprocess.Popen[str]
    started_at: str
    stdout_log: Path
    stderr_log: Path
    stdout_handle: TextIO
    stderr_handle: TextIO


def ensure_confined_directory(root: Path, path: Path, *, label: str) -> Path:
    """Create an engine-owned directory without following an escape symlink."""

    resolved_root = root.expanduser().resolve()
    candidate = path.expanduser()
    prospective_path = candidate.resolve(strict=False)
    if not prospective_path.is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside the job directory: {path}")
    current = candidate
    while current != resolved_root and current != current.parent:
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symlinks: {current}")
        current = current.parent
    durable_mkdir(path, parents=True, exist_ok=True)
    resolved_path = path.expanduser().resolve()
    if not path.is_dir() or not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside the job directory: {path}")
    return resolved_path


def require_confined_regular_file(root: Path, path: Path, *, label: str) -> Path:
    resolved_root = root.expanduser().resolve()
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside its root: {path}")
    try:
        file_status = path.stat()
    except OSError as exc:
        raise ValueError(f"{label} is not a readable regular file: {path}") from exc
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
        raise ValueError(f"{label} must be a single-link regular file: {path}")
    return resolved_path


def recreate_confined_directory(root: Path, path: Path, *, label: str) -> Path:
    """Recreate an engine-owned directory so stale entries cannot redirect outputs."""

    resolved_root = root.expanduser().resolve()
    prospective_path = path.expanduser().resolve(strict=False)
    if not prospective_path.is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside the job directory: {path}")
    current = path.expanduser()
    while current != resolved_root and current != current.parent:
        if current.is_symlink():
            raise ValueError(f"{label} must not contain symlinks: {current}")
        current = current.parent
    if path.exists():
        resolved_path = path.expanduser().resolve()
        if not path.is_dir() or not resolved_path.is_relative_to(resolved_root):
            raise ValueError(f"{label} must stay inside the job directory: {path}")
        shutil.rmtree(path)
        fsync_directory(path.parent)
    durable_mkdir(path, parents=True, exist_ok=False)
    resolved_path = path.expanduser().resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside the job directory: {path}")
    return resolved_path


def thread_limited_env(base_env: Mapping[str, str], max_cores: int) -> dict[str, str]:
    thread_count = str(max(1, int(max_cores)))
    return {
        **base_env,
        "OMP_NUM_THREADS": thread_count,
        "OPENBLAS_NUM_THREADS": thread_count,
        "MKL_NUM_THREADS": thread_count,
        "NUMEXPR_NUM_THREADS": thread_count,
    }


def open_confined_log(
    cwd: Path,
    log_path: Path,
    *,
    label: str,
    append: bool = False,
    readable: bool = False,
) -> TextIO:
    resolved_cwd = cwd.expanduser().resolve()
    parent = log_path.parent
    if not parent.expanduser().resolve().is_relative_to(resolved_cwd):
        raise ValueError(f"Engine {label} must stay inside the job directory: {log_path}")
    directory_flags = os.O_RDONLY
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    try:
        file_flags = (os.O_RDWR if readable else os.O_WRONLY) | os.O_CREAT
        if append:
            file_flags |= os.O_APPEND
        file_flags |= os.O_NONBLOCK
        file_flags |= os.O_NOFOLLOW
        descriptor = os.open(log_path.name, file_flags, 0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    file_status = os.fstat(descriptor)
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"Engine {label} must be a single-link regular file: {log_path}")
    if not append:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
    return os.fdopen(descriptor, "r+" if readable else "w", encoding="utf-8")


def read_confined_text(
    root: Path,
    path: Path,
    *,
    label: str,
    max_links: int = 1,
    max_bytes: int | None = None,
) -> str:
    """Read stable UTF-8 text through pinned parent and file descriptors."""

    if max_links < 1:
        raise ValueError("max_links must be positive")
    if max_bytes is not None and max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    resolved_root = root.expanduser().resolve()
    parent = path.parent
    if parent.is_symlink() or not parent.expanduser().resolve().is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside its root: {path}")
    directory_flags = os.O_RDONLY
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    descriptor = -1
    try:
        opened_parent = os.fstat(directory_fd)
        descriptor = open_pinned_readonly(path.name, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or before.st_nlink > max_links:
            raise ValueError(f"{label} must be a single-link regular file: {path}")
        if max_bytes is not None and before.st_size > max_bytes:
            raise ValueError(f"{label} exceeds its read limit: {path}")
        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            read_size = 1024 * 1024
            if max_bytes is not None:
                read_size = min(read_size, max_bytes - total_bytes + 1)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            total_bytes += len(chunk)
            if max_bytes is not None and total_bytes > max_bytes:
                raise ValueError(f"{label} exceeds its read limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        visible_file = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        visible_parent = os.stat(parent, follow_symlinks=False)
        file_identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_size),
            int(before.st_mtime_ns),
        )
        if file_identity != (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_mode),
            int(after.st_size),
            int(after.st_mtime_ns),
        ) or file_identity != (
            int(visible_file.st_dev),
            int(visible_file.st_ino),
            int(visible_file.st_mode),
            int(visible_file.st_size),
            int(visible_file.st_mtime_ns),
        ):
            raise ValueError(f"{label} changed while it was read: {path}")
        for details in (after, visible_file):
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink < 1
                or details.st_nlink > max_links
            ):
                raise ValueError(f"{label} must be a single-link regular file: {path}")
        if max_links == 1 and not (
            before.st_ctime_ns == after.st_ctime_ns == visible_file.st_ctime_ns
        ):
            raise ValueError(f"{label} changed while it was read: {path}")
        if (
            int(opened_parent.st_dev),
            int(opened_parent.st_ino),
        ) != (
            int(visible_parent.st_dev),
            int(visible_parent.st_ino),
        ):
            raise ValueError(f"{label} parent directory changed while it was read: {parent}")
        payload = b"".join(chunks)
        if len(payload) != after.st_size:
            raise ValueError(f"{label} changed while it was read: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return payload.decode("utf-8", errors="strict")


def read_confined_tail_lines(
    root: Path,
    path: Path,
    *,
    label: str,
    max_lines: int,
    max_bytes: int = 8 * 1024 * 1024,
) -> list[str]:
    """Read a bounded suffix of a confined ASCII-compatible line log."""

    if max_lines <= 0:
        return []
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    resolved_root = root.expanduser().resolve()
    parent = path.parent
    if parent.is_symlink() or not parent.expanduser().resolve().is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside its root: {path}")
    directory_flags = os.O_RDONLY
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    try:
        descriptor = open_pinned_readonly(path.name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    file_status = os.fstat(descriptor)
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"{label} must be a single-link regular file: {path}")
    start = max(0, int(file_status.st_size) - max_bytes)
    try:
        os.lseek(descriptor, start, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = int(file_status.st_size) - start
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if start > 0:
        separator = payload.find(b"\n")
        payload = b"" if separator < 0 else payload[separator + 1 :]
    return payload.decode("utf-8").splitlines()[-max_lines:]


def atomic_write_confined_bytes(
    root: Path,
    path: Path,
    payload: bytes,
    *,
    label: str,
    mode: int = 0o600,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Atomically replace a regular file without following a final symlink."""

    resolved_root = root.expanduser().resolve()
    parent = path.parent
    if parent.is_symlink() or not parent.expanduser().resolve().is_relative_to(resolved_root):
        raise ValueError(f"{label} must stay inside its engine directory: {path}")
    directory_flags = os.O_RDONLY
    directory_flags |= os.O_DIRECTORY
    directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    parent_status = os.fstat(directory_fd)
    if (
        expected_parent_identity is not None
        and (
            int(parent_status.st_dev),
            int(parent_status.st_ino),
        )
        != expected_parent_identity
    ):
        os.close(directory_fd)
        raise ValueError(f"{label} parent directory identity changed: {parent}")
    temporary_name = f".{path.name}.{secrets.token_hex(12)}.tmp"
    descriptor = -1
    try:
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, file_flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def start_logged_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_log: Path,
    stderr_log: Path,
    max_cores: int,
    base_env: Mapping[str, str],
    now_utc_iso_fn: Callable[[], str],
    popen_fn: Callable[..., subprocess.Popen[str]],
    stdin_value: object,
    preexec_fn: Callable[[], None] | None,
) -> LoggedEngineProcess:
    started_at = now_utc_iso_fn()
    env = thread_limited_env(base_env, max_cores)
    stdout_handle = open_confined_log(cwd, stdout_log, label="stdout log")
    try:
        stderr_handle = open_confined_log(cwd, stderr_log, label="stderr log")
    except Exception:
        stdout_handle.close()
        raise
    try:
        process = popen_fn(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=stdin_value,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )
    except Exception:
        stdout_handle.close()
        stderr_handle.close()
        raise
    return LoggedEngineProcess(
        process=process,
        started_at=started_at,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
    )


def cleanup_failed_logged_process_start(
    launched: LoggedEngineProcess,
    *,
    terminate_process_fn: Callable[[Any], bool] = terminate_process_group,
) -> None:
    """Clean a process that launched before its running-job object could be built."""
    try:
        terminated = terminate_process_fn(launched.process)
    except Exception:  # noqa: BLE001
        terminated = False
    if terminated is not True:
        retain_process_ownership_until_exit(
            launched.process,
            terminate_process=terminate_process_fn,
        )
    launched.stdout_handle.close()
    launched.stderr_handle.close()


__all__ = [
    "LoggedEngineProcess",
    "atomic_write_confined_bytes",
    "cleanup_failed_logged_process_start",
    "ensure_confined_directory",
    "open_confined_log",
    "recreate_confined_directory",
    "read_confined_tail_lines",
    "read_confined_text",
    "require_confined_regular_file",
    "start_logged_process",
    "thread_limited_env",
]
