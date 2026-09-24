from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from orca_auto.core.paths.reserved import relative_reaches_reserved_generation
from orca_auto.core.paths.retired import path_is_retired_workflow_owned

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
    if path_is_retired_workflow_owned(raw_job_dir, resolved_root):
        raise ValueError(
            "Workflow directories are retired; submit a standalone ORCA input directory"
        )
    try:
        relative = Path(raw_job_dir).expanduser().resolve().relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return
    if relative_reaches_reserved_generation(resolved_root, relative):
        raise ValueError(
            "run-dir target is inside an ORCA execution generation; submit its public parent "
            f"job directory instead: {raw_job_dir}"
        )
