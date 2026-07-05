from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from orca_auto.core.admission import reserve_slot
from orca_auto.core.config.schema import resolved_admission_limit

from ..child.execution import find_queue_entry_by_id
from ..dependencies import ChildQueueWorkerDeps
from .models import ReservedQueueEntry

T = TypeVar("T")

_INTERNAL_ENGINE_WORKER_SOURCES = {
    "crest": "orca_auto.flow.engines.crest.queue_worker",
    "xtb": "orca_auto.flow.engines.xtb.queue_worker",
}


def engine_queue_worker_source(engine: str) -> str:
    engine_slug = str(engine).strip().replace("-", "_")
    return _INTERNAL_ENGINE_WORKER_SOURCES.get(
        engine_slug,
        f"orca_auto.{engine_slug}.queue_worker",
    )


def resolve_admission_root(cfg: Any) -> str:
    return str(
        getattr(cfg.runtime, "resolved_admission_root", None)
        or getattr(cfg.runtime, "admission_root", "")
        or cfg.runtime.allowed_root
    )


def resolve_admission_limit(cfg: Any) -> int:
    raw = getattr(cfg.runtime, "resolved_admission_limit", None)
    if raw not in (None, "", 0):
        return resolved_admission_limit(raw, 1)
    raw = getattr(cfg.runtime, "admission_limit", None)
    fallback = getattr(cfg.runtime, "max_concurrent", 1)
    if raw in (None, "", 0):
        raw = fallback
    return resolved_admission_limit(raw, fallback)


def reserve_queue_worker_slot(
    cfg: Any,
    *,
    source: str,
    app_name: str,
    reserve_slot_fn: Callable[..., str | None] = reserve_slot,
) -> str | None:
    return reserve_slot_fn(
        resolve_admission_root(cfg),
        resolve_admission_limit(cfg),
        source=source,
        app_name=app_name,
    )


def reserve_engine_queue_worker_slot(
    cfg: Any,
    *,
    engine: str,
    reserve_slot_fn: Callable[..., str | None] = reserve_slot,
) -> str | None:
    engine_slug = str(engine).strip().replace("-", "_")
    return reserve_queue_worker_slot(
        cfg,
        source=engine_queue_worker_source(engine_slug),
        app_name=f"orca_auto_{engine_slug}",
        reserve_slot_fn=reserve_slot_fn,
    )


def dequeue_next_across_roots(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    dequeue_next_fn: Callable[[Path], T | None],
    dequeue_entry_fn: Callable[[Path, str], T | None] | None = None,
    accept_entry_fn: Callable[[T], bool] | None = None,
) -> tuple[Path, T] | None:
    if len(roots) == 1 and accept_entry_fn is None:
        entry = dequeue_next_fn(roots[0])
        if entry is None:
            return None
        return roots[0], entry

    selected_root: Path | None = None
    selected_queue_id = ""
    selected_key: tuple[int, str, int, int] | None = None

    for root_index, root in enumerate(roots):
        for entry_index, entry in enumerate(list_queue_fn(root)):
            status_value = getattr(getattr(entry, "status", None), "value", None)
            status = str(status_value).strip().lower()
            if status != "pending" or getattr(entry, "cancel_requested", False):
                continue
            if accept_entry_fn is not None and not accept_entry_fn(entry):
                if dequeue_entry_fn is None:
                    break
                continue
            key = (
                int(getattr(entry, "priority", 10) or 10),
                str(getattr(entry, "enqueued_at", "")),
                root_index,
                entry_index,
            )
            if selected_key is None or key < selected_key:
                selected_key = key
                selected_root = root
                selected_queue_id = str(getattr(entry, "queue_id", "")).strip()
            if dequeue_entry_fn is None:
                break

    if selected_root is None:
        return None

    if dequeue_entry_fn is not None and selected_queue_id:
        entry = dequeue_entry_fn(selected_root, selected_queue_id)
    else:
        entry = dequeue_next_fn(selected_root)
    if entry is None:
        return None
    return selected_root, entry


def queue_entry_by_id(
    queue_root: str | Path,
    queue_id: str,
    *,
    list_queue_fn: Callable[[str | Path], Any],
) -> Any | None:
    return find_queue_entry_by_id(
        queue_root,
        queue_id,
        list_queue_fn=list_queue_fn,
    )


def reserve_dequeued_entry(
    cfg: Any,
    *,
    admission_root: str | Path,
    reserve_slot_fn: Callable[[Any], str | None],
    dequeue_next_fn: Callable[[Any], tuple[Path, T] | None],
    release_slot_fn: Callable[[str | Path, str], object],
) -> tuple[str, ReservedQueueEntry[T] | None]:
    admission_token = reserve_slot_fn(cfg)
    if admission_token is None:
        return "blocked", None

    try:
        dequeued = dequeue_next_fn(cfg)
    except Exception:
        release_slot_fn(admission_root, admission_token)
        raise
    if dequeued is None:
        release_slot_fn(admission_root, admission_token)
        return "idle", None

    queue_root, entry = dequeued
    return (
        "processed",
        ReservedQueueEntry(
            queue_root=queue_root,
            entry=entry,
            admission_token=admission_token,
        ),
    )


def make_child_queue_worker_deps(
    *,
    poll_interval_seconds: int,
    time_module: Any,
    release_slot_fn: Callable[[str | Path, str], object],
    admission_root_fn: Callable[[Any], str],
    dequeue_next_entry_fn: Callable[[Any], tuple[Path, Any] | None],
    start_background_job_process_fn: Callable[..., Any],
    try_reserve_admission_slot_fn: Callable[[Any], str | None],
    reserve_dequeued_entry_fn: Callable[..., tuple[str, Any | None]] = reserve_dequeued_entry,
) -> ChildQueueWorkerDeps:
    return ChildQueueWorkerDeps(
        poll_interval_seconds=poll_interval_seconds,
        time=time_module,
        release_slot=release_slot_fn,
        reserve_dequeued_entry=reserve_dequeued_entry_fn,
        admission_root=admission_root_fn,
        dequeue_next_entry=dequeue_next_entry_fn,
        start_background_job_process=start_background_job_process_fn,
        try_reserve_admission_slot=try_reserve_admission_slot_fn,
    )


def config_path_for_worker(args: Any, *, default_config_path_fn: Callable[[], str]) -> str:
    configured = str(getattr(args, "config", "") or "").strip()
    return configured or default_config_path_fn()


__all__ = [
    "config_path_for_worker",
    "dequeue_next_across_roots",
    "make_child_queue_worker_deps",
    "queue_entry_by_id",
    "reserve_dequeued_entry",
    "reserve_engine_queue_worker_slot",
    "reserve_queue_worker_slot",
    "resolve_admission_limit",
    "resolve_admission_root",
]
