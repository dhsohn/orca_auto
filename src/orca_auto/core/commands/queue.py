from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def display_status(entry: Any) -> str:
    status_value = getattr(getattr(entry, "status", None), "value", None)
    normalized = str(status_value).strip() or "unknown"
    if getattr(entry, "cancel_requested", False) and normalized == "running":
        return "cancel_requested"
    return normalized


@dataclass(frozen=True)
class QueueRuntime:
    load_config_fn: Callable[[Any], Any]
    runtime_roots_for_cfg_fn: Callable[[Any], tuple[Path, ...]]
    list_queue_fn: Callable[[Path], list[Any]]
    dequeue_next_fn: Callable[[Path], Any | None]
    dequeue_next_across_roots_fn: Callable[..., tuple[Path, Any] | None]
    dequeue_entry_if_pending_fn: Callable[[Path, str], Any | None] | None = None
    accept_entry_fn: Callable[[Any], bool] | None = None

    def queue_roots(self, cfg: Any) -> tuple[Path, ...]:
        return queue_roots(
            cfg,
            runtime_roots_for_cfg_fn=self.runtime_roots_for_cfg_fn,
        )

    def queue_entries_with_roots(self, cfg: Any) -> list[tuple[Path, Any]]:
        return queue_entries_with_roots(
            cfg,
            queue_roots_fn=self.queue_roots,
            list_queue_fn=self.list_queue_fn,
        )

    def dequeue_next_entry(self, cfg: Any) -> tuple[Path, Any] | None:
        return dequeue_next_entry(
            cfg,
            queue_roots_fn=self.queue_roots,
            list_queue_fn=self.list_queue_fn,
            dequeue_next_fn=self.dequeue_next_fn,
            dequeue_entry_if_pending_fn=self.dequeue_entry_if_pending_fn,
            dequeue_next_across_roots_fn=self.dequeue_next_across_roots_fn,
            accept_entry_fn=self.accept_entry_fn,
        )


def queue_roots(
    cfg: Any,
    *,
    runtime_roots_for_cfg_fn: Callable[[Any], tuple[Path, ...]],
) -> tuple[Path, ...]:
    return tuple(runtime_roots_for_cfg_fn(cfg))


def existing_queue_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(root for root in roots if Path(root).expanduser().exists())


def queue_entries_with_roots(
    cfg: Any,
    *,
    queue_roots_fn: Callable[[Any], tuple[Path, ...]],
    list_queue_fn: Callable[[Path], list[Any]],
) -> list[tuple[Path, Any]]:
    rows: list[tuple[Path, Any]] = []
    for root in existing_queue_roots(queue_roots_fn(cfg)):
        for entry in list_queue_fn(root):
            rows.append((root, entry))
    return rows


def dequeue_next_entry(
    cfg: Any,
    *,
    queue_roots_fn: Callable[[Any], tuple[Path, ...]],
    list_queue_fn: Callable[[Path], list[Any]],
    dequeue_next_fn: Callable[[Path], Any | None],
    dequeue_next_across_roots_fn: Callable[..., tuple[Path, Any] | None],
    dequeue_entry_if_pending_fn: Callable[[Path, str], Any | None] | None = None,
    accept_entry_fn: Callable[[Any], bool] | None = None,
) -> tuple[Path, Any] | None:
    return dequeue_next_across_roots_fn(
        existing_queue_roots(queue_roots_fn(cfg)),
        list_queue_fn=list_queue_fn,
        dequeue_next_fn=dequeue_next_fn,
        dequeue_entry_fn=dequeue_entry_if_pending_fn,
        accept_entry_fn=accept_entry_fn,
    )


def run_queue_worker_command(
    args: Any,
    *,
    load_config_fn: Callable[[Any], Any],
    config_path_fn: Callable[[Any], str],
    worker_factory: Callable[..., Any],
    existing_pid_fn: Callable[[Any], int | None] | None = None,
    existing_pid_report_fn: Callable[[int], Any] | None = None,
    max_concurrent_fn: Callable[[Any], int] | None = None,
) -> int:
    cfg = load_config_fn(getattr(args, "config", None))

    if existing_pid_fn is not None:
        existing_pid = existing_pid_fn(cfg)
        if existing_pid is not None:
            if existing_pid_report_fn is not None:
                existing_pid_report_fn(existing_pid)
            else:
                print(
                    f"error: queue worker already running (pid={existing_pid})",
                    file=sys.stderr,
                )
            return 1

    worker_kwargs: dict[str, Any] = {}
    if max_concurrent_fn is not None:
        worker_kwargs["max_concurrent"] = max_concurrent_fn(cfg)

    worker = worker_factory(
        cfg,
        config_path_fn(args),
        **worker_kwargs,
    )
    return worker.run()


def cfg_allowed_root(cfg: Any) -> Path:
    return Path(str(cfg.runtime.allowed_root)).expanduser().resolve()


def existing_worker_pid_from_allowed_root(
    cfg: Any,
    *,
    read_worker_pid_fn: Callable[[Path], int | None],
) -> int | None:
    return read_worker_pid_fn(cfg_allowed_root(cfg))


def run_pidfile_queue_worker_command(
    args: Any,
    *,
    load_config_fn: Callable[[Any], Any],
    config_path_fn: Callable[[Any], str],
    worker_factory: Callable[..., Any],
    read_worker_pid_fn: Callable[[Path], int | None],
    existing_pid_report_fn: Callable[[int], Any] | None = None,
    max_concurrent_fn: Callable[[Any], int] | None = None,
) -> int:
    return run_queue_worker_command(
        args,
        load_config_fn=load_config_fn,
        config_path_fn=config_path_fn,
        existing_pid_fn=lambda cfg: existing_worker_pid_from_allowed_root(
            cfg,
            read_worker_pid_fn=read_worker_pid_fn,
        ),
        existing_pid_report_fn=existing_pid_report_fn,
        max_concurrent_fn=max_concurrent_fn,
        worker_factory=worker_factory,
    )
