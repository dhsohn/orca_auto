from __future__ import annotations

import logging

from ..child.process import (
    reconcile_orphaned_child_queue_entries as reconcile_orphaned_child_queue_entries,
)
from ..child.process import (
    shutdown_child_process_with_grace as shutdown_child_process_with_grace,
)
from ..child.process import (
    status_matches as status_matches,
)
from ..types import QueueStatus as QueueStatus
from .hooks import (
    ChildExitPolicy,
    EngineQueueProcessLifecycleHooks,
    EngineQueueProcessReconcileHooks,
    EngineQueueProcessShutdownHooks,
    EngineQueueTerminalSideEffectHooks,
    OrphanedRunningPolicy,
)
from .reconcile import (
    live_worker_pid_slots,
    reconcile_orphaned_process_entries,
    reconcile_orphaned_running,
    reconcile_orphaned_running_with_policy,
)
from .shutdown import (
    cancel_running_process_job,
    finalize_child_exit_with_policy,
    finalize_child_worker_exit,
    request_pending_cancellations,
    shutdown_running_job,
    shutdown_running_process_job,
)
from .terminal import (
    TerminalProcessQueueMarkResult,
    attach_started_process_metadata,
    entry_status_is,
    entry_status_is_running,
    finalize_process_finished_job,
    job_queue_root,
    mark_terminal_process_queue_entry,
    mark_terminal_process_queue_entry_with_result,
    record_terminal_process_side_effects,
    resolved_job_queue_root,
    run_terminal_process_side_effects,
    sync_terminal_running_entries,
)

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ChildExitPolicy",
    "EngineQueueProcessLifecycleHooks",
    "EngineQueueProcessReconcileHooks",
    "EngineQueueProcessShutdownHooks",
    "EngineQueueTerminalSideEffectHooks",
    "OrphanedRunningPolicy",
    "TerminalProcessQueueMarkResult",
    "attach_started_process_metadata",
    "cancel_running_process_job",
    "entry_status_is",
    "entry_status_is_running",
    "finalize_child_exit_with_policy",
    "finalize_child_worker_exit",
    "finalize_process_finished_job",
    "job_queue_root",
    "live_worker_pid_slots",
    "mark_terminal_process_queue_entry",
    "mark_terminal_process_queue_entry_with_result",
    "reconcile_orphaned_process_entries",
    "reconcile_orphaned_running",
    "reconcile_orphaned_running_with_policy",
    "record_terminal_process_side_effects",
    "request_pending_cancellations",
    "resolved_job_queue_root",
    "run_terminal_process_side_effects",
    "shutdown_running_job",
    "shutdown_running_process_job",
    "sync_terminal_running_entries",
]
