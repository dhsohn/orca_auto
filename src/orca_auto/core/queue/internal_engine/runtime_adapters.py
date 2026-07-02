from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def reserve_admission_slot_fn(
    runtime: Any,
    reserve_slot_fn: Callable[..., str | None],
) -> Callable[[Any], str | None]:
    def _reserve_admission_slot(cfg: Any) -> str | None:
        return runtime.reserve_admission_slot(cfg, reserve_slot_fn=reserve_slot_fn)

    return _reserve_admission_slot


def start_background_job_process_fn(
    runtime: Any,
    *,
    start_background_process_fn: Callable[[list[str]], Any],
    build_worker_child_command_fn: Callable[..., list[str]],
) -> Callable[..., Any]:
    def _start_background_job_process(
        *,
        config_path: str,
        queue_root: Path,
        entry: Any,
        admission_root: str | Path,
        admission_token: str,
    ) -> Any:
        return runtime.start_child_process(
            config_path=config_path,
            queue_root=queue_root,
            entry=entry,
            admission_root=admission_root,
            admission_token=admission_token,
            start_background_process_fn=start_background_process_fn,
            build_worker_child_command_fn=build_worker_child_command_fn,
        )

    return _start_background_job_process


def config_path_for_worker_fn(
    *,
    config_path_for_worker_fn: Callable[..., str],
    default_config_path_fn: Callable[[], str],
) -> Callable[[Any], str]:
    def _config_path_for_worker(args: Any) -> str:
        return config_path_for_worker_fn(
            args,
            default_config_path_fn=default_config_path_fn,
        )

    return _config_path_for_worker


__all__ = [
    "config_path_for_worker_fn",
    "reserve_admission_slot_fn",
    "start_background_job_process_fn",
]
