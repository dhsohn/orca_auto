from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime import InternalEngineQueueRuntime, own_engine_accept_entry
from .worker_deps import (
    InternalEngineQueueWorkerDeps,
    InternalEngineQueueWorkerDepsResolver,
)


@dataclass(frozen=True)
class InternalEngineQueueWorkerLifecycleFacade:
    runtime: InternalEngineQueueRuntime
    resolver: InternalEngineQueueWorkerDepsResolver
    shutdown_grace_seconds: float

    def queue_worker_hooks(self) -> Any:
        deps = self.resolver.deps

        def activate_reserved_slot_fn(*args: Any, **kwargs: Any) -> Any:
            return deps.activate_reserved_slot(*args, **kwargs)

        def terminate_process_fn(process: Any) -> Any:
            return deps.terminate_process(process)

        def mark_failed_fn(*args: Any, **kwargs: Any) -> Any:
            return deps.mark_failed(*args, **kwargs)

        def sleep_fn(seconds: float) -> None:
            deps.time_module.sleep(seconds)

        return self.runtime.child_worker_hooks(
            engine=self.runtime.spec.engine,
            handle_worker_start_error_fn=deps.handle_worker_start_error,
            finalize_completed_job_fn=deps.finalize_completed_job,
            finalize_child_exit_fn=deps.finalize_child_exit,
            reconcile_worker_state_fn=deps.reconcile_worker_state,
            activate_reserved_slot_fn=activate_reserved_slot_fn,
            terminate_process_fn=terminate_process_fn,
            mark_failed_fn=mark_failed_fn,
            shutdown_grace_seconds=self.shutdown_grace_seconds,
            sleep_fn=sleep_fn,
            on_worker_process_started_fn=deps.on_worker_process_started,
            shutdown_running_job_fn=deps.shutdown_running_job,
            before_shutdown_all_fn=deps.before_shutdown_all,
        )

    def finalize_child_exit(self, worker: Any, job: Any, *, rc: int) -> None:
        deps = self.resolver.deps
        self.runtime.spec.lifecycle().finalize_child_exit(
            worker.cfg,
            job,
            rc=rc,
            shutdown_requested=worker._shutdown_requested,
            admission_root=getattr(worker, "admission_root", None),
            find_queue_entry_fn=self.resolver.find_queue_entry,
            mark_cancelled_fn=deps.mark_cancelled,
            requeue_running_entry_fn=deps.requeue_running_entry,
            mark_failed_fn=deps.mark_failed,
            mark_recovery_pending_fn=deps.mark_recovery_pending,
            release_admission_slot_fn=worker._release_admission_slot,
        )

    def reconcile_orphaned_running(
        self,
        worker: Any,
        *,
        list_slots_fn: Callable[[Any], list[Any]] | None = None,
    ) -> None:
        deps = self.resolver.deps
        accept_entry = own_engine_accept_entry(self.runtime.spec.engine)
        self.runtime.spec.lifecycle().reconcile_orphaned_running(
            worker.cfg,
            admission_root=worker.admission_root,
            queue_roots_fn=self.runtime.queue_roots,
            list_queue_fn=lambda root: [
                entry for entry in deps.list_queue(root) if accept_entry(entry)
            ],
            list_slots_fn=list_slots_fn or deps.list_slots,
            reconcile_stale_slots_fn=deps.reconcile_stale_slots,
            reconcile_orphaned_child_queue_entries_fn=(deps.reconcile_orphaned_child_queue_entries),
            mark_cancelled_fn=deps.mark_cancelled,
            requeue_running_entry_fn=deps.requeue_running_entry,
            mark_recovery_pending_fn=deps.mark_recovery_pending,
        )


@dataclass(frozen=True)
class InternalEngineQueueWorkerCommandRunner:
    runtime: InternalEngineQueueRuntime
    resolver: InternalEngineQueueWorkerDepsResolver

    def run_pidfile_worker_command(
        self,
        args: Any,
        *,
        config_path_fn: Callable[[Any], str],
        config_path_keyword: bool = True,
        load_config_fn: Callable[[Any], Any] | None = None,
        read_worker_pid_fn: Callable[[Path], int | None] | None = None,
        existing_pid_report_fn: Callable[[int], Any] | None = None,
        max_concurrent_fn: Callable[[Any], int] | None = None,
        worker_factory: Callable[..., Any] | None = None,
    ) -> int:
        deps = self.resolver.deps

        def default_worker_factory(cfg: Any, config_path: str, **kwargs: Any) -> Any:
            worker_cls = deps.worker_class
            if worker_cls is None:
                raise ValueError("worker_class is required for queue worker command support")
            if config_path_keyword:
                return worker_cls(cfg, config_path=config_path, **kwargs)
            return worker_cls(cfg, config_path, **kwargs)

        resolved_load_config = load_config_fn or deps.load_config
        if resolved_load_config is None:
            raise ValueError("load_config is required for queue worker command support")
        resolved_read_worker_pid = read_worker_pid_fn or deps.read_worker_pid
        if resolved_read_worker_pid is None:
            raise ValueError("read_worker_pid is required for queue worker command support")

        return self.runtime.run_pidfile_worker_command(
            args,
            config_path_fn=config_path_fn,
            load_config_fn=resolved_load_config,
            read_worker_pid_fn=resolved_read_worker_pid,
            existing_pid_report_fn=existing_pid_report_fn,
            max_concurrent_fn=max_concurrent_fn,
            worker_factory=worker_factory or default_worker_factory,
        )


@dataclass(frozen=True)
class InternalEngineQueueWorkerFacade:
    runtime: InternalEngineQueueRuntime
    deps: InternalEngineQueueWorkerDeps
    poll_interval_seconds: int
    shutdown_grace_seconds: float

    def _resolver(self) -> InternalEngineQueueWorkerDepsResolver:
        return InternalEngineQueueWorkerDepsResolver(
            runtime=self.runtime,
            deps=self.deps,
        )

    def _lifecycle(self) -> InternalEngineQueueWorkerLifecycleFacade:
        return InternalEngineQueueWorkerLifecycleFacade(
            runtime=self.runtime,
            resolver=self._resolver(),
            shutdown_grace_seconds=self.shutdown_grace_seconds,
        )

    def queue_worker_deps(self) -> Any:
        return self._resolver().queue_worker_deps(
            poll_interval_seconds=self.poll_interval_seconds,
            start_background_job_process_fn=self.start_background_job_process,
            try_reserve_admission_slot_fn=self.try_reserve_admission_slot,
        )

    def try_reserve_admission_slot(self, cfg: Any) -> str | None:
        return self._resolver().try_reserve_admission_slot(cfg)

    def start_background_job_process(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
    ) -> Any:
        return self._resolver().start_background_job_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
        )

    def config_path_for_worker(self, args: Any) -> str:
        return self._resolver().config_path_for_worker(args)

    def queue_worker_hooks(self) -> Any:
        return self._lifecycle().queue_worker_hooks()

    def run_pidfile_worker_command(
        self,
        args: Any,
        *,
        config_path_fn: Callable[[Any], str],
        config_path_keyword: bool = True,
        load_config_fn: Callable[[Any], Any] | None = None,
        read_worker_pid_fn: Callable[[Path], int | None] | None = None,
        existing_pid_report_fn: Callable[[int], Any] | None = None,
        max_concurrent_fn: Callable[[Any], int] | None = None,
        worker_factory: Callable[..., Any] | None = None,
    ) -> int:
        return InternalEngineQueueWorkerCommandRunner(
            runtime=self.runtime,
            resolver=self._resolver(),
        ).run_pidfile_worker_command(
            args,
            config_path_fn=config_path_fn,
            config_path_keyword=config_path_keyword,
            load_config_fn=load_config_fn,
            read_worker_pid_fn=read_worker_pid_fn,
            existing_pid_report_fn=existing_pid_report_fn,
            max_concurrent_fn=max_concurrent_fn,
            worker_factory=worker_factory,
        )

    def finalize_child_exit(self, worker: Any, job: Any, *, rc: int) -> None:
        self._lifecycle().finalize_child_exit(worker, job, rc=rc)

    def reconcile_orphaned_running(
        self,
        worker: Any,
        *,
        list_slots_fn: Callable[[Any], list[Any]] | None = None,
    ) -> None:
        self._lifecycle().reconcile_orphaned_running(worker, list_slots_fn=list_slots_fn)


__all__ = [
    "InternalEngineQueueWorkerCommandRunner",
    "InternalEngineQueueWorkerDeps",
    "InternalEngineQueueWorkerDepsResolver",
    "InternalEngineQueueWorkerFacade",
    "InternalEngineQueueWorkerLifecycleFacade",
]
