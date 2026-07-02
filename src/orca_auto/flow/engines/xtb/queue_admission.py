from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.queue.internal_engine import InternalEngineSpec

_ADMISSION = InternalEngineSpec(engine="xtb").admission()

reserve_admission_slot = _ADMISSION.reserve_admission_slot
start_background_job_process = _ADMISSION.start_background_job_process
mark_worker_start_error = _ADMISSION.mark_worker_start_error
attach_started_process = _ADMISSION.attach_started_process


def finalize_worker_start_error(
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    admission_token: str,
    exc: OSError,
    release_admission_slot_fn: Callable[[str], Any],
    build_terminal_result_fn: Callable[..., Any],
    finalize_execution_result_fn: Callable[..., Any],
    job_dir_fn: Callable[[Any], Path],
    selected_xyz_fn: Callable[[Any], Path],
    job_type_fn: Callable[[Any], str],
    reaction_key_fn: Callable[[Any, Path], str],
    input_summary_fn: Callable[[Any], dict[str, Any]],
    entry_resource_request_fn: Callable[[Any, Any], dict[str, int]],
) -> None:
    _ADMISSION.finalize_start_error_as_terminal_result(
        cfg,
        queue_root=queue_root,
        entry=entry,
        admission_token=admission_token,
        exc=exc,
        release_admission_slot_fn=release_admission_slot_fn,
        build_terminal_result_fn=build_terminal_result_fn,
        finalize_execution_result_fn=finalize_execution_result_fn,
        job_dir_fn=job_dir_fn,
        selected_xyz_fn=selected_xyz_fn,
        job_type_fn=job_type_fn,
        reaction_key_fn=reaction_key_fn,
        input_summary_fn=input_summary_fn,
        entry_resource_request_fn=entry_resource_request_fn,
    )


__all__ = [
    "attach_started_process",
    "finalize_worker_start_error",
    "mark_worker_start_error",
    "reserve_admission_slot",
    "start_background_job_process",
]
