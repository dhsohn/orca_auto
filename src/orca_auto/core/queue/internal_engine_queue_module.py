from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.engines.definitions import EngineDefinition

from .internal_engine_runtime import InternalEngineQueueRuntime
from .internal_engine_spec import InternalEngineSpec
from .internal_engine_worker_facade import (
    InternalEngineQueueWorkerDeps,
    InternalEngineQueueWorkerFacade,
)


@dataclass(frozen=True)
class InternalEngineQueueModule:
    runtime: InternalEngineQueueRuntime
    facade: InternalEngineQueueWorkerFacade

    @classmethod
    def create(
        cls,
        *,
        spec: InternalEngineSpec,
        load_config: Callable[[Any], Any],
        runtime_roots_for_cfg: Callable[[Any], tuple[Path, ...]],
        list_queue: Callable[[str | Path], list[Any]],
        dequeue_next: Callable[[Path], Any | None],
        poll_interval_seconds: int,
        shutdown_grace_seconds: float,
        deps: InternalEngineQueueWorkerDeps,
        dequeue_entry_if_pending: Callable[[Path, str], Any | None] | None = None,
    ) -> InternalEngineQueueModule:
        runtime = InternalEngineQueueRuntime.create(
            spec=spec,
            load_config=load_config,
            runtime_roots_for_cfg=runtime_roots_for_cfg,
            list_queue=list_queue,
            dequeue_next=dequeue_next,
            dequeue_entry_if_pending=dequeue_entry_if_pending,
        )
        return cls(
            runtime=runtime,
            facade=InternalEngineQueueWorkerFacade(
                runtime=runtime,
                poll_interval_seconds=poll_interval_seconds,
                shutdown_grace_seconds=shutdown_grace_seconds,
                deps=deps,
            ),
        )

    @classmethod
    def create_from_definition(
        cls,
        *,
        definition: EngineDefinition,
        spec: InternalEngineSpec,
        poll_interval_seconds: int,
        shutdown_grace_seconds: float,
        deps: InternalEngineQueueWorkerDeps,
        runtime_roots_for_cfg: Callable[[Any], tuple[Path, ...]] | None = None,
        list_queue: Callable[[str | Path], list[Any]] | None = None,
        dequeue_next: Callable[[Path], Any | None] | None = None,
        dequeue_entry_if_pending: Callable[[Path, str], Any | None] | None = None,
    ) -> InternalEngineQueueModule:
        queue_functions = definition.queue_functions
        if queue_functions is None:
            raise ValueError(
                "EngineDefinition.queue_functions is required for queue module support"
            )
        worker_pid_file_name = (
            queue_functions.worker_pid_file_name or definition.worker_pid_file_name
        )
        runtime = InternalEngineQueueRuntime.create(
            spec=spec,
            load_config=definition.load_config,
            runtime_roots_for_cfg=runtime_roots_for_cfg or queue_functions.runtime_roots_for_cfg,
            list_queue=list_queue or queue_functions.list_queue,
            dequeue_next=dequeue_next or queue_functions.dequeue_next,
            dequeue_entry_if_pending=dequeue_entry_if_pending
            or queue_functions.dequeue_entry_if_pending,
            worker_pid_file_name=worker_pid_file_name,
        )
        return cls(
            runtime=runtime,
            facade=InternalEngineQueueWorkerFacade(
                runtime=runtime,
                poll_interval_seconds=poll_interval_seconds,
                shutdown_grace_seconds=shutdown_grace_seconds,
                deps=deps,
            ),
        )

    @property
    def queue_roots(self) -> Callable[[Any], tuple[Path, ...]]:
        return self.runtime.queue_roots

    @property
    def queue_entries_with_roots(self) -> Callable[[Any], list[tuple[Path, Any]]]:
        return self.runtime.queue_entries_with_roots

    @property
    def dequeue_next_entry(self) -> Callable[[Any], tuple[Path, Any] | None]:
        return self.runtime.dequeue_next_entry

    @property
    def read_worker_pid(self) -> Callable[[Path], int | None]:
        return self.runtime.read_worker_pid

    @property
    def queue_entry_by_id(self) -> Callable[[Path | str, str], Any | None]:
        return self.runtime.queue_entry_by_id

    @property
    def admission_root(self) -> Callable[[Any], str]:
        return self.runtime.admission_root

    def queue_worker_deps(self) -> Any:
        return self.facade.queue_worker_deps()

    def queue_worker_hooks(self) -> Any:
        return self.facade.queue_worker_hooks()

    def try_reserve_admission_slot(self, cfg: Any) -> str | None:
        return self.facade.try_reserve_admission_slot(cfg)

    def start_background_job_process(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
    ) -> Any:
        return self.facade.start_background_job_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
        )

    def config_path_for_worker(self, args: Any) -> str:
        return self.facade.config_path_for_worker(args)

    def finalize_child_exit(self, worker: Any, job: Any, *, rc: int) -> None:
        self.facade.finalize_child_exit(worker, job, rc=rc)

    def reconcile_orphaned_running(
        self,
        worker: Any,
        *,
        list_slots_fn: Callable[[Any], list[Any]] | None = None,
    ) -> None:
        self.facade.reconcile_orphaned_running(worker, list_slots_fn=list_slots_fn)

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
        return self.facade.run_pidfile_worker_command(
            args,
            config_path_fn=config_path_fn,
            config_path_keyword=config_path_keyword,
            load_config_fn=load_config_fn,
            read_worker_pid_fn=read_worker_pid_fn,
            existing_pid_report_fn=existing_pid_report_fn,
            max_concurrent_fn=max_concurrent_fn,
            worker_factory=worker_factory,
        )


__all__ = ["InternalEngineQueueModule"]
