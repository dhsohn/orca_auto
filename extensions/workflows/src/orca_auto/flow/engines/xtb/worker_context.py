from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.queue import execution as _queue_execution
from orca_auto.core.queue.engine import execution as _engine_execution
from orca_auto.core.queue.engine.input_snapshot import (
    read_stable_regular_file,
    verify_input_snapshots,
)

from .job_locations import reaction_key_from_job_dir, runtime_roots_for_cfg
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
    execution_snapshot: dict[str, Any] = field(default_factory=dict)


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


def execution_snapshot(entry: Any) -> dict[str, Any]:
    return _engine_execution.entry_metadata_dict(entry, "execution_snapshot")


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
    verify_execution_snapshot: bool = True,
) -> XtbExecutionContext:
    resolved_job_dir = _engine_execution.require_path_within_roots(
        context_deps.job_dir(entry),
        runtime_roots_for_cfg(cfg),
        label="Queue metadata 'job_dir'",
    )
    resolved_selected_xyz = _engine_execution.require_path_within_root(
        context_deps.selected_xyz(entry),
        resolved_job_dir,
        label="Queue metadata 'selected_input_xyz'",
    )
    resolved_job_type = context_deps.job_type(entry)
    resolved_reaction_key = context_deps.reaction_key(entry, resolved_job_dir)
    resolved_input_summary = context_deps.input_summary(entry)
    resource_request = context_deps.entry_resource_request(cfg, entry)
    snapshot = execution_snapshot(entry)
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
        verified_inputs = verify_input_snapshots(resolved_job_dir, input_snapshots)
        if verified_inputs.get("selected") != resolved_selected_xyz:
            raise ValueError("Queued selected xTB input does not match its immutable snapshot")
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
            raise ValueError("Queued xTB manifest payload does not match its immutable snapshot")
        if str(snapshot.get("selected_input_xyz") or "") != str(resolved_selected_xyz):
            raise ValueError("Queued xTB execution snapshot has a mismatched selected input")
        if str(snapshot.get("job_type") or "") != resolved_job_type:
            raise ValueError("Queued xTB execution snapshot has a mismatched job type")
        if str(snapshot.get("reaction_key") or "") != resolved_reaction_key:
            raise ValueError("Queued xTB execution snapshot has a mismatched reaction key")
        if snapshot.get("input_summary") != resolved_input_summary:
            raise ValueError("Queued xTB execution snapshot has a mismatched input summary")
        if snapshot.get("resource_request") != resource_request:
            raise ValueError("Queued xTB execution snapshot has a mismatched resource request")
        secondary_text = str(snapshot.get("secondary_input_xyz") or "").strip()
        if secondary_text:
            if verified_inputs.get("secondary") != Path(secondary_text).expanduser().resolve():
                raise ValueError("Queued secondary xTB input does not match its immutable snapshot")
        elif "secondary" in verified_inputs:
            raise ValueError("Queued xTB execution snapshot has an unexpected secondary input")
        xcontrol = str(manifest_snapshot.get("xcontrol") or "").strip()
        if xcontrol and verified_inputs.get("xcontrol") != Path(xcontrol).expanduser().resolve():
            raise ValueError("Queued xTB xcontrol file does not match its immutable snapshot")
        expected_roles = {"selected", "manifest"}
        if xcontrol:
            expected_roles.add("xcontrol")
        elif "xcontrol" in verified_inputs:
            raise ValueError("Queued xTB execution snapshot has an unexpected xcontrol file")
        if resolved_job_type == "path_search":
            expected_roles.add("secondary")
            if (
                str(resolved_input_summary.get("reactant_xyz") or "") != str(resolved_selected_xyz)
                or str(resolved_input_summary.get("product_xyz") or "") != secondary_text
            ):
                raise ValueError("Queued xTB path-search endpoints do not match input snapshots")
        elif resolved_job_type == "ranking":
            candidate_roles = sorted(
                role for role in verified_inputs if role.startswith("candidate_")
            )
            expected_roles.update(candidate_roles)
            expected_candidates = [resolved_selected_xyz]
            expected_candidates.extend(verified_inputs[role] for role in candidate_roles)
            actual_candidates = [
                Path(str(candidate)).expanduser().resolve()
                for candidate in resolved_input_summary.get("candidate_paths", [])
            ]
            if (
                actual_candidates != expected_candidates
                or int(resolved_input_summary.get("candidate_count", -1))
                != len(expected_candidates)
                or int(resolved_input_summary.get("top_n", -1))
                != int(manifest_snapshot.get("top_n", 3))
            ):
                raise ValueError("Queued xTB ranking candidates do not match input snapshots")
        elif str(resolved_input_summary.get("input_xyz") or "") != str(resolved_selected_xyz):
            raise ValueError("Queued xTB input summary does not match its selected snapshot")
        if set(verified_inputs) != expected_roles:
            raise ValueError("Queued xTB execution snapshot has unexpected input roles")
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
        execution_snapshot=snapshot,
    )


__all__ = [
    "WorkerExecutionHooks",
    "XtbExecutionContext",
    "build_execution_context",
    "default_worker_execution_hooks",
    "execution_snapshot",
    "input_summary",
    "job_dir",
    "job_type",
    "matching_state",
    "reaction_key",
    "selected_xyz",
]
