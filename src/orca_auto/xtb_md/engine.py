from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.config.engines import load_xtb_md_config
from orca_auto.core.engines import (
    build_lazy_queue_worker_runner,
    build_lazy_worker_child_runner,
    build_queue_engine_definition,
    build_queue_entry_by_id,
)
from orca_auto.core.queue import dequeue_entry_if_pending, dequeue_next, list_queue
from orca_auto.core.queue.internal_engine import own_engine_accept_entry

from .records import build_job_artifact, persist_job_artifact

_ACCEPT_XTB_MD_ENTRY = own_engine_accept_entry("xtb_md")


def _list_xtb_md_queue(root: str | Path) -> list[Any]:
    return [entry for entry in list_queue(root) if _ACCEPT_XTB_MD_ENTRY(entry)]


def _dequeue_next_xtb_md(root: Path) -> Any | None:
    return dequeue_next(root, accept_entry_fn=_ACCEPT_XTB_MD_ENTRY)


def _dequeue_xtb_md_entry_if_pending(
    root: Path,
    queue_id: str,
    *,
    expected_entry: Any | None = None,
) -> Any | None:
    return dequeue_entry_if_pending(
        root,
        queue_id,
        accept_entry_fn=_ACCEPT_XTB_MD_ENTRY,
        expected_entry=expected_entry,
    )


def _persist_pending_cancelled_job(entry: Any, *, config_path: str) -> None:
    cfg = load_xtb_md_config(config_path)
    payload = build_job_artifact(
        entry,
        status="cancelled",
        reason="cancel_requested",
        exit_code=None,
    )
    persist_job_artifact(cfg, entry, payload)


ENGINE_DEFINITION = build_queue_engine_definition(
    engine="xtb_md",
    load_config=load_xtb_md_config,
    run_worker_child_job=build_lazy_worker_child_runner(
        "orca_auto.xtb_md.execution",
        "run_worker_job",
    ),
    queue_worker_runner=build_lazy_queue_worker_runner("orca_auto.xtb_md.queue_runtime"),
    list_queue=_list_xtb_md_queue,
    dequeue_next=_dequeue_next_xtb_md,
    dequeue_entry_if_pending=_dequeue_xtb_md_entry_if_pending,
    queue_entry_by_id=build_queue_entry_by_id(_list_xtb_md_queue),
    worker_pid_file_name="xtb_md_queue_worker.pid",
    before_pending_cancel=_persist_pending_cancelled_job,
)
build_worker_child_command = ENGINE_DEFINITION.build_worker_child_command


__all__ = ["ENGINE_DEFINITION", "build_worker_child_command"]
