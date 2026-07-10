from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.child import execution as _child_execution


@dataclass(frozen=True)
class ChildWorkerEntrypointJob:
    cfg: Any
    queue_root: Path
    entry: Any
    _admission_root_fn: Callable[[Any], str | Path]

    def admission_root(self) -> str | Path:
        return self._admission_root_fn(self.cfg)


def queue_entry_by_id(
    queue_root: str | Path,
    queue_id: str,
    *,
    list_queue_fn: Callable[[str | Path], Iterable[Any]],
) -> Any | None:
    return _child_execution.find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue_fn,
    )


def load_child_worker_entrypoint_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    admission_root_fn: Callable[[Any], str | Path],
    release_slot_fn: Callable[[str | Path, str], Any],
    admission_token: str | None = None,
    find_queue_entry_fn: Callable[[Path, str], Any | None] | None = None,
    list_queue_fn: Callable[[str | Path], Iterable[Any]] | None = None,
    entry_ready_fn: Callable[[Any], bool] | None = None,
) -> ChildWorkerEntrypointJob | None:
    if find_queue_entry_fn is None:
        if list_queue_fn is None:
            raise ValueError("find_queue_entry_fn or list_queue_fn is required")

        def find_queue_entry_fn(root: Path, target_queue_id: str) -> Any | None:
            return queue_entry_by_id(
                root,
                target_queue_id,
                list_queue_fn=list_queue_fn,
            )

    job = _child_execution.load_child_queue_job(
        config_path=config_path,
        queue_root=queue_root,
        queue_id=queue_id,
        load_config_fn=load_config_fn,
        find_queue_entry_fn=find_queue_entry_fn,
        entry_ready_fn=entry_ready_fn,
        admission_token=admission_token,
        admission_root_fn=admission_root_fn,
        release_slot_fn=release_slot_fn,
    )
    if job is None:
        return None
    return ChildWorkerEntrypointJob(
        cfg=job.cfg,
        queue_root=job.queue_root,
        entry=job.entry,
        _admission_root_fn=admission_root_fn,
    )


def activate_child_worker_admission(
    job: ChildWorkerEntrypointJob,
    admission_token: str | None,
    *,
    work_dir: str | Path,
    queue_id: str,
    source: str | None,
    activate_reserved_slot_fn: Callable[..., Any],
) -> bool:
    return _child_execution.activate_child_admission_token(
        job.admission_root(),
        admission_token,
        work_dir=work_dir,
        queue_id=queue_id,
        source=source,
        activate_reserved_slot_fn=activate_reserved_slot_fn,
    )


def release_child_worker_admission(
    job: ChildWorkerEntrypointJob,
    admission_token: str | None,
    *,
    release_slot_fn: Callable[[str | Path, str], Any],
) -> None:
    if not admission_token:
        return
    _child_execution.release_child_admission_token(
        job.admission_root(),
        admission_token,
        release_slot_fn=release_slot_fn,
    )


@contextmanager
def child_worker_admission_scope(
    job: ChildWorkerEntrypointJob,
    admission_token: str | None,
    *,
    release_slot_fn: Callable[[str | Path, str], Any],
) -> Iterator[None]:
    try:
        yield
    except BaseException:
        # An asynchronous BaseException can land after Popen but before the
        # active identity is published. Keep pending/active ownership for the
        # parent recovery path instead of releasing the only capacity fence.
        raise
    else:
        return


__all__ = [
    "ChildWorkerEntrypointJob",
    "activate_child_worker_admission",
    "child_worker_admission_scope",
    "load_child_worker_entrypoint_job",
    "queue_entry_by_id",
    "release_child_worker_admission",
]
