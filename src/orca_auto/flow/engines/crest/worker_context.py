from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.engine.input_snapshot import (
    read_stable_regular_file,
    verify_input_snapshots,
)
from orca_auto.flow.engines.crest import artifacts as _queue_artifacts

from .job_locations import molecule_key_from_selected_xyz, runtime_roots_for_cfg


@dataclass(frozen=True)
class ExecutionContext:
    entry: Any
    job_dir: Path
    selected_xyz: Path
    molecule_key: str
    mode: str
    resource_request: dict[str, int]
    execution_snapshot: dict[str, Any] = field(default_factory=dict)


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
    verify_execution_snapshot: bool = True,
) -> ExecutionContext:
    job_dir = _engine_execution.require_path_within_roots(
        _engine_execution.entry_metadata_resolved_path(entry, "job_dir"),
        runtime_roots_for_cfg(cfg),
        label="Queue metadata 'job_dir'",
    )
    selected_xyz = _engine_execution.require_path_within_root(
        _engine_execution.entry_metadata_resolved_path(entry, "selected_input_xyz"),
        job_dir,
        label="Queue metadata 'selected_input_xyz'",
    )
    resource_request = _queue_artifacts.entry_resource_request(cfg, entry)
    snapshot = _engine_execution.entry_metadata_dict(entry, "execution_snapshot")
    if verify_execution_snapshot and snapshot.get("version") != 1:
        raise ValueError("Queue metadata 'execution_snapshot' has an unsupported version")
    manifest_snapshot = snapshot.get("manifest")
    input_snapshots = snapshot.get("input_snapshots")
    if verify_execution_snapshot and (
        not isinstance(manifest_snapshot, dict) or not isinstance(input_snapshots, dict)
    ):
        raise ValueError("Queue metadata 'execution_snapshot' is incomplete")
    if verify_execution_snapshot:
        assert isinstance(manifest_snapshot, dict)
        assert isinstance(input_snapshots, dict)
        verified_inputs = verify_input_snapshots(job_dir, input_snapshots)
        if verified_inputs.get("selected") != selected_xyz:
            raise ValueError("Queued selected CREST input does not match its immutable snapshot")
        manifest_path = verified_inputs.get("manifest")
        if (
            manifest_path is None
            or str(snapshot.get("manifest_path") or "") != str(manifest_path)
            or read_stable_regular_file(manifest_path, require_single_link=True)
            != json.dumps(
                manifest_snapshot,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ):
            raise ValueError("Queued CREST manifest payload does not match its immutable snapshot")
    resolved_mode = mode(entry)
    resolved_molecule_key = molecule_key_resolver(entry, selected_xyz, job_dir)
    if verify_execution_snapshot and str(snapshot.get("selected_input_xyz") or "") != str(
        selected_xyz
    ):
        raise ValueError("Queued CREST execution snapshot has a mismatched selected input")
    if verify_execution_snapshot and str(snapshot.get("mode") or "") != resolved_mode:
        raise ValueError("Queued CREST execution snapshot has a mismatched mode")
    if (
        verify_execution_snapshot
        and str(snapshot.get("molecule_key") or "") != resolved_molecule_key
    ):
        raise ValueError("Queued CREST execution snapshot has a mismatched molecule key")
    if verify_execution_snapshot and snapshot.get("resource_request") != resource_request:
        raise ValueError("Queued CREST execution snapshot has a mismatched resource request")
    return ExecutionContext(
        entry=entry,
        job_dir=job_dir,
        selected_xyz=selected_xyz,
        molecule_key=resolved_molecule_key,
        mode=resolved_mode,
        resource_request=resource_request,
        execution_snapshot=snapshot,
    )


__all__ = [
    "ExecutionContext",
    "build_execution_context",
    "mode",
    "molecule_key",
]
