from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.admission import (
    get_slot,
    recover_orphaned_engine_slots,
    recover_slot_engine_process,
)
from orca_auto.core.commands.queue import QueueRuntime, run_pidfile_queue_worker_command

from .. import lifecycle as _queue_lifecycle
from ..dependencies import ChildQueueWorkerDeps
from ..worker import (
    PidFileChildProcessQueueWorkerHooks,
    dequeue_next_across_roots,
    make_child_queue_worker_deps,
    read_worker_pid_file,
    resolve_admission_root,
)
from ..worker import (
    queue_entry_by_id as _queue_entry_by_id,
)
from .admission import (
    reserve_engine_admission_slot,
    start_engine_child_process,
)
from .runtime_hooks import (
    attach_started_child_process,
    build_child_worker_hooks,
    shutdown_child_job,
)


@dataclass(frozen=True)
class EngineQueueRuntime:
    load_config: Callable[[Any], Any]
    runtime_roots_for_cfg: Callable[[Any], tuple[Path, ...]]
    list_queue: Callable[[str | Path], list[Any]]
    dequeue_next: Callable[[Path], Any | None]
    worker_pid_file_name: str
    dequeue_next_across_roots: Callable[..., tuple[Path, Any] | None] = dequeue_next_across_roots
    dequeue_entry_if_pending: Callable[..., Any | None] | None = None
    accept_entry_fn: Callable[[Any], bool] | None = None
    queue_entry_by_id_fn: Callable[[str | Path, str], Any | None] | None = None

    def _queue_runtime(
        self,
        *,
        list_queue_fn: Callable[[str | Path], list[Any]] | None = None,
    ) -> QueueRuntime:
        return QueueRuntime(
            load_config_fn=self.load_config,
            runtime_roots_for_cfg_fn=self.runtime_roots_for_cfg,
            list_queue_fn=list_queue_fn or self.list_queue,
            dequeue_next_fn=self.dequeue_next,
            dequeue_next_across_roots_fn=self.dequeue_next_across_roots,
            dequeue_entry_if_pending_fn=self.dequeue_entry_if_pending,
            accept_entry_fn=self.accept_entry_fn,
        )

    def queue_roots(self, cfg: Any) -> tuple[Path, ...]:
        return self._queue_runtime().queue_roots(cfg)

    def queue_entries_with_roots(
        self,
        cfg: Any,
        *,
        list_queue_fn: Callable[[str | Path], list[Any]] | None = None,
    ) -> list[tuple[Path, Any]]:
        return self._queue_runtime(list_queue_fn=list_queue_fn).queue_entries_with_roots(cfg)

    def dequeue_next_entry(self, cfg: Any) -> tuple[Path, Any] | None:
        return self._queue_runtime().dequeue_next_entry(cfg)

    def queue_entry_by_id(self, queue_root: Path | str, queue_id: str) -> Any | None:
        if self.queue_entry_by_id_fn is not None:
            entry = self.queue_entry_by_id_fn(queue_root, queue_id)
        else:
            entry = _queue_entry_by_id(
                queue_root,
                queue_id,
                list_queue_fn=self.list_queue,
            )
        if entry is not None and not self.accepts_entry(entry):
            return None
        return entry

    def accepts_entry(self, entry: Any) -> bool:
        return bool(self.accept_entry_fn is None or self.accept_entry_fn(entry))

    def admission_root(self, cfg: Any) -> str:
        return resolve_admission_root(cfg)

    def read_worker_pid(self, allowed_root: Path) -> int | None:
        return read_worker_pid_file(allowed_root, self.worker_pid_file_name)

    def child_worker_deps(
        self,
        *,
        poll_interval_seconds: int,
        time_module: Any,
        release_slot_fn: Callable[[str | Path, str], object],
        start_background_job_process_fn: Callable[..., Any],
        try_reserve_admission_slot_fn: Callable[[Any], str | None],
    ) -> ChildQueueWorkerDeps:
        return make_child_queue_worker_deps(
            poll_interval_seconds=poll_interval_seconds,
            time_module=time_module,
            release_slot_fn=release_slot_fn,
            admission_root_fn=self.admission_root,
            dequeue_next_entry_fn=self.dequeue_next_entry,
            start_background_job_process_fn=start_background_job_process_fn,
            try_reserve_admission_slot_fn=try_reserve_admission_slot_fn,
        )

    def max_concurrent(self, cfg: Any) -> int:
        return max(1, int(getattr(cfg.runtime, "max_concurrent", 1)))

    def reserve_admission_slot(
        self,
        cfg: Any,
        *,
        engine: str,
        reserve_slot_fn: Callable[..., str | None],
    ) -> str | None:
        return reserve_engine_admission_slot(
            cfg,
            engine=engine,
            reserve_slot_fn=reserve_slot_fn,
        )

    def attach_started_child_process(
        self,
        *,
        engine: str,
        worker: Any,
        queue_root: Path,
        entry: Any,
        process: Any,
        admission_token: str,
        activate_reserved_slot_fn: Callable[..., Any],
        terminate_process_fn: Callable[[Any], Any],
        mark_failed_fn: Callable[..., Any],
    ) -> bool:
        return attach_started_child_process(
            engine=engine,
            worker=worker,
            queue_root=queue_root,
            entry=entry,
            process=process,
            admission_token=admission_token,
            activate_reserved_slot_fn=activate_reserved_slot_fn,
            terminate_process_fn=terminate_process_fn,
            mark_failed_fn=mark_failed_fn,
        )

    def shutdown_child_job(
        self,
        worker: Any,
        job: Any,
        *,
        finalize_child_exit_fn: Callable[..., Any],
        grace_seconds: float,
        sleep_fn: Callable[[float], None],
    ) -> None:
        shutdown_child_job(
            worker,
            job,
            finalize_child_exit_fn=finalize_child_exit_fn,
            grace_seconds=grace_seconds,
            sleep_fn=sleep_fn,
        )

    def child_worker_hooks(
        self,
        *,
        engine: str,
        handle_worker_start_error_fn: Callable[[Any, Path, Any, str, OSError], None],
        finalize_completed_job_fn: Callable[[Any, str, Any, int], None],
        finalize_child_exit_fn: Callable[..., Any],
        reconcile_worker_state_fn: Callable[[Any], None],
        activate_reserved_slot_fn: Callable[..., Any],
        terminate_process_fn: Callable[[Any], Any],
        mark_failed_fn: Callable[..., Any],
        shutdown_grace_seconds: float,
        sleep_fn: Callable[[float], None],
        on_worker_process_started_fn: Callable[[Any, Path, Any, Any, str], bool] | None = None,
        shutdown_running_job_fn: Callable[[Any, str, Any], Any] | None = None,
        before_shutdown_all_fn: Callable[[Any, int], Any] | None = None,
    ) -> PidFileChildProcessQueueWorkerHooks:
        return build_child_worker_hooks(
            engine=engine,
            handle_worker_start_error_fn=handle_worker_start_error_fn,
            finalize_completed_job_fn=finalize_completed_job_fn,
            finalize_child_exit_fn=finalize_child_exit_fn,
            reconcile_worker_state_fn=reconcile_worker_state_fn,
            activate_reserved_slot_fn=activate_reserved_slot_fn,
            terminate_process_fn=terminate_process_fn,
            mark_failed_fn=mark_failed_fn,
            shutdown_grace_seconds=shutdown_grace_seconds,
            sleep_fn=sleep_fn,
            on_worker_process_started_fn=on_worker_process_started_fn,
            shutdown_running_job_fn=shutdown_running_job_fn,
            before_shutdown_all_fn=before_shutdown_all_fn,
        )

    def start_child_process(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
        start_background_process_fn: Callable[[list[str]], Any],
        build_worker_child_command_fn: Callable[..., list[str]],
        include_admission_root: bool,
    ) -> Any:
        return start_engine_child_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
            start_background_process_fn=start_background_process_fn,
            build_worker_child_command_fn=build_worker_child_command_fn,
            include_admission_root=include_admission_root,
        )

    def finalize_child_exit(
        self,
        cfg: Any,
        job: Any,
        *,
        rc: int,
        shutdown_requested: bool,
        admission_root: str | Path | None = None,
        find_queue_entry_fn: Callable[[Any, str], Any | None],
        mark_cancelled_fn: Callable[..., Any],
        requeue_running_entry_fn: Callable[..., Any],
        mark_failed_fn: Callable[..., Any],
        mark_recovery_pending_fn: Callable[..., Any],
        release_admission_slot_fn: Callable[[str], Any],
    ) -> None:
        """Finalize a managed engine child without releasing a live engine process."""
        if admission_root is not None:
            slot = get_slot(admission_root, job.admission_token)
            if slot is not None:
                recover_slot_engine_process(admission_root, job.admission_token)
        _queue_lifecycle.finalize_child_exit_with_policy(
            cfg,
            job,
            policy=_queue_lifecycle.ChildExitPolicy(
                shutdown_requested=shutdown_requested,
                fail_unexpected_exit=True,
                use_entry_fallback=False,
                recovery_entry_fn=lambda _current, current_job: current_job.entry,
            ),
            find_queue_entry_fn=find_queue_entry_fn,
            mark_cancelled_fn=mark_cancelled_fn,
            requeue_running_entry_fn=requeue_running_entry_fn,
            mark_recovery_pending_fn=mark_recovery_pending_fn,
            release_admission_slot_fn=release_admission_slot_fn,
            mark_failed_fn=mark_failed_fn,
            rc=rc,
        )

    def reconcile_orphaned_running(
        self,
        cfg: Any,
        *,
        admission_root: Any,
        list_slots_fn: Callable[[Any], list[Any]],
        reconcile_stale_slots_fn: Callable[[Any], Any],
        reconcile_orphaned_child_queue_entries_fn: Callable[..., Any],
        mark_cancelled_fn: Callable[..., Any],
        requeue_running_entry_fn: Callable[..., Any],
        mark_recovery_pending_fn: Callable[..., Any],
        list_queue_fn: Callable[[str | Path], list[Any]] | None = None,
    ) -> None:
        """Recover admission records and reconcile this engine's orphaned rows."""
        queue_lister = list_queue_fn or self.list_queue
        recover_orphaned_engine_slots(admission_root, strict=False)
        _queue_lifecycle.reconcile_orphaned_running_with_policy(
            cfg,
            policy=_queue_lifecycle.OrphanedRunningPolicy(),
            admission_root=admission_root,
            queue_roots_fn=self.queue_roots,
            list_queue_fn=lambda root: [
                entry for entry in queue_lister(root) if self.accepts_entry(entry)
            ],
            list_slots_fn=list_slots_fn,
            reconcile_stale_slots_fn=reconcile_stale_slots_fn,
            reconcile_orphaned_child_queue_entries_fn=(reconcile_orphaned_child_queue_entries_fn),
            mark_cancelled_fn=mark_cancelled_fn,
            requeue_running_entry_fn=requeue_running_entry_fn,
            mark_recovery_pending_fn=mark_recovery_pending_fn,
        )

    def run_pidfile_worker_command(
        self,
        args: Any,
        *,
        config_path_fn: Callable[[Any], str],
        worker_factory: Callable[..., Any],
        load_config_fn: Callable[[Any], Any] | None = None,
        read_worker_pid_fn: Callable[[Path], int | None] | None = None,
        existing_pid_report_fn: Callable[[int], Any] | None = None,
        max_concurrent_fn: Callable[[Any], int] | None = None,
    ) -> int:
        return run_pidfile_queue_worker_command(
            args,
            load_config_fn=load_config_fn or self.load_config,
            config_path_fn=config_path_fn,
            read_worker_pid_fn=read_worker_pid_fn or self.read_worker_pid,
            existing_pid_report_fn=existing_pid_report_fn,
            max_concurrent_fn=max_concurrent_fn or self.max_concurrent,
            worker_factory=worker_factory,
        )


__all__ = ["EngineQueueRuntime"]
