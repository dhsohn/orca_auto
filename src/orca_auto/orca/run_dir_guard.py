from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from orca_auto.core.paths.reserved import relative_reaches_reserved_generation

RunDirPublicationGuard = Callable[[str], None]
_ACTIVE_RUN_DIR_PUBLICATION_GUARD: ContextVar[RunDirPublicationGuard | None] = ContextVar(
    "orca_auto_run_dir_publication_guard",
    default=None,
)
_ACTIVE_RUN_DIR_PINNED_TARGET: ContextVar[Path | None] = ContextVar(
    "orca_auto_run_dir_pinned_target",
    default=None,
)


@contextmanager
def use_run_dir_publication_guard(
    guard: RunDirPublicationGuard,
    *,
    pinned_target: str | Path | None = None,
) -> Iterator[None]:
    guard_token = _ACTIVE_RUN_DIR_PUBLICATION_GUARD.set(guard)
    target_token = _ACTIVE_RUN_DIR_PINNED_TARGET.set(
        Path(pinned_target) if pinned_target is not None else None
    )
    try:
        yield
    finally:
        _ACTIVE_RUN_DIR_PINNED_TARGET.reset(target_token)
        _ACTIVE_RUN_DIR_PUBLICATION_GUARD.reset(guard_token)


def assert_run_dir_publication_allowed(stage: str) -> None:
    guard = _ACTIVE_RUN_DIR_PUBLICATION_GUARD.get()
    if guard is not None:
        guard(stage)


def active_run_dir_pinned_target() -> Path | None:
    """Return the fd-backed public target while synchronous submission is active."""

    return _ACTIVE_RUN_DIR_PINNED_TARGET.get()


def validate_production_run_dir_target(
    raw_job_dir: str | Path,
    runs_root: str | Path,
) -> None:
    """Reject public submission of a nested ORCA execution generation."""

    resolved_root = Path(runs_root).expanduser().resolve()
    try:
        relative = Path(raw_job_dir).expanduser().resolve().relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return
    if relative_reaches_reserved_generation(resolved_root, relative):
        raise ValueError(
            "run-dir target is inside an ORCA execution generation; submit its public parent "
            f"job directory instead: {raw_job_dir}"
        )


class _RunDirTargetChangedError(ValueError):
    pass


@dataclass(frozen=True)
class _RunDirPublicationContract:
    pinned_target: Path
    namespace_target: Path
    runs_root: str
    expected_identity: tuple[int, int]

    def __call__(self, stage: str) -> None:
        try:
            path_stat = self.pinned_target.stat()
        except OSError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir target became unavailable before {stage}"
            ) from exc
        if (path_stat.st_dev, path_stat.st_ino) != self.expected_identity:
            raise _RunDirTargetChangedError(f"run-dir target identity changed before {stage}")
        try:
            namespace_stat = self.namespace_target.stat()
        except OSError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir namespace target became unavailable before {stage}"
            ) from exc
        if (namespace_stat.st_dev, namespace_stat.st_ino) != self.expected_identity:
            raise _RunDirTargetChangedError(
                f"run-dir namespace target identity changed before {stage}"
            )
        if not self.runs_root:
            return
        try:
            validate_production_run_dir_target(self.pinned_target, self.runs_root)
        except ValueError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir publication guard rejected the target before {stage}: {exc}"
            ) from exc


@contextmanager
def _pinned_run_dir_target(raw_target: str | Path) -> Iterator[Path]:
    """Yield one inode for classification, policy checks, and dispatch."""

    target = Path(raw_target).expanduser()
    flags = os.O_RDONLY | os.O_DIRECTORY
    try:
        directory_fd = os.open(target, flags)
    except FileNotFoundError as exc:
        raise _RunDirTargetChangedError(
            f"run-dir target not found: {target.resolve(strict=False)}"
        ) from exc
    except NotADirectoryError as exc:
        raise _RunDirTargetChangedError(
            f"run-dir target is not a directory: {target.resolve(strict=False)}"
        ) from exc
    except OSError as exc:
        raise _RunDirTargetChangedError(
            f"run-dir target could not be opened safely: {target}"
        ) from exc

    try:
        try:
            opened_stat = os.fstat(directory_fd)
            if not stat.S_ISDIR(opened_stat.st_mode):
                raise OSError("opened run-dir target is not a directory")
            pinned_target = Path("/proc/self/fd") / str(directory_fd)
            pinned_stat = pinned_target.stat()
            if (pinned_stat.st_dev, pinned_stat.st_ino) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ):
                raise OSError("fd path does not identify the opened run-dir target")
        except OSError as exc:
            raise _RunDirTargetChangedError(
                f"run-dir target could not be pinned safely: {target}"
            ) from exc
        yield pinned_target
    finally:
        os.close(directory_fd)
