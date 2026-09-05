from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.worker import HookedPidFileChildProcessQueueWorker

QUEUE_WORKER_MODULE = "orca_auto.core.engines.queue_worker"


WorkerCallback = Callable[..., Any]
ReserveGateCallback = Callable[[Any], tuple[str, Any | None] | None]


@dataclass(frozen=True)
class EngineWorkerPolicy:
    """The engine-owned behaviour a parent queue worker composes in.

    Every field is optional; an engine sets only the steps it owns and the
    shared worker keeps its default for the rest. ORCA installs its terminal
    replay, publication repair and cancellation policies here, while the
    internal xTB/CREST engines install their publication-repair gate and
    orphan reconciliation. The value is immutable so a running worker cannot
    have its policy swapped underneath it.
    """

    after_init: WorkerCallback | None = None
    before_run: WorkerCallback | None = None
    after_run: WorkerCallback | None = None
    keyboard_interrupt: WorkerCallback | None = None
    running_queue_id: WorkerCallback | None = None
    running_job_factory: WorkerCallback | None = None
    finalize_finished_job: WorkerCallback | None = None
    finalize_child_exit: WorkerCallback | None = None
    reconcile_orphaned_running: WorkerCallback | None = None
    check_cancel_requests: WorkerCallback | None = None
    reserve_gate: ReserveGateCallback | None = None


class EngineQueueWorker(HookedPidFileChildProcessQueueWorker):
    """Common parent queue worker for orca_auto engine runtimes."""

    def __init__(
        self,
        cfg: Any,
        config_path: str,
        *,
        engine: str,
        deps: Any,
        hooks: Any,
        worker_pid_file_name: str,
        max_concurrent: int | None = None,
        admission_root: str | Path | None = None,
        policy: EngineWorkerPolicy | None = None,
    ) -> None:
        self.engine = engine
        self.admission_limit: int | None = None
        # Engine-owned worker state. The engine's after_init hook may attach a
        # typed object here (for example the ORCA terminal-replay bookkeeping)
        # instead of stuffing untyped attributes onto the shared worker.
        self.engine_state: Any = None
        self.policy = policy if policy is not None else EngineWorkerPolicy()
        super().__init__(
            cfg,
            config_path=config_path,
            max_concurrent=max_concurrent,
            deps=deps,
            hooks=hooks,
            worker_pid_file_name=worker_pid_file_name,
            admission_root=admission_root,
        )
        if self.policy.after_init is not None:
            self.policy.after_init(self)

    def _before_run(self) -> None:
        super()._before_run()
        if self.policy.before_run is not None:
            self.policy.before_run(self)

    def _after_run(self) -> None:
        super()._after_run()
        if self.policy.after_run is not None:
            self.policy.after_run(self)

    def _run_iteration(self) -> None:
        try:
            super()._run_iteration()
        except KeyboardInterrupt:
            if self.policy.keyboard_interrupt is not None:
                self.policy.keyboard_interrupt(self)
            raise

    def _reserve_next_entry(self) -> tuple[str, Any | None]:
        if self.policy.reserve_gate is not None:
            gated = self.policy.reserve_gate(self)
            if gated is not None:
                return gated
        return super()._reserve_next_entry()

    def _running_queue_id(self, entry: Any) -> str:
        if self.policy.running_queue_id is not None:
            return str(self.policy.running_queue_id(entry))
        return super()._running_queue_id(entry)

    def _make_running_job(
        self,
        *,
        queue_root: Path,
        entry: Any,
        process: Any,
        admission_token: str,
    ) -> Any:
        if self.policy.running_job_factory is not None:
            return self.policy.running_job_factory(
                self,
                queue_root=queue_root,
                entry=entry,
                process=process,
                admission_token=admission_token,
            )
        return super()._make_running_job(
            queue_root=queue_root,
            entry=entry,
            process=process,
            admission_token=admission_token,
        )

    def _finalize_finished_job(self, queue_id: str, job: Any, *, rc: int) -> None:
        if self.policy.finalize_finished_job is not None:
            self.policy.finalize_finished_job(self, queue_id, job, rc=rc)
            return
        self._finalize_completed_job(queue_id, job, rc)

    def _finalize_child_exit(self, job: Any, *, rc: int) -> None:
        if self.policy.finalize_child_exit is None:
            raise AttributeError("finalize_child_exit callback is not configured")
        self.policy.finalize_child_exit(self, job, rc=rc)

    def _reconcile_orphaned_running(self) -> None:
        if self.policy.reconcile_orphaned_running is None:
            self._reconcile_worker_state()
            return
        self.policy.reconcile_orphaned_running(self)

    def _check_cancel_requests(self) -> None:
        if self.policy.check_cancel_requests is None:
            super()._check_cancel_requests()
            return
        self.policy.check_cancel_requests(self)


def build_runtime_engine_queue_worker(
    cfg: Any,
    *,
    config_path: str | None,
    default_config_path: Callable[[], str],
    engine: str,
    max_concurrent: int | None,
    deps: Any,
    hooks: Any,
    worker_pid_file_name: str,
    admission_root: str | Path,
    policy: EngineWorkerPolicy | None = None,
) -> EngineQueueWorker:
    resolved_config_path = str(config_path or "").strip() or default_config_path()
    return EngineQueueWorker(
        cfg,
        config_path=resolved_config_path,
        engine=engine,
        max_concurrent=max_concurrent,
        deps=deps,
        hooks=hooks,
        worker_pid_file_name=worker_pid_file_name,
        admission_root=admission_root,
        policy=policy,
    )


def run_engine_queue_worker(engine: str, argv: list[str]) -> int:
    from .registry import get_engine_definition

    definition = get_engine_definition(engine)
    return definition.queue_worker_main(argv)


def build_engine_queue_worker_parser(prog: str) -> argparse.ArgumentParser:
    """The argv contract every engine's parent queue worker is invoked with."""

    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--config", required=True)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = build_engine_queue_worker_parser(f"python -m {QUEUE_WORKER_MODULE}")
    parser.add_argument("--engine", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, remainder = parser.parse_known_args(argv)
    forwarded = ["--config", args.config, *remainder]
    return run_engine_queue_worker(str(args.engine).strip().lower(), forwarded)


__all__ = [
    "EngineQueueWorker",
    "EngineWorkerPolicy",
    "QUEUE_WORKER_MODULE",
    "build_runtime_engine_queue_worker",
    "build_engine_queue_worker_parser",
    "build_parser",
    "main",
    "run_engine_queue_worker",
]


if __name__ == "__main__":
    raise SystemExit(main())
