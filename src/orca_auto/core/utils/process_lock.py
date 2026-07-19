"""Shared JSON lock-file utilities with stale process recovery."""

from __future__ import annotations

import errno
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from orca_auto.core.engine_process import read_confined_text
from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.lock import tmpfs_file_lock


def parse_lock_info(lock_path: Path) -> dict[str, Any]:
    try:
        raw = read_confined_text(
            lock_path.parent,
            lock_path,
            label="process lock",
            max_links=2,
        ).strip()
    except (OSError, ValueError):
        return _empty_lock_info()
    if not raw:
        return _empty_lock_info()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_lock_info()

    if isinstance(parsed, dict):
        return {
            "pid": _positive_int(parsed.get("pid")),
            "started_at": _nonempty_string(parsed.get("started_at")),
            "process_start_ticks": _positive_int(parsed.get("process_start_ticks")),
            "boot_id": _nonempty_string(parsed.get("boot_id")),
        }

    return _empty_lock_info()


def _empty_lock_info() -> dict[str, Any]:
    return {"pid": None, "started_at": None, "process_start_ticks": None, "boot_id": None}


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _write_lock_payload(lock_path: Path, lock_payload: str) -> bool:
    # Publish the lock atomically WITH its payload already present. A plain
    # ``O_CREAT | O_EXCL`` create makes the lock file visible while still empty,
    # and a contender that reads it in that window sees empty content, which
    # ``parse_lock_info`` reports as an unreadable owner -- a spurious hard failure
    # against a valid, freshly created lock. Instead write the payload to a private
    # temp file first, then hard-link it into place: ``os.link`` raises
    # ``FileExistsError`` if the target already exists (preserving exclusive
    # creation), and the linked file is never observed empty.
    if lock_path.parent.is_symlink():
        raise ValueError(f"Process lock directory must not be a symlink: {lock_path.parent}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{lock_path.name}.",
        suffix=".tmp",
        dir=lock_path.parent,
    )
    tmp_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(lock_payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, lock_path, follow_symlinks=False)
        except FileExistsError:
            return False
        return True
    finally:
        with suppress(OSError):
            os.unlink(tmp_path)


_STALE_RECOVERY_FLOCK_TIMEOUT_SECONDS = 10.0


def _claim_stale_lock(
    lock_path: Path,
    *,
    parse_lock_info_fn: Callable[[Path], dict[str, Any]],
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
    boot_id_fn: Callable[[], str | None] = process_utils.linux_boot_id,
    logger: logging.Logger,
    stale_pid_reuse_log_template: str,
) -> bool:
    """Verify-and-remove a lock judged stale; True when it was removed.

    A bare unlink-then-recreate races: two contenders can both judge the same
    lock stale, and the slower one unlinks the winner's FRESH lock — leaving
    two owners. Recovery is therefore serialized through a sibling flock:
    while holding it, the lock content is re-verified and only a still-dead
    owner's file is unlinked. A live lock is never moved or removed, and the
    lock path is only ever missing after a verified-stale unlink — a window
    where any contender may legitimately create.
    """
    recovery_lock = lock_path.with_name(f".{lock_path.name}.recovery")
    try:
        with tmpfs_file_lock(
            recovery_lock,
            timeout_seconds=_STALE_RECOVERY_FLOCK_TIMEOUT_SECONDS,
        ):
            current = parse_lock_info_fn(lock_path)
            current_pid = current.get("pid")
            if isinstance(current_pid, int) and _lock_owner_alive(
                lock_pid=current_pid,
                lock_start_ticks=current.get("process_start_ticks"),
                lock_boot_id=current.get("boot_id"),
                is_process_alive_fn=is_process_alive_fn,
                process_start_ticks_fn=process_start_ticks_fn,
                boot_id_fn=boot_id_fn,
                logger=logger,
                stale_pid_reuse_log_template=stale_pid_reuse_log_template,
                lock_path=lock_path,
            ):
                # A fresh live lock replaced the stale one while we waited:
                # it is not ours to touch.
                return False
            with suppress(FileNotFoundError):
                lock_path.unlink()
            return True
    except TimeoutError:
        # Recovery is contended beyond reason; treat the lock as active and
        # let the caller's wait/raise policy decide.
        return False


def _lock_owner_alive(
    *,
    lock_pid: int,
    lock_start_ticks: Any,
    lock_boot_id: Any,
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
    boot_id_fn: Callable[[], str | None],
    logger: logging.Logger,
    stale_pid_reuse_log_template: str,
    lock_path: Path,
) -> bool:
    if isinstance(lock_boot_id, str) and lock_boot_id.strip():
        observed_boot_id = boot_id_fn()
        if (
            isinstance(observed_boot_id, str)
            and observed_boot_id.strip()
            and observed_boot_id.strip() != lock_boot_id.strip()
        ):
            return False
    alive = is_process_alive_fn(lock_pid)
    if not alive or not isinstance(lock_start_ticks, int):
        return alive
    observed_ticks = process_start_ticks_fn(lock_pid)
    if observed_ticks is None or observed_ticks == lock_start_ticks:
        return alive
    logger.info(
        stale_pid_reuse_log_template,
        lock_pid,
        lock_start_ticks,
        observed_ticks,
        lock_path,
    )
    return False


def _raise_lock_timeout(
    *,
    timeout_seconds: int | None,
    lock_path: Path,
    timeout_error_builder: Callable[[Path, int], RuntimeError] | None,
) -> NoReturn:
    timeout_value = int(timeout_seconds) if timeout_seconds is not None else 0
    if timeout_error_builder is not None:
        raise timeout_error_builder(lock_path, timeout_value)
    raise RuntimeError(f"Lock acquisition timed out after {timeout_value}s. Lock file: {lock_path}")


def _handle_existing_lock(
    *,
    lock_path: Path,
    lock_info: dict[str, Any],
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
    boot_id_fn: Callable[[], str | None],
    logger: logging.Logger,
    stale_pid_reuse_log_template: str,
    stale_lock_log_template: str,
    deadline: float | None,
    claim_stale_lock_fn: Callable[[], bool],
    active_lock_error_builder: Callable[[int, dict[str, Any], Path], RuntimeError] | None,
    unreadable_lock_error_builder: Callable[[Path], RuntimeError] | None,
    stale_remove_error_builder: Callable[[int, Path, OSError], RuntimeError] | None,
) -> bool:
    lock_pid = lock_info.get("pid")
    if not isinstance(lock_pid, int):
        return _handle_unreadable_lock(
            lock_path=lock_path,
            logger=logger,
            deadline=deadline,
            claim_stale_lock_fn=claim_stale_lock_fn,
            unreadable_lock_error_builder=unreadable_lock_error_builder,
        )

    alive = _lock_owner_alive(
        lock_pid=lock_pid,
        lock_start_ticks=lock_info.get("process_start_ticks"),
        lock_boot_id=lock_info.get("boot_id"),
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
        boot_id_fn=boot_id_fn,
        logger=logger,
        stale_pid_reuse_log_template=stale_pid_reuse_log_template,
        lock_path=lock_path,
    )
    if not alive:
        logger.info(stale_lock_log_template, lock_pid, lock_path)
        try:
            claimed = claim_stale_lock_fn()
        except OSError as exc:
            if stale_remove_error_builder is not None:
                raise stale_remove_error_builder(lock_pid, lock_path, exc) from exc
            raise
        if claimed:
            return True
        # The file we claimed belonged to a live owner (a fresh lock raced our
        # staleness check): treat it as an actively held lock.
        if deadline is None:
            if active_lock_error_builder is not None:
                raise active_lock_error_builder(lock_pid, lock_info, lock_path)
            raise RuntimeError(f"Lock is held by active pid={lock_pid}. Lock file: {lock_path}")
        return False

    if deadline is None:
        if active_lock_error_builder is not None:
            raise active_lock_error_builder(lock_pid, lock_info, lock_path)
        raise RuntimeError(f"Lock is held by active pid={lock_pid}. Lock file: {lock_path}")
    return False


def _handle_unreadable_lock(
    *,
    lock_path: Path,
    logger: logging.Logger,
    deadline: float | None,
    claim_stale_lock_fn: Callable[[], bool],
    unreadable_lock_error_builder: Callable[[Path], RuntimeError] | None,
) -> bool:
    if deadline is None:
        if unreadable_lock_error_builder is not None:
            raise unreadable_lock_error_builder(lock_path)
        raise RuntimeError(f"Lock file owner is unreadable. Lock file: {lock_path}")
    logger.warning("Lock file has unreadable owner, treating as stale: %s", lock_path)
    try:
        return claim_stale_lock_fn()
    except OSError:
        return False


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # Only ProcessLookupError/ESRCH proves absence. Unknown probe errors
        # are fail-closed so callers never reap or replace a possibly live
        # owner without a verified identity.
        return exc.errno != errno.ESRCH
    return True


def process_start_ticks(pid: int) -> int | None:
    return process_utils.process_start_ticks(pid, proc_root=Path("/proc"))


def current_process_start_ticks() -> int | None:
    return process_start_ticks(os.getpid())


def current_boot_id() -> str | None:
    return process_utils.linux_boot_id(proc_root=Path("/proc"))


@dataclass(frozen=True)
class FileLockOptions:
    lock_path: Path
    lock_payload_obj: dict[str, Any]
    timeout_seconds: int | None = None
    poll_interval_seconds: float = 0.5


@dataclass(frozen=True)
class FileLockDeps:
    parse_lock_info: Callable[[Path], dict[str, Any]]
    is_process_alive: Callable[[int], bool]
    process_start_ticks: Callable[[int], int | None]
    boot_id: Callable[[], str | None]
    logger: logging.Logger


@dataclass(frozen=True)
class FileLockMessages:
    acquired_log_template: str
    released_log_template: str
    stale_pid_reuse_log_template: str
    stale_lock_log_template: str


@dataclass(frozen=True)
class FileLockErrorBuilders:
    active_lock_error_builder: Callable[[int, dict[str, Any], Path], RuntimeError] | None = None
    unreadable_lock_error_builder: Callable[[Path], RuntimeError] | None = None
    timeout_error_builder: Callable[[Path, int], RuntimeError] | None = None
    stale_remove_error_builder: Callable[[int, Path, OSError], RuntimeError] | None = None


@contextmanager
def acquire_file_lock_from_options(
    *,
    options: FileLockOptions,
    deps: FileLockDeps,
    messages: FileLockMessages,
    errors: FileLockErrorBuilders | None = None,
) -> Iterator[None]:
    """Acquire an exclusive lock file using grouped options/dependencies."""

    active_errors = errors or FileLockErrorBuilders()
    lock_path = options.lock_path
    timeout_seconds = options.timeout_seconds
    lock_payload = json.dumps(options.lock_payload_obj, ensure_ascii=True)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

    def claim_stale_lock() -> bool:
        return _claim_stale_lock(
            lock_path,
            parse_lock_info_fn=deps.parse_lock_info,
            is_process_alive_fn=deps.is_process_alive,
            process_start_ticks_fn=deps.process_start_ticks,
            boot_id_fn=deps.boot_id,
            logger=deps.logger,
            stale_pid_reuse_log_template=messages.stale_pid_reuse_log_template,
        )

    while True:
        if _write_lock_payload(lock_path, lock_payload):
            deps.logger.debug(messages.acquired_log_template, lock_path)
            break

        lock_info = deps.parse_lock_info(lock_path)
        if _handle_existing_lock(
            lock_path=lock_path,
            lock_info=lock_info,
            is_process_alive_fn=deps.is_process_alive,
            process_start_ticks_fn=deps.process_start_ticks,
            boot_id_fn=deps.boot_id,
            logger=deps.logger,
            stale_pid_reuse_log_template=messages.stale_pid_reuse_log_template,
            stale_lock_log_template=messages.stale_lock_log_template,
            deadline=deadline,
            claim_stale_lock_fn=claim_stale_lock,
            active_lock_error_builder=active_errors.active_lock_error_builder,
            unreadable_lock_error_builder=active_errors.unreadable_lock_error_builder,
            stale_remove_error_builder=active_errors.stale_remove_error_builder,
        ):
            continue

        if deadline is None:
            _raise_lock_timeout(
                timeout_seconds=timeout_seconds,
                lock_path=lock_path,
                timeout_error_builder=active_errors.timeout_error_builder,
            )
        if time.monotonic() >= deadline:
            _raise_lock_timeout(
                timeout_seconds=timeout_seconds,
                lock_path=lock_path,
                timeout_error_builder=active_errors.timeout_error_builder,
            )
        time.sleep(options.poll_interval_seconds)

    try:
        yield
    finally:
        _release_owned_lock(
            lock_path,
            payload_obj=options.lock_payload_obj,
            parse_lock_info_fn=deps.parse_lock_info,
            logger=deps.logger,
            released_log_template=messages.released_log_template,
        )


def _release_owned_lock(
    lock_path: Path,
    *,
    payload_obj: dict[str, Any],
    parse_lock_info_fn: Callable[[Path], dict[str, Any]],
    logger: logging.Logger,
    released_log_template: str,
) -> None:
    """Unlink the lock only while it still carries our payload.

    If our lock was stolen (crash-recovery race) and someone else re-created
    the file, an unconditional unlink would delete the new owner's lock too.
    """
    with suppress(OSError):
        our_pid = payload_obj.get("pid")
        if isinstance(our_pid, int):
            current = parse_lock_info_fn(lock_path)
            if current.get("pid") not in (None, our_pid):
                logger.warning(
                    "Not releasing lock owned by pid=%s (expected our pid=%d): %s",
                    current.get("pid"),
                    our_pid,
                    lock_path,
                )
                return
        lock_path.unlink()
        logger.debug(released_log_template, lock_path)


@contextmanager
def acquire_file_lock(
    *,
    lock_path: Path,
    lock_payload_obj: dict[str, Any],
    parse_lock_info_fn: Callable[[Path], dict[str, Any]],
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
    boot_id_fn: Callable[[], str | None] = current_boot_id,
    logger: logging.Logger,
    acquired_log_template: str,
    released_log_template: str,
    stale_pid_reuse_log_template: str,
    stale_lock_log_template: str,
    timeout_seconds: int | None = None,
    poll_interval_seconds: float = 0.5,
    active_lock_error_builder: Callable[[int, dict[str, Any], Path], RuntimeError] | None = None,
    unreadable_lock_error_builder: Callable[[Path], RuntimeError] | None = None,
    timeout_error_builder: Callable[[Path, int], RuntimeError] | None = None,
    stale_remove_error_builder: Callable[[int, Path, OSError], RuntimeError] | None = None,
) -> Iterator[None]:
    """Acquire an exclusive lock file with stale-lock recovery.

    If ``timeout_seconds`` is ``None``, active/unreadable lock owners raise
    immediately via the corresponding builders.
    If ``timeout_seconds`` is set, lock acquisition retries until timeout.
    """
    with acquire_file_lock_from_options(
        options=FileLockOptions(
            lock_path=lock_path,
            lock_payload_obj=lock_payload_obj,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        ),
        deps=FileLockDeps(
            parse_lock_info=parse_lock_info_fn,
            is_process_alive=is_process_alive_fn,
            process_start_ticks=process_start_ticks_fn,
            boot_id=boot_id_fn,
            logger=logger,
        ),
        messages=FileLockMessages(
            acquired_log_template=acquired_log_template,
            released_log_template=released_log_template,
            stale_pid_reuse_log_template=stale_pid_reuse_log_template,
            stale_lock_log_template=stale_lock_log_template,
        ),
        errors=FileLockErrorBuilders(
            active_lock_error_builder=active_lock_error_builder,
            unreadable_lock_error_builder=unreadable_lock_error_builder,
            timeout_error_builder=timeout_error_builder,
            stale_remove_error_builder=stale_remove_error_builder,
        ),
    ):
        yield
