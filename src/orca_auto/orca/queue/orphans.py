"""Recovery for ORCA queue entries left running after worker loss."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue import store as _queue_store
from orca_auto.core.queue.types import QueueEntry
from orca_auto.core.utils.persistence import now_utc_iso
from orca_auto.core.utils.process_tracking import active_run_lock_pid, read_pid_file

from ..state import load_state, report_json_path
from . import reconciliation as _queue_reconciliation
from .entries import (
    WORKER_PID_FILE_NAME,
    queue_entry_id,
    queue_entry_reaction_dir,
    queue_entry_status,
)
from .entries import (
    load_entries as _load_entries,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return now_utc_iso()


def active_lock_pid(reaction_dir: Path) -> int | None:
    return active_run_lock_pid(
        reaction_dir,
        on_pid_reuse=lambda pid, expected_ticks, observed_ticks: logger.info(
            "Ignoring stale run.lock due to PID reuse: reaction_dir=%s pid=%d expected=%d observed=%s",
            reaction_dir,
            pid,
            expected_ticks,
            observed_ticks,
        ),
    )


def read_worker_pid(allowed_root: Path) -> int | None:
    return read_pid_file(allowed_root / WORKER_PID_FILE_NAME)


def load_report_payload(reaction_dir: Path) -> dict | None:
    return _queue_reconciliation.load_report_payload(
        reaction_dir,
        report_json_path_fn=report_json_path,
        logger=logger,
    )


def terminal_report_data(
    reaction_dir: Path,
) -> tuple[str, str | None, str | None, str | None] | None:
    return _queue_reconciliation.terminal_report_data(
        reaction_dir,
        load_report_payload_fn=load_report_payload,
    )


def apply_terminal_reconciliation(
    entry: QueueEntry,
    *,
    status: str,
    run_id: str | None,
    finished_at: str | None,
    error: str | None = None,
) -> QueueEntry:
    return _queue_reconciliation.apply_terminal_reconciliation(
        entry,
        status=status,
        run_id=run_id,
        finished_at=finished_at,
        error=error,
        now_iso_fn=_now_iso,
    )


@dataclass(frozen=True)
class QueueReconciliationDeps:
    load_state: Any
    queue_entry_id: Any
    queue_entry_reaction_dir: Any
    queue_entry_status: Any
    active_lock_pid: Any
    queue_lock: Any
    apply_terminal_reconciliation: Any
    load_entries: Any
    read_worker_pid: Any
    save_entries: Any
    terminal_report_data: Any


def queue_reconciliation_deps() -> QueueReconciliationDeps:
    return QueueReconciliationDeps(
        load_state=load_state,
        queue_entry_id=queue_entry_id,
        queue_entry_reaction_dir=queue_entry_reaction_dir,
        queue_entry_status=queue_entry_status,
        active_lock_pid=active_lock_pid,
        queue_lock=_queue_store.queue_lock,
        apply_terminal_reconciliation=apply_terminal_reconciliation,
        load_entries=_load_entries,
        read_worker_pid=read_worker_pid,
        save_entries=_queue_store.save_entries,
        terminal_report_data=terminal_report_data,
    )


def reconcile_orphaned_running_entries(
    allowed_root: Path,
    *,
    ignore_worker_pid: bool = False,
    protected_queue_keys: set[tuple[str, str]] | None = None,
    protected_queue_ids: set[str] | None = None,
) -> int:
    return _queue_reconciliation.reconcile_orphaned_running_entries(
        allowed_root,
        ignore_worker_pid=ignore_worker_pid,
        protected_queue_keys=protected_queue_keys,
        protected_queue_ids=protected_queue_ids,
        deps=queue_reconciliation_deps(),
        logger=logger,
    )
