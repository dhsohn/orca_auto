"""Shared JSON lock-file utilities with stale process recovery."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from orca_auto.core.utils import process as process_utils


def parse_lock_info(lock_path: Path) -> dict[str, Any]:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
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
        }

    return _empty_lock_info()


def _empty_lock_info() -> dict[str, Any]:
    return {"pid": None, "started_at": None, "process_start_ticks": None}


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
    tmp_path = lock_path.with_name(f".{lock_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(lock_payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_path, lock_path)
        except FileExistsError:
            return False
        return True
    finally:
        with suppress(OSError):
            os.unlink(tmp_path)


def _unlink_lock(lock_path: Path) -> bool:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return True
    return True


def _lock_owner_alive(
    *,
    lock_pid: int,
    lock_start_ticks: Any,
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
    logger: logging.Logger,
    stale_pid_reuse_log_template: str,
    lock_path: Path,
) -> bool:
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
    logger: logging.Logger,
    stale_pid_reuse_log_template: str,
    stale_lock_log_template: str,
    deadline: float | None,
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
            unreadable_lock_error_builder=unreadable_lock_error_builder,
        )

    alive = _lock_owner_alive(
        lock_pid=lock_pid,
        lock_start_ticks=lock_info.get("process_start_ticks"),
        is_process_alive_fn=is_process_alive_fn,
        process_start_ticks_fn=process_start_ticks_fn,
        logger=logger,
        stale_pid_reuse_log_template=stale_pid_reuse_log_template,
        lock_path=lock_path,
    )
    if not alive:
        logger.info(stale_lock_log_template, lock_pid, lock_path)
        try:
            _unlink_lock(lock_path)
        except OSError as exc:
            if stale_remove_error_builder is not None:
                raise stale_remove_error_builder(lock_pid, lock_path, exc) from exc
            raise
        return True

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
    unreadable_lock_error_builder: Callable[[Path], RuntimeError] | None,
) -> bool:
    if deadline is None:
        if unreadable_lock_error_builder is not None:
            raise unreadable_lock_error_builder(lock_path)
        raise RuntimeError(f"Lock file owner is unreadable. Lock file: {lock_path}")
    logger.warning("Lock file has unreadable owner, treating as stale: %s", lock_path)
    try:
        _unlink_lock(lock_path)
    except OSError:
        return False
    return True


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_ticks(pid: int) -> int | None:
    return process_utils.process_start_ticks(pid, proc_root=Path("/proc"))


def current_process_start_ticks() -> int | None:
    return process_start_ticks(os.getpid())


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
            logger=deps.logger,
            stale_pid_reuse_log_template=messages.stale_pid_reuse_log_template,
            stale_lock_log_template=messages.stale_lock_log_template,
            deadline=deadline,
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
        with suppress(OSError):
            lock_path.unlink()
            deps.logger.debug(messages.released_log_template, lock_path)


@contextmanager
def acquire_file_lock(
    *,
    lock_path: Path,
    lock_payload_obj: dict[str, Any],
    parse_lock_info_fn: Callable[[Path], dict[str, Any]],
    is_process_alive_fn: Callable[[int], bool],
    process_start_ticks_fn: Callable[[int], int | None],
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
