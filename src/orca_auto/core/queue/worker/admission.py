from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from orca_auto.core.admission import reserve_slot
from orca_auto.core.engine_catalog import find_engine_catalog_entry

from ..child.execution import find_queue_entry_by_id
from ..dependencies import ChildQueueWorkerDeps
from ..priority import normalize_queue_priority
from ..publication import queue_entry_is_claimable
from .models import ReservedQueueEntry

T = TypeVar("T")


def engine_queue_worker_source(engine: str) -> str:
    engine_slug = str(engine).strip().replace("-", "_")
    entry = find_engine_catalog_entry(engine_slug)
    return entry.admission_source if entry is not None else f"orca_auto.{engine_slug}.queue_worker"


def resolve_admission_root(cfg: Any) -> str:
    """Adapt the runtime's admission root to the callable the workers inject.

    The resolution itself belongs to the config: two engine worker modules pass
    this function as a value, so a property alone cannot serve them.
    """
    return str(cfg.runtime.resolved_admission_root)


def reserve_engine_queue_worker_slot(
    cfg: Any,
    *,
    engine: str,
    reserve_slot_fn: Callable[..., str | None] = reserve_slot,
) -> str | None:
    engine_slug = str(engine).strip().replace("-", "_")
    entry = find_engine_catalog_entry(engine_slug)
    reservation_kwargs: dict[str, Any] = {
        "source": engine_queue_worker_source(engine_slug),
        "app_name": entry.app_id if entry is not None else f"orca_auto_{engine_slug}",
    }
    if entry is not None and entry.managed_admission:
        # Internal-engine child entrypoints publish engine identities through
        # register_running_job (ORCA once per retry attempt).
        reservation_kwargs["engine_process_state"] = "idle"
        if entry.engine_launch_gated:
            reservation_kwargs["engine_launch_gated"] = True
    return reserve_slot_fn(
        resolve_admission_root(cfg),
        cfg.runtime.resolved_admission_limit,
        **reservation_kwargs,
    )


def _select_next_claimable_entry(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    select_all_rows: bool,
    accept_entry_fn: Callable[[T], bool] | None,
) -> tuple[Path, T] | None:
    """Pick the row a dequeue would claim across *roots* without mutating anything.

    ``select_all_rows`` is true when the caller can dequeue a specific row by
    id; otherwise each root's own head-of-queue rule decides, so only the first
    eligible row per root is considered.
    """
    selected_root: Path | None = None
    selected_entry: T | None = None
    selected_key: tuple[int, str, int] | None = None

    for root_index, root in enumerate(roots):
        champion_entry: T | None = None
        champion_key: tuple[int, int] | None = None
        for entry_index, entry in enumerate(list_queue_fn(root)):
            status_value = getattr(getattr(entry, "status", None), "value", None)
            status = str(status_value).strip().lower()
            if status != "pending" or getattr(entry, "cancel_requested", False):
                continue
            if not queue_entry_is_claimable(entry):
                continue
            if accept_entry_fn is not None and not accept_entry_fn(entry):
                if not select_all_rows:
                    break
                continue
            # Within one queue file the row position is the arrival order:
            # rows are only ever appended under the queue lock. The wall-clock
            # enqueued_at is not monotonic (WSL2 skew corrections step it
            # backwards), so it must not reorder same-priority dispatch.
            key = (
                normalize_queue_priority(getattr(entry, "priority", None)),
                entry_index,
            )
            if champion_key is None or key < champion_key:
                champion_key = key
                champion_entry = entry
            if not select_all_rows:
                break
        if champion_entry is None or champion_key is None:
            continue
        # Different queue files share no arrival order, so the wall clock
        # remains the cross-root fairness comparator.
        root_key = (
            champion_key[0],
            str(getattr(champion_entry, "enqueued_at", "")),
            root_index,
        )
        if selected_key is None or root_key < selected_key:
            selected_key = root_key
            selected_root = root
            selected_entry = champion_entry

    if selected_root is None or selected_entry is None:
        return None
    return selected_root, selected_entry


def peek_next_across_roots(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    select_all_rows: bool,
    accept_entry_fn: Callable[[T], bool] | None = None,
) -> tuple[Path, T] | None:
    """Read-only preview of ``dequeue_next_across_roots`` for the same roots.

    The worker consults this before reserving an admission slot so an idle
    poll never rewrites the admission file. The preview is never stricter than
    the dequeue: a row it selects may still be lost to a concurrent claim or
    cancellation, and the dequeue then reports that. On the single-root fast
    path the root's own ``dequeue_next`` owns the eligibility rule, so the
    preview only requires what every queue store requires — a pending,
    uncancelled row — and leaves the rest to the dequeue.
    """
    if len(roots) == 1 and accept_entry_fn is None:
        for entry in list_queue_fn(roots[0]):
            status_value = getattr(getattr(entry, "status", None), "value", None)
            status = str(status_value).strip().lower()
            if status == "pending" and not getattr(entry, "cancel_requested", False):
                return roots[0], entry
        return None
    return _select_next_claimable_entry(
        roots,
        list_queue_fn=list_queue_fn,
        select_all_rows=select_all_rows,
        accept_entry_fn=accept_entry_fn,
    )


def dequeue_next_across_roots(
    roots: tuple[Path, ...],
    *,
    list_queue_fn: Callable[[Path], list[T]],
    dequeue_next_fn: Callable[[Path], T | None],
    dequeue_entry_fn: Callable[..., T | None] | None = None,
    accept_entry_fn: Callable[[T], bool] | None = None,
) -> tuple[Path, T] | None:
    if len(roots) == 1 and accept_entry_fn is None:
        entry = dequeue_next_fn(roots[0])
        if entry is None:
            return None
        return roots[0], entry

    selected = _select_next_claimable_entry(
        roots,
        list_queue_fn=list_queue_fn,
        select_all_rows=dequeue_entry_fn is not None,
        accept_entry_fn=accept_entry_fn,
    )
    if selected is None:
        return None
    selected_root, selected_entry = selected
    selected_queue_id = str(getattr(selected_entry, "queue_id", "")).strip()

    if dequeue_entry_fn is not None and selected_queue_id:
        entry = dequeue_entry_fn(
            selected_root,
            selected_queue_id,
            expected_entry=selected_entry,
        )
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
    peek_next_fn: Callable[[Any], tuple[Path, T] | None],
    reserve_slot_fn: Callable[[Any], str | None],
    dequeue_next_fn: Callable[[Any], tuple[Path, T] | None],
    release_slot_fn: Callable[[str | Path, str], object],
) -> tuple[str, ReservedQueueEntry[T] | None]:
    # Look before reserving: an admission reservation is a durable write to
    # the shared slot file, and an idle worker must not pay it (twice, with
    # the release) on every poll. The slot still comes before the dequeue so a
    # claimed row always holds capacity; a preview that loses the race simply
    # releases the slot again.
    if peek_next_fn(cfg) is None:
        return "idle", None

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
    peek_next_entry_fn: Callable[[Any], tuple[Path, Any] | None],
    dequeue_next_entry_fn: Callable[[Any], tuple[Path, Any] | None],
    start_background_job_process_fn: Callable[..., Any],
    try_reserve_admission_slot_fn: Callable[[Any], str | None],
) -> ChildQueueWorkerDeps:
    return ChildQueueWorkerDeps(
        poll_interval_seconds=poll_interval_seconds,
        time=time_module,
        release_slot=release_slot_fn,
        reserve_dequeued_entry=reserve_dequeued_entry,
        admission_root=admission_root_fn,
        peek_next_entry=peek_next_entry_fn,
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
    "peek_next_across_roots",
    "queue_entry_by_id",
    "reserve_dequeued_entry",
    "reserve_engine_queue_worker_slot",
    "resolve_admission_root",
]
