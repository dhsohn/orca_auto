from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ChildWorkerShutdownController:
    requested: bool = False

    def request(self) -> None:
        self.requested = True

    def is_requested(self) -> bool:
        return self.requested


@dataclass(frozen=True)
class ChildWorkerEntrypointJob:
    cfg: Any
    queue_root: Path
    entry: Any
    _admission_root_fn: Callable[[Any], str | Path]

    def admission_root(self) -> str | Path:
        return self._admission_root_fn(self.cfg)


def find_queue_entry_by_id(
    queue_root: str | Path,
    queue_id: str,
    *,
    list_queue_fn: Callable[[str | Path], Iterable[Any]],
) -> Any | None:
    for entry in list_queue_fn(queue_root):
        if entry.queue_id == queue_id:
            return entry
    return None


def build_queue_entry_lookup(
    *,
    list_queue_fn: Callable[[str | Path], Iterable[Any]],
    coerce_root_to_path: bool = False,
) -> Callable[[str | Path, str], Any | None]:
    def queue_entry_by_id(queue_root: str | Path, queue_id: str) -> Any | None:
        resolved_root: str | Path = Path(queue_root) if coerce_root_to_path else queue_root
        return find_queue_entry_by_id(
            resolved_root,
            queue_id,
            list_queue_fn=list_queue_fn,
        )

    return queue_entry_by_id


def load_child_worker_entrypoint_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    find_queue_entry_fn: Callable[[Path, str], Any | None],
    admission_root_fn: Callable[[Any], str | Path],
    entry_ready_fn: Callable[[Any], bool] | None = None,
) -> ChildWorkerEntrypointJob | None:
    cfg = load_config_fn(config_path)
    resolved_queue_root = Path(queue_root).expanduser().resolve()
    entry = find_queue_entry_fn(resolved_queue_root, queue_id)
    ready = entry is not None and (entry_ready_fn is None or entry_ready_fn(entry))
    if not ready:
        # The parent owns final release. Keeping the slot lets a parent that
        # already spawned this child observe its exit and finalize atomically;
        # after a parent crash, normal stale-owner reconciliation removes it.
        return None
    return ChildWorkerEntrypointJob(
        cfg=cfg,
        queue_root=resolved_queue_root,
        entry=entry,
        _admission_root_fn=admission_root_fn,
    )


def install_shutdown_request_handlers(
    controller: ChildWorkerShutdownController,
    *,
    install_signal_handlers_fn: Callable[[Callable[[], None]], Any],
) -> None:
    install_signal_handlers_fn(controller.request)


__all__ = [
    "ChildWorkerEntrypointJob",
    "ChildWorkerShutdownController",
    "build_queue_entry_lookup",
    "find_queue_entry_by_id",
    "install_shutdown_request_handlers",
    "load_child_worker_entrypoint_job",
]
