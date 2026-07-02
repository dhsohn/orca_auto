from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue import engine_execution as _engine_execution
from orca_auto.core.queue import execution as _queue_execution

from .job_locations import reaction_key_from_job_dir
from .state import load_state, state_matches_job


@dataclass(frozen=True)
class XtbExecutionContext:
    entry: Any
    job_dir: Path
    selected_xyz: Path
    job_type: str
    reaction_key: str
    input_summary: dict[str, Any]
    resource_request: dict[str, int]
    previous_state: dict[str, Any]
    resumed: bool


@dataclass(frozen=True)
class WorkerExecutionHooks:
    job_dir: Callable[[Any], Path]
    selected_xyz: Callable[[Any], Path]
    job_type: Callable[[Any], str]
    reaction_key: Callable[[Any, Path], str]
    input_summary: Callable[[Any], dict[str, Any]]
    matching_state: Callable[..., dict[str, Any]]


def job_dir(entry: Any) -> Path:
    return _engine_execution.entry_metadata_resolved_path(entry, "job_dir")


def selected_xyz(entry: Any) -> Path:
    return _engine_execution.entry_metadata_resolved_path(entry, "selected_input_xyz")


def job_type(entry: Any) -> str:
    value = _engine_execution.entry_metadata_text(entry, "job_type").lower()
    return value or "path_search"


def reaction_key(entry: Any, job_dir: Path) -> str:
    value = _engine_execution.entry_metadata_text(entry, "reaction_key")
    return value or reaction_key_from_job_dir(job_dir)


def input_summary(entry: Any) -> dict[str, Any]:
    return _engine_execution.entry_metadata_dict(entry, "input_summary")


def matching_state(
    _entry: Any,
    *,
    job_dir: Path,
    selected_xyz: Path,
    job_type: str,
    reaction_key: str,
) -> dict[str, Any]:
    return _queue_execution.load_matching_state(
        job_dir,
        load_state_fn=load_state,
        state_matches_job_fn=state_matches_job,
        match_kwargs={
            "selected_input_xyz": str(selected_xyz),
            "job_type": job_type,
            "reaction_key": reaction_key,
        },
    )


def default_worker_execution_hooks() -> WorkerExecutionHooks:
    return WorkerExecutionHooks(
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        job_type=job_type,
        reaction_key=reaction_key,
        input_summary=input_summary,
        matching_state=matching_state,
    )


def build_execution_context(
    cfg: Any,
    entry: Any,
    *,
    context_deps: Any,
) -> XtbExecutionContext:
    resolved_job_dir = context_deps.job_dir(entry)
    resolved_selected_xyz = context_deps.selected_xyz(entry)
    resolved_job_type = context_deps.job_type(entry)
    resolved_reaction_key = context_deps.reaction_key(entry, resolved_job_dir)
    resolved_input_summary = context_deps.input_summary(entry)
    resource_request = context_deps.entry_resource_request(cfg, entry)
    previous_state = context_deps.matching_state(
        entry,
        job_dir=resolved_job_dir,
        selected_xyz=resolved_selected_xyz,
        job_type=resolved_job_type,
        reaction_key=resolved_reaction_key,
    )
    resumed = _engine_execution.is_resumed_state(
        previous_state,
        is_recovery_pending_fn=context_deps.is_recovery_pending,
    )
    return XtbExecutionContext(
        entry=entry,
        job_dir=resolved_job_dir,
        selected_xyz=resolved_selected_xyz,
        job_type=resolved_job_type,
        reaction_key=resolved_reaction_key,
        input_summary=resolved_input_summary,
        resource_request=resource_request,
        previous_state=previous_state,
        resumed=resumed,
    )


__all__ = [
    "WorkerExecutionHooks",
    "XtbExecutionContext",
    "build_execution_context",
    "default_worker_execution_hooks",
    "input_summary",
    "job_dir",
    "job_type",
    "matching_state",
    "reaction_key",
    "selected_xyz",
]
