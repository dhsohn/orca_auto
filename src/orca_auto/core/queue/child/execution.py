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
class ChildQueueJob:
    cfg: Any
    queue_root: Path
    entry: Any


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


def load_child_queue_job(
    *,
    config_path: str,
    queue_root: str | Path,
    queue_id: str,
    load_config_fn: Callable[[str], Any],
    find_queue_entry_fn: Callable[[Path, str], Any | None],
    entry_ready_fn: Callable[[Any], bool] | None = None,
    admission_token: str | None = None,
    admission_root_fn: Callable[[Any], str | Path] | None = None,
    release_slot_fn: Callable[[str | Path, str], Any] | None = None,
) -> ChildQueueJob | None:
    cfg = load_config_fn(config_path)
    resolved_queue_root = Path(queue_root).expanduser().resolve()
    entry = find_queue_entry_fn(resolved_queue_root, queue_id)
    ready = entry is not None and (entry_ready_fn is None or entry_ready_fn(entry))
    if not ready:
        if admission_token and admission_root_fn is not None and release_slot_fn is not None:
            release_child_admission_token(
                admission_root_fn(cfg),
                admission_token,
                release_slot_fn=release_slot_fn,
            )
        return None
    return ChildQueueJob(cfg=cfg, queue_root=resolved_queue_root, entry=entry)


def activate_child_admission_token(
    admission_root: str | Path,
    admission_token: str | None,
    *,
    work_dir: str | Path,
    queue_id: str,
    source: str,
    activate_reserved_slot_fn: Callable[..., Any],
) -> bool:
    if not admission_token:
        return True
    activated = activate_reserved_slot_fn(
        admission_root,
        admission_token,
        work_dir=work_dir,
        queue_id=queue_id,
        source=source,
    )
    return activated is not None


def release_child_admission_token(
    admission_root: str | Path,
    admission_token: str | None,
    *,
    release_slot_fn: Callable[[str | Path, str], Any],
) -> None:
    if admission_token:
        release_slot_fn(admission_root, admission_token)


def install_shutdown_request_handlers(
    controller: ChildWorkerShutdownController,
    *,
    install_signal_handlers_fn: Callable[[Callable[[], None]], Any],
) -> None:
    install_signal_handlers_fn(controller.request)


__all__ = [
    "ChildQueueJob",
    "ChildWorkerShutdownController",
    "activate_child_admission_token",
    "build_queue_entry_lookup",
    "find_queue_entry_by_id",
    "install_shutdown_request_handlers",
    "load_child_queue_job",
    "release_child_admission_token",
]
