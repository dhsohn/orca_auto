from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.notifications.engines import notify_xtb_job_finished as notify_job_finished
from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue import mark_cancelled, mark_completed, mark_failed
from orca_auto.core.utils import now_utc_iso
from orca_auto.flow.engines.xtb import artifacts as _queue_artifacts
from orca_auto.flow.engines.xtb import terminal as _queue_terminal
from orca_auto.flow.engines.xtb.job_locations import upsert_job_record
from orca_auto.flow.engines.xtb.runner import XtbRunResult
from orca_auto.flow.engines.xtb.state import write_state
from orca_auto.flow.engines.xtb.worker_context import (
    input_summary as _input_summary,
)
from orca_auto.flow.engines.xtb.worker_context import (
    job_dir as _job_dir,
)
from orca_auto.flow.engines.xtb.worker_context import (
    job_type as _job_type,
)
from orca_auto.flow.engines.xtb.worker_context import (
    reaction_key as _reaction_key,
)
from orca_auto.flow.engines.xtb.worker_context import (
    selected_xyz as _selected_xyz,
)


@dataclass(frozen=True)
class WorkerExecutionOutcome:
    result: XtbRunResult


def write_running_state(
    cfg: Any,
    entry: Any,
    *,
    worker_job_pid: int | None = None,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
) -> None:
    _queue_artifacts.write_running_state(
        cfg,
        entry,
        worker_job_pid=worker_job_pid,
        previous_state=previous_state,
        resumed=resumed,
        input_summary_fn=_input_summary,
        entry_resource_request_fn=_queue_artifacts.entry_resource_request,
        coerce_mapping_fn=_queue_execution.coerce_mapping,
        now_utc_iso_fn=now_utc_iso,
        job_type_fn=_job_type,
        reaction_key_fn=_reaction_key,
        write_state_fn=write_state,
    )


def write_execution_artifacts(
    entry: Any,
    result: XtbRunResult,
    *,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
) -> None:
    _queue_artifacts.write_execution_artifacts(
        entry,
        result,
        previous_state=previous_state,
        resumed=resumed,
        coerce_mapping_fn=_queue_execution.coerce_mapping,
        write_state_fn=write_state,
    )


def build_terminal_result(entry: Any, **kwargs: Any) -> XtbRunResult:
    return _queue_artifacts.build_terminal_result(
        entry,
        **kwargs,
        now_utc_iso_fn=now_utc_iso,
    )


def finalize_execution_result(
    cfg: Any,
    *,
    queue_root: Path,
    entry: Any,
    result: XtbRunResult,
    emit_output: bool,
    previous_state: dict[str, Any] | None = None,
    resumed: bool = False,
) -> WorkerExecutionOutcome:
    return _queue_terminal.finalize_execution_result(
        cfg,
        queue_root=queue_root,
        entry=entry,
        result=result,
        emit_output=emit_output,
        previous_state=previous_state,
        resumed=resumed,
        outcome_cls=WorkerExecutionOutcome,
        write_execution_artifacts_fn=write_execution_artifacts,
        selected_xyz_fn=_selected_xyz,
        job_dir_fn=_job_dir,
        mark_completed_fn=mark_completed,
        mark_cancelled_fn=mark_cancelled,
        mark_failed_fn=mark_failed,
        upsert_job_record_fn=upsert_job_record,
        notify_job_finished_fn=notify_job_finished,
    )


__all__ = [
    "WorkerExecutionOutcome",
    "build_terminal_result",
    "finalize_execution_result",
    "write_execution_artifacts",
    "write_running_state",
]
