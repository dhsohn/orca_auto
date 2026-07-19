from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from .persistence import now_utc_iso

_TMPFS_ROOT = Path("/dev/shm")
_TMPFS_LOCK_PROTOCOL = b"orca_auto.tmpfs_file_lock.v1"
_TMPFS_LOCK_NAMESPACE_PREFIX = "orca_auto-locks-"
_TMPFS_LOCK_STRIPE_COUNT = 4096
_TMPFS_LOCK_STRIPE_FILENAME_PREFIX = "orca_auto-tmpfs-file-lock-v1-stripe-"

if _TMPFS_LOCK_STRIPE_COUNT <= 0 or (_TMPFS_LOCK_STRIPE_COUNT & (_TMPFS_LOCK_STRIPE_COUNT - 1)):
    raise RuntimeError("Tmpfs lock stripe count must be a positive power of two")


_TmpfsLockKey = tuple[int, int, int, str]


@dataclass(frozen=True)
class _HeldTmpfsLock:
    pid: int
    descriptor: int
    registration: object
    status: os.stat_result


class _TmpfsLockThreadState(threading.local):
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.held: dict[_TmpfsLockKey, _HeldTmpfsLock] = {}


_TMPFS_FLOCK_REGISTRY_GUARD = threading.RLock()
_TMPFS_REGISTERED_FLOCKS: dict[int, object] = {}
_TMPFS_LOCK_THREAD_STATE = _TmpfsLockThreadState()


def _current_thread_tmpfs_locks() -> dict[_TmpfsLockKey, _HeldTmpfsLock]:
    pid = os.getpid()
    if _TMPFS_LOCK_THREAD_STATE.pid != pid:
        _TMPFS_LOCK_THREAD_STATE.pid = pid
        _TMPFS_LOCK_THREAD_STATE.held = {}
    return _TMPFS_LOCK_THREAD_STATE.held


def _register_tmpfs_flock(descriptor: int) -> object:
    registration = object()
    with _TMPFS_FLOCK_REGISTRY_GUARD:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _TMPFS_REGISTERED_FLOCKS[descriptor] = registration
    return registration


def _release_registered_tmpfs_flock(descriptor: int, registration: object) -> None:
    with _TMPFS_FLOCK_REGISTRY_GUARD:
        if _TMPFS_REGISTERED_FLOCKS.get(descriptor) is not registration:
            return
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        _TMPFS_REGISTERED_FLOCKS.pop(descriptor, None)
        with suppress(OSError):
            os.close(descriptor)


def _before_tmpfs_lock_fork() -> None:
    _TMPFS_FLOCK_REGISTRY_GUARD.acquire()


def _after_tmpfs_lock_fork_in_parent() -> None:
    _TMPFS_FLOCK_REGISTRY_GUARD.release()


def _after_tmpfs_lock_fork_in_child() -> None:
    global _TMPFS_LOCK_THREAD_STATE

    try:
        # Closing the inherited duplicate does not unlock the parent's shared
        # open-file description. Never issue LOCK_UN from the child here.
        for descriptor in tuple(_TMPFS_REGISTERED_FLOCKS):
            with suppress(OSError):
                os.close(descriptor)
        _TMPFS_REGISTERED_FLOCKS.clear()
        _TMPFS_LOCK_THREAD_STATE = _TmpfsLockThreadState()
    finally:
        _TMPFS_FLOCK_REGISTRY_GUARD.release()


os.register_at_fork(
    before=_before_tmpfs_lock_fork,
    after_in_parent=_after_tmpfs_lock_fork_in_parent,
    after_in_child=_after_tmpfs_lock_fork_in_child,
)


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _canonical_logical_lock_path(lock_path: Path) -> Path:
    expanded = Path(lock_path).expanduser()
    return expanded.parent.resolve(strict=False) / expanded.name


def _tmpfs_lock_stripe_name(identity: bytes) -> str:
    digest = hashlib.sha256(identity).digest()
    stripe_index = int.from_bytes(digest, byteorder="big") & (_TMPFS_LOCK_STRIPE_COUNT - 1)
    return f"{_TMPFS_LOCK_STRIPE_FILENAME_PREFIX}{stripe_index:04d}.lock"


def _tmpfs_lock_name(logical_lock_path: Path, *, effective_uid: int) -> str:
    canonical = _canonical_logical_lock_path(logical_lock_path)
    identity = b"\0".join(
        (
            _TMPFS_LOCK_PROTOCOL,
            str(effective_uid).encode("ascii"),
            os.fsencode(str(canonical)),
        )
    )
    return _tmpfs_lock_stripe_name(identity)


def _tmpfs_lock_name_at(
    directory_status: os.stat_result,
    lock_name: str,
    *,
    effective_uid: int,
) -> str:
    identity = b"\0".join(
        (
            _TMPFS_LOCK_PROTOCOL,
            str(effective_uid).encode("ascii"),
            b"directory-inode",
            str(int(directory_status.st_dev)).encode("ascii"),
            str(int(directory_status.st_ino)).encode("ascii"),
            os.fsencode(lock_name),
        )
    )
    return _tmpfs_lock_stripe_name(identity)


def _validate_plain_lock_name(lock_name: str) -> None:
    if not lock_name or Path(lock_name).name != lock_name or lock_name in {".", ".."}:
        raise ValueError(f"Lock name must be one plain filename: {lock_name!r}")


def _validate_tmpfs_lock_directory_status(
    status: os.stat_result,
    *,
    effective_uid: int,
    display_path: Path,
) -> None:
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"Tmpfs lock directory fd must reference a directory: {display_path}")
    if status.st_uid != effective_uid:
        raise ValueError(
            f"Tmpfs lock directory fd must be owned by uid={effective_uid}: {display_path}"
        )


def _validate_tmpfs_namespace_status(
    status: os.stat_result,
    *,
    effective_uid: int,
    root_device: int,
    display_path: Path,
) -> None:
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError(f"Tmpfs lock namespace must be a directory: {display_path}")
    if status.st_uid != effective_uid:
        raise ValueError(
            f"Tmpfs lock namespace must be owned by uid={effective_uid}: {display_path}"
        )
    if stat.S_IMODE(status.st_mode) != 0o700:
        raise ValueError(f"Tmpfs lock namespace must have mode 0700: {display_path}")
    if status.st_dev != root_device:
        raise ValueError(f"Tmpfs lock namespace must be on /dev/shm: {display_path}")


def _validate_tmpfs_lock_status(
    status: os.stat_result,
    *,
    effective_uid: int,
    display_path: Path,
) -> None:
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise ValueError(f"Tmpfs lock must be a single-link regular file: {display_path}")
    if status.st_uid != effective_uid or stat.S_IMODE(status.st_mode) != 0o600:
        raise ValueError(f"Tmpfs lock must be an owner-private mode 0600 file: {display_path}")


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _validate_reentrant_tmpfs_lock(
    held: _HeldTmpfsLock,
    namespace_fd: int,
    lock_name: str,
    *,
    effective_uid: int,
    display_path: Path,
) -> None:
    if held.pid != os.getpid():
        raise ValueError(f"Inherited tmpfs lock state cannot be reused: {display_path}")
    with _TMPFS_FLOCK_REGISTRY_GUARD:
        registered = _TMPFS_REGISTERED_FLOCKS.get(held.descriptor) is held.registration
    if not registered:
        raise ValueError(f"Tmpfs lock is no longer held safely: {display_path}")
    opened = os.fstat(held.descriptor)
    _validate_tmpfs_lock_status(
        opened,
        effective_uid=effective_uid,
        display_path=display_path,
    )
    named = _named_status(namespace_fd, lock_name, display_path=display_path)
    _validate_tmpfs_lock_status(
        named,
        effective_uid=effective_uid,
        display_path=display_path,
    )
    if not _same_inode(held.status, opened) or not _same_inode(held.status, named):
        raise ValueError(f"Tmpfs lock changed during reentrant acquisition: {display_path}")


def _named_status(directory_fd: int, name: str, *, display_path: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"Tmpfs lock path cannot be verified safely: {display_path}") from exc


def _open_tmpfs_lock_namespace(
    root_fd: int,
    root_status: os.stat_result,
    *,
    effective_uid: int,
) -> tuple[int, str, os.stat_result]:
    namespace_name = f"{_TMPFS_LOCK_NAMESPACE_PREFIX}{effective_uid}"
    namespace_path = _TMPFS_ROOT / namespace_name
    try:
        os.mkdir(namespace_name, mode=0o700, dir_fd=root_fd)
    except FileExistsError:
        pass

    named = _named_status(root_fd, namespace_name, display_path=namespace_path)
    _validate_tmpfs_namespace_status(
        named,
        effective_uid=effective_uid,
        root_device=int(root_status.st_dev),
        display_path=namespace_path,
    )
    try:
        namespace_fd = os.open(
            namespace_name,
            _directory_open_flags(),
            dir_fd=root_fd,
        )
    except OSError as exc:
        raise ValueError(f"Tmpfs lock namespace cannot be opened safely: {namespace_path}") from exc
    opened = os.fstat(namespace_fd)
    try:
        _validate_tmpfs_namespace_status(
            opened,
            effective_uid=effective_uid,
            root_device=int(root_status.st_dev),
            display_path=namespace_path,
        )
        if not _same_inode(named, opened):
            raise ValueError(f"Tmpfs lock namespace changed while opening: {namespace_path}")
    except BaseException:
        os.close(namespace_fd)
        raise
    return namespace_fd, namespace_name, opened


def _open_tmpfs_lock_file(
    namespace_fd: int,
    lock_name: str,
    *,
    effective_uid: int,
    display_path: Path,
) -> tuple[int, os.stat_result]:
    flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(lock_name, flags, 0o600, dir_fd=namespace_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise ValueError(f"Tmpfs lock cannot be opened safely: {display_path}") from exc
        raise
    opened = os.fstat(descriptor)
    try:
        _validate_tmpfs_lock_status(
            opened,
            effective_uid=effective_uid,
            display_path=display_path,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


@contextmanager
def _tmpfs_lock_by_name(
    lock_name: str,
    *,
    effective_uid: int,
    timeout_seconds: float,
    timeout_path: Path,
) -> Iterator[None]:
    root_fd = os.open(_TMPFS_ROOT, _directory_open_flags())
    namespace_fd = -1
    descriptor = -1
    registration: object | None = None
    try:
        root_status = os.fstat(root_fd)
        namespace_fd, namespace_name, namespace_status = _open_tmpfs_lock_namespace(
            root_fd,
            root_status,
            effective_uid=effective_uid,
        )
        namespace_path = _TMPFS_ROOT / namespace_name
        actual_lock_path = namespace_path / lock_name
        lock_key: _TmpfsLockKey = (
            int(namespace_status.st_dev),
            int(namespace_status.st_ino),
            effective_uid,
            lock_name,
        )
        thread_locks = _current_thread_tmpfs_locks()
        held = thread_locks.get(lock_key)
        if held is not None:
            _validate_reentrant_tmpfs_lock(
                held,
                namespace_fd,
                lock_name,
                effective_uid=effective_uid,
                display_path=actual_lock_path,
            )
            yield
            return

        deadline = time.monotonic() + timeout_seconds
        opened_status: os.stat_result | None = None
        while descriptor < 0:
            candidate, candidate_status = _open_tmpfs_lock_file(
                namespace_fd,
                lock_name,
                effective_uid=effective_uid,
                display_path=actual_lock_path,
            )
            try:
                candidate_registration = _register_tmpfs_flock(candidate)
            except BlockingIOError:
                os.close(candidate)
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {timeout_path}") from None
                time.sleep(0.1)
                continue
            except BaseException:
                os.close(candidate)
                raise
            descriptor = candidate
            registration = candidate_registration
            opened_status = candidate_status

        try:
            current_namespace = _named_status(
                root_fd,
                namespace_name,
                display_path=namespace_path,
            )
            _validate_tmpfs_namespace_status(
                current_namespace,
                effective_uid=effective_uid,
                root_device=int(root_status.st_dev),
                display_path=namespace_path,
            )
            if not _same_inode(namespace_status, current_namespace):
                raise ValueError(
                    f"Tmpfs lock namespace changed during acquisition: {namespace_path}"
                )

            current_lock = _named_status(
                namespace_fd,
                lock_name,
                display_path=actual_lock_path,
            )
            _validate_tmpfs_lock_status(
                current_lock,
                effective_uid=effective_uid,
                display_path=actual_lock_path,
            )
            if opened_status is None or not _same_inode(opened_status, current_lock):
                raise ValueError(f"Tmpfs lock changed during acquisition: {actual_lock_path}")

            with os.fdopen(descriptor, "r+", encoding="utf-8", closefd=False) as handle:
                handle.seek(0)
                handle.truncate()
                handle.write(f"pid={os.getpid()}\nacquired_at={now_utc_iso()}\n")
                handle.flush()
                os.fsync(handle.fileno())

            current_lock = _named_status(
                namespace_fd,
                lock_name,
                display_path=actual_lock_path,
            )
            _validate_tmpfs_lock_status(
                current_lock,
                effective_uid=effective_uid,
                display_path=actual_lock_path,
            )
            if not _same_inode(opened_status, current_lock):
                raise ValueError(f"Tmpfs lock changed during acquisition: {actual_lock_path}")
            if registration is None:
                raise RuntimeError(f"Tmpfs lock registration is missing: {actual_lock_path}")
            held = _HeldTmpfsLock(
                pid=os.getpid(),
                descriptor=descriptor,
                registration=registration,
                status=opened_status,
            )
            thread_locks[lock_key] = held
            try:
                yield
            finally:
                if thread_locks.get(lock_key) is held:
                    thread_locks.pop(lock_key, None)
        finally:
            if descriptor >= 0 and registration is not None:
                _release_registered_tmpfs_flock(descriptor, registration)
    finally:
        if namespace_fd >= 0:
            with suppress(OSError):
                os.close(namespace_fd)
        with suppress(OSError):
            os.close(root_fd)


@contextmanager
def tmpfs_file_lock(
    logical_lock_path: Path,
    *,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Lock a canonical logical path in a private, per-user /dev/shm namespace."""

    canonical = _canonical_logical_lock_path(logical_lock_path)
    effective_uid = os.geteuid()
    with _tmpfs_lock_by_name(
        _tmpfs_lock_name(canonical, effective_uid=effective_uid),
        effective_uid=effective_uid,
        timeout_seconds=timeout_seconds,
        timeout_path=canonical,
    ):
        yield


@contextmanager
def tmpfs_file_lock_at(
    directory_fd: int,
    lock_name: str,
    *,
    display_path: Path | None = None,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Lock one literal leaf by its already-pinned directory inode in /dev/shm.

    The logical leaf is never opened or created. A duplicate of ``directory_fd``
    pins the directory identity for the full critical section, so rename and
    bind-style aliases of the same directory contend on one tmpfs inode.
    """

    _validate_plain_lock_name(lock_name)
    shown_path = display_path or Path(lock_name)
    effective_uid = os.geteuid()
    try:
        pinned_directory_fd = os.dup(directory_fd)
    except OSError as exc:
        raise ValueError(f"Tmpfs lock directory fd cannot be pinned safely: {shown_path}") from exc
    try:
        directory_status = os.fstat(pinned_directory_fd)
        _validate_tmpfs_lock_directory_status(
            directory_status,
            effective_uid=effective_uid,
            display_path=shown_path,
        )
        hashed_lock_name = _tmpfs_lock_name_at(
            directory_status,
            lock_name,
            effective_uid=effective_uid,
        )
        with _tmpfs_lock_by_name(
            hashed_lock_name,
            effective_uid=effective_uid,
            timeout_seconds=timeout_seconds,
            timeout_path=shown_path,
        ):
            current_status = os.fstat(pinned_directory_fd)
            _validate_tmpfs_lock_directory_status(
                current_status,
                effective_uid=effective_uid,
                display_path=shown_path,
            )
            if not _same_inode(directory_status, current_status):
                raise ValueError(
                    f"Tmpfs lock directory fd changed during acquisition: {shown_path}"
                )
            yield
    finally:
        os.close(pinned_directory_fd)


@contextmanager
def file_lock_at(
    directory_fd: int,
    lock_name: str,
    *,
    display_path: Path | None = None,
    timeout_seconds: float = 10.0,
) -> Iterator[None]:
    """Lock one single-link regular file relative to an already-pinned directory."""

    _validate_plain_lock_name(lock_name)
    shown_path = display_path or Path(lock_name)
    deadline = time.monotonic() + timeout_seconds
    file_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NONBLOCK"):
        file_flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_name, file_flags, 0o600, dir_fd=directory_fd)
    file_status = os.fstat(descriptor)
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"Lock path must be a single-link regular file: {shown_path}")
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out acquiring lock: {shown_path}") from None
                time.sleep(0.1)

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nacquired_at={now_utc_iso()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        try:
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    if lock_path.parent.is_symlink():
        raise ValueError(f"Lock directory must not be a symlink: {lock_path.parent}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(lock_path.parent, directory_flags)
    try:
        with file_lock_at(
            directory_fd,
            lock_path.name,
            display_path=lock_path,
            timeout_seconds=timeout_seconds,
        ):
            yield
    finally:
        os.close(directory_fd)
