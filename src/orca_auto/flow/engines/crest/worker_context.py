from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.flow.engines.crest import artifacts as _queue_artifacts

from .job_locations import molecule_key_from_selected_xyz


@dataclass(frozen=True)
class ExecutionContext:
    entry: Any
    job_dir: Path
    selected_xyz: Path
    molecule_key: str
    mode: str
    resource_request: dict[str, int]


def molecule_key(entry: Any, selected_xyz: Path, job_dir: Path) -> str:
    raw = _engine_execution.entry_metadata_text(entry, "molecule_key")
    if raw:
        return raw
    return molecule_key_from_selected_xyz(str(selected_xyz), job_dir)


def mode(entry: Any) -> str:
    metadata = getattr(entry, "metadata", {})
    getter = getattr(metadata, "get", None)
    if getter is None:
        return "standard"
    return str(getter("mode", "standard"))


def build_execution_context(
    cfg: Any,
    entry: Any,
    *,
    molecule_key_resolver: Callable[[Any, Path, Path], str],
) -> ExecutionContext:
    job_dir = _engine_execution.entry_metadata_resolved_path(entry, "job_dir")
    selected_xyz = _engine_execution.entry_metadata_resolved_path(entry, "selected_input_xyz")
    return ExecutionContext(
        entry=entry,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        molecule_key=molecule_key_resolver(entry, selected_xyz, job_dir),
        mode=mode(entry),
        resource_request=_queue_artifacts.entry_resource_request(cfg, entry),
    )


__all__ = [
    "ExecutionContext",
    "build_execution_context",
    "mode",
    "molecule_key",
]
