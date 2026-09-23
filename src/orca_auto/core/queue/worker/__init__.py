from __future__ import annotations

import signal as signal

from ..child.process import (
    live_queue_slot_keys_for_slots,
    reconcile_orphaned_child_queue_entries,
    shutdown_child_process_with_grace,
    start_background_process,
    status_matches,
)
from ..processes import (
    ManagedProcess,
    current_worker_pid_payload,
    install_shutdown_signal_handlers,
    read_worker_pid_file,
    remove_worker_pid_file,
    terminate_process_group,
    worker_pid_file_path,
    write_worker_pid_file,
)
from .admission import (
    admission_has_capacity,
    dequeue_next_across_roots,
    peek_next_across_roots,
    queue_entry_by_id,
    reserve_dequeued_entry,
    resolve_admission_root,
)
from .loop import (
    QueueWorkerLoop,
    fill_worker_slots,
    pop_completed_worker_jobs,
)
from .models import (
    BackgroundRunningJob,
    ReservedQueueEntry,
    SlotFillResult,
)
from .process import (
    ChildProcessQueueWorker,
    PidFileChildProcessQueueWorker,
    QueueWorkerPidFileMixin,
)

__all__ = [
    "BackgroundRunningJob",
    "ChildProcessQueueWorker",
    "admission_has_capacity",
    "ManagedProcess",
    "PidFileChildProcessQueueWorker",
    "QueueWorkerLoop",
    "QueueWorkerPidFileMixin",
    "ReservedQueueEntry",
    "SlotFillResult",
    "current_worker_pid_payload",
    "dequeue_next_across_roots",
    "fill_worker_slots",
    "install_shutdown_signal_handlers",
    "live_queue_slot_keys_for_slots",
    "peek_next_across_roots",
    "pop_completed_worker_jobs",
    "queue_entry_by_id",
    "read_worker_pid_file",
    "reconcile_orphaned_child_queue_entries",
    "remove_worker_pid_file",
    "reserve_dequeued_entry",
    "resolve_admission_root",
    "shutdown_child_process_with_grace",
    "signal",
    "start_background_process",
    "status_matches",
    "terminate_process_group",
    "worker_pid_file_path",
    "write_worker_pid_file",
]
