from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.engine_catalog import find_engine_catalog_entry

from ..engine import admission as _engine_admission
from ..engine.runtime import EngineQueueRuntime
from . import runtime_adapters as _runtime_adapters
from .spec import InternalEngineSpec


def _entry_text(entry: Any, field: str) -> str:
    return str(getattr(entry, field, "") or "").strip()


def entry_matches_engine_identity(entry: Any, engine: str) -> bool:
    """Return whether a queue row has the complete canonical engine identity."""
    expected_engine = str(engine).strip()
    catalog_entry = find_engine_catalog_entry(expected_engine)
    task_kind = _entry_text(entry, "task_kind")
    if catalog_entry is None:
        expected_app_name = f"orca_auto_{expected_engine}"
        task_kind_matches = task_kind.startswith(f"{expected_engine}_") and bool(
            task_kind.removeprefix(f"{expected_engine}_")
        )
    else:
        expected_app_name = catalog_entry.app_id
        task_kind_matches = task_kind in catalog_entry.task_kinds
    if expected_engine == "xtb" and catalog_entry is not None:
        metadata = getattr(entry, "metadata", {})
        job_type = str(metadata.get("job_type") or "").strip() if isinstance(metadata, dict) else ""
        task_kind_matches = task_kind_matches and (
            task_kind == f"xtb_{job_type}" and task_kind in catalog_entry.task_kinds
        )
    return bool(
        expected_engine
        and _entry_text(entry, "app_name") == expected_app_name
        and _entry_text(entry, "engine") == expected_engine
        and _entry_text(entry, "queue_id")
        and _entry_text(entry, "task_id")
        and task_kind_matches
    )


def own_engine_accept_entry(engine: str) -> Callable[[Any], bool]:
    """Predicate: claim only rows with this engine's complete identity.

    Internal-engine workers share the single runs root with standalone ORCA
    jobs, so they must never claim another engine's entry (e.g. an ORCA OptTS).
    Incomplete, conflicting, or historical task labels remain durable for
    inspection but are quarantined from execution.
    """
    return lambda entry: entry_matches_engine_identity(entry, engine)


@dataclass(frozen=True)
class InternalEngineQueueRuntime:
    spec: InternalEngineSpec
    runtime: EngineQueueRuntime

    @classmethod
    def create(
        cls,
        *,
        spec: InternalEngineSpec,
        load_config: Callable[[Any], Any],
        runtime_roots_for_cfg: Callable[[Any], tuple[Path, ...]],
        list_queue: Callable[[str | Path], list[Any]],
        dequeue_next: Callable[[Path], Any | None],
        dequeue_entry_if_pending: Callable[[Path, str], Any | None] | None = None,
        worker_pid_file_name: str | None = None,
    ) -> InternalEngineQueueRuntime:
        pid_file_name = worker_pid_file_name or spec.worker_pid_file_name
        if not pid_file_name:
            raise ValueError("worker_pid_file_name is required for queue runtime support")
        accept_entry_fn = own_engine_accept_entry(spec.engine)
        return cls(
            spec=spec,
            runtime=EngineQueueRuntime(
                load_config=load_config,
                runtime_roots_for_cfg=runtime_roots_for_cfg,
                list_queue=list_queue,
                dequeue_next=dequeue_next,
                dequeue_entry_if_pending=dequeue_entry_if_pending,
                worker_pid_file_name=pid_file_name,
                accept_entry_fn=accept_entry_fn,
            ),
        )

    def queue_roots(self, cfg: Any) -> tuple[Path, ...]:
        return self.runtime.queue_roots(cfg)

    def queue_entries_with_roots(self, cfg: Any) -> list[tuple[Path, Any]]:
        return self.runtime.queue_entries_with_roots(cfg)

    def dequeue_next_entry(self, cfg: Any) -> tuple[Path, Any] | None:
        return self.runtime.dequeue_next_entry(cfg)

    def queue_entry_by_id(self, queue_root: Path | str, queue_id: str) -> Any | None:
        return self.runtime.queue_entry_by_id(queue_root, queue_id)

    def accepts_entry(self, entry: Any) -> bool:
        accept_entry = self.runtime.accept_entry_fn
        return bool(accept_entry is None or accept_entry(entry))

    def admission_root(self, cfg: Any) -> str:
        return self.runtime.admission_root(cfg)

    def read_worker_pid(self, allowed_root: Path) -> int | None:
        return self.runtime.read_worker_pid(allowed_root)

    def child_worker_deps(self, **kwargs: Any) -> Any:
        return self.runtime.child_worker_deps(**kwargs)

    def max_concurrent(self, cfg: Any) -> int:
        return self.runtime.max_concurrent(cfg)

    def reserve_admission_slot(
        self,
        cfg: Any,
        *,
        reserve_slot_fn: Callable[..., str | None],
        engine: str | None = None,
    ) -> str | None:
        if engine is None or engine == self.spec.engine:
            return self.spec.admission().reserve_admission_slot(
                cfg,
                reserve_slot_fn=reserve_slot_fn,
            )
        return self.runtime.reserve_admission_slot(
            cfg,
            engine=engine,
            reserve_slot_fn=reserve_slot_fn,
        )

    def child_worker_hooks(self, **kwargs: Any) -> Any:
        kwargs.setdefault("engine", self.spec.engine)
        return self.runtime.child_worker_hooks(**kwargs)

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
        include_admission_root: bool | None = None,
    ) -> Any:
        if include_admission_root is None:
            return self.spec.admission().start_background_job_process(
                config_path=config_path,
                queue_root=queue_root,
                entry=entry,
                admission_root=admission_root,
                admission_token=admission_token,
                start_background_process_fn=start_background_process_fn,
                build_worker_child_command_fn=build_worker_child_command_fn,
            )
        return _engine_admission.start_engine_child_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
            start_background_process_fn=start_background_process_fn,
            build_worker_child_command_fn=build_worker_child_command_fn,
            include_admission_root=include_admission_root,
        )

    def run_pidfile_worker_command(self, args: Any, **kwargs: Any) -> int:
        return self.runtime.run_pidfile_worker_command(args, **kwargs)

    def reserve_admission_slot_fn(
        self,
        reserve_slot_fn: Callable[..., str | None],
    ) -> Callable[[Any], str | None]:
        return _runtime_adapters.reserve_admission_slot_fn(self, reserve_slot_fn)

    def start_background_job_process_fn(
        self,
        *,
        start_background_process_fn: Callable[[list[str]], Any],
        build_worker_child_command_fn: Callable[..., list[str]],
    ) -> Callable[..., Any]:
        return _runtime_adapters.start_background_job_process_fn(
            self,
            start_background_process_fn=start_background_process_fn,
            build_worker_child_command_fn=build_worker_child_command_fn,
        )

    def config_path_for_worker_fn(
        self,
        *,
        config_path_for_worker_fn: Callable[..., str],
        default_config_path_fn: Callable[[], str],
    ) -> Callable[[Any], str]:
        return _runtime_adapters.config_path_for_worker_fn(
            config_path_for_worker_fn=config_path_for_worker_fn,
            default_config_path_fn=default_config_path_fn,
        )


__all__ = [
    "InternalEngineQueueRuntime",
    "entry_matches_engine_identity",
    "own_engine_accept_entry",
]
