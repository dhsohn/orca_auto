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

from .. import lifecycle as _queue_lifecycle
from ..engine import admission as _engine_admission
from ..worker.admission import engine_queue_worker_source


@dataclass(frozen=True)
class InternalEngineAdmission:
    engine: str
    include_admission_root: bool = False

    @property
    def child_source(self) -> str:
        return f"{engine_queue_worker_source(self.engine)}.child"

    def reserve_admission_slot(
        self,
        cfg: Any,
        *,
        reserve_slot_fn: Callable[..., str | None],
    ) -> str | None:
        return _engine_admission.reserve_engine_admission_slot(
            cfg,
            engine=self.engine,
            reserve_slot_fn=reserve_slot_fn,
        )

    def start_background_job_process(
        self,
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
        start_background_process_fn: Callable[[list[str]], Any],
        build_worker_child_command_fn: Callable[..., list[str]],
    ) -> Any:
        return _engine_admission.start_engine_child_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
            start_background_process_fn=start_background_process_fn,
            build_worker_child_command_fn=build_worker_child_command_fn,
            include_admission_root=self.include_admission_root,
        )

    def attach_started_process(
        self,
        *,
        admission_root: str | Path,
        queue_root: Path,
        entry: Any,
        process: Any,
        admission_token: str,
        activate_reserved_slot_fn: Callable[..., Any],
        terminate_process_fn: Callable[[Any], Any],
        mark_entry_failed_and_release_fn: Callable[..., Any],
        mark_failed_fn: Callable[..., Any],
        source: str | None = None,
    ) -> bool:
        return _engine_admission.attach_started_process(
            admission_root=admission_root,
            queue_root=queue_root,
            entry=entry,
            process=process,
            admission_token=admission_token,
            activate_reserved_slot_fn=activate_reserved_slot_fn,
            terminate_process_fn=terminate_process_fn,
            mark_entry_failed_and_release_fn=mark_entry_failed_and_release_fn,
            mark_failed_fn=mark_failed_fn,
            source=source or self.child_source,
        )

    def attach_started_process_metadata(
        self,
        *,
        worker: Any,
        queue_root: Path,
        entry: Any,
        process: Any,
        admission_token: str,
        hooks: Any,
    ) -> bool:
        return _queue_lifecycle.attach_started_process_metadata(
            worker,
            queue_root,
            entry,
            process=process,
            admission_token=admission_token,
            hooks=hooks,
        )

    def mark_worker_start_error(
        self,
        *,
        queue_root: Path,
        entry: Any,
        admission_token: str,
        exc: OSError,
        mark_entry_failed_and_release_fn: Callable[..., Any],
        mark_failed_fn: Callable[..., Any],
    ) -> None:
        _engine_admission.mark_worker_start_error(
            queue_root=queue_root,
            entry=entry,
            admission_token=admission_token,
            exc=exc,
            mark_entry_failed_and_release_fn=mark_entry_failed_and_release_fn,
            mark_failed_fn=mark_failed_fn,
        )

    def finalize_start_error_as_terminal_result(self, cfg: Any, **kwargs: Any) -> None:
        _engine_admission.finalize_start_error_as_terminal_result(cfg, **kwargs)


@dataclass(frozen=True)
class InternalEngineLifecycle:
    engine: str

    @property
    def child_exit_policy(self) -> _queue_lifecycle.ChildExitPolicy:
        return _queue_lifecycle.ChildExitPolicy(
            fail_unexpected_exit=True,
            use_entry_fallback=False,
            recovery_entry_fn=lambda _current, current_job: current_job.entry,
        )

    @property
    def orphaned_running_policy(self) -> _queue_lifecycle.OrphanedRunningPolicy:
        return _queue_lifecycle.OrphanedRunningPolicy()

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
        # A child can be SIGKILLed while its engine runs in a separate
        # session. Reap and verify that durable slot record before any queue
        # terminalization or admission release.
        if admission_root is not None:
            slot = get_slot(admission_root, job.admission_token)
            if slot is not None:
                recover_slot_engine_process(
                    admission_root,
                    job.admission_token,
                )
        _queue_lifecycle.finalize_child_exit_with_policy(
            cfg,
            job,
            policy=_queue_lifecycle.ChildExitPolicy(
                shutdown_requested=shutdown_requested,
                fail_unexpected_exit=self.child_exit_policy.fail_unexpected_exit,
                use_entry_fallback=self.child_exit_policy.use_entry_fallback,
                recovery_entry_fn=self.child_exit_policy.recovery_entry_fn,
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
        queue_roots_fn: Callable[[Any], tuple[Any, ...]],
        list_queue_fn: Callable[[Any], list[Any]],
        list_slots_fn: Callable[[Any], list[Any]],
        reconcile_stale_slots_fn: Callable[[Any], Any],
        reconcile_orphaned_child_queue_entries_fn: Callable[..., Any],
        mark_cancelled_fn: Callable[..., Any],
        requeue_running_entry_fn: Callable[..., Any],
        mark_recovery_pending_fn: Callable[..., Any],
    ) -> None:
        # Admission capacity is global across engines. Any worker starting on
        # this root must recover every trustworthy dead-owner engine record.
        recover_orphaned_engine_slots(admission_root, strict=False)
        _queue_lifecycle.reconcile_orphaned_running_with_policy(
            cfg,
            policy=self.orphaned_running_policy,
            admission_root=admission_root,
            queue_roots_fn=queue_roots_fn,
            list_queue_fn=list_queue_fn,
            list_slots_fn=list_slots_fn,
            reconcile_stale_slots_fn=reconcile_stale_slots_fn,
            mark_cancelled_fn=mark_cancelled_fn,
            requeue_running_entry_fn=requeue_running_entry_fn,
            mark_recovery_pending_fn=mark_recovery_pending_fn,
            reconcile_orphaned_child_queue_entries_fn=reconcile_orphaned_child_queue_entries_fn,
        )


__all__ = ["InternalEngineAdmission", "InternalEngineLifecycle"]
