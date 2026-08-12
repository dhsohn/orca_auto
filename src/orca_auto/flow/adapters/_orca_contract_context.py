from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.utils.coercion import normalize_text
from orca_auto.orca.job_locations._generation import current_generation_payloads

from . import _orca_local_lookup as _local_lookup
from . import _orca_path_helpers as _path_helpers
from . import _orca_tracking as _tracking


@dataclass(frozen=True)
class LoadRequest:
    target: str
    queue_id: str
    run_id: str
    reaction_dir: str


@dataclass(frozen=True)
class LoadRoots:
    allowed: Path | None


@dataclass
class LoaderContext:
    tracked_artifact_dir: Path | None
    tracked_dir: Path | None
    tracked_record: Any
    state: dict[str, Any]
    report: dict[str, Any]
    queue_entry: dict[str, Any] | None
    current_dir: Path | None = None
    resolved_run_id: str = ""


def resolve_roots(orca_allowed_root: str | Path | None) -> LoadRoots:
    allowed = Path(orca_allowed_root).expanduser().resolve() if orca_allowed_root else None
    return LoadRoots(allowed=allowed)


def load_context(request: LoadRequest, roots: LoadRoots) -> LoaderContext:
    runtime_context = _tracking.tracked_runtime_context_impl(
        index_root=roots.allowed,
        target=request.target,
        queue_id=request.queue_id,
        run_id=request.run_id,
        reaction_dir=request.reaction_dir,
    )
    if runtime_context is not None:
        context = context_from_runtime(runtime_context)
    else:
        tracked_dir, record, state, report = _tracking.tracked_artifact_context_impl(
            index_root=roots.allowed,
            targets=(request.target, request.run_id, request.reaction_dir),
        )
        context = LoaderContext(tracked_dir, tracked_dir, record, dict(state), dict(report), None)
    if context.queue_entry is None:
        context.queue_entry = explicit_queue_entry(request, roots)
    return context


def context_from_runtime(runtime_context: tuple[Any, ...]) -> LoaderContext:
    tracked_artifact_dir, tracked_dir, record, state, report, queue_entry = runtime_context
    return LoaderContext(
        tracked_artifact_dir=tracked_artifact_dir,
        tracked_dir=tracked_dir,
        tracked_record=record,
        state=dict(state),
        report=dict(report),
        queue_entry=queue_entry,
    )


def explicit_queue_entry(request: LoadRequest, roots: LoadRoots) -> dict[str, Any] | None:
    if not (
        normalize_text(request.queue_id)
        or normalize_text(request.run_id)
        or normalize_text(request.reaction_dir)
    ):
        return None
    return _local_lookup.find_queue_entry_impl(
        allowed_root=roots.allowed,
        queue_id=request.queue_id,
        run_id=request.run_id,
        reaction_dir=request.reaction_dir,
    )


def set_current_dir(request: LoadRequest, context: LoaderContext) -> None:
    context.current_dir = (
        context.tracked_dir
        or context.tracked_artifact_dir
        or _path_helpers.direct_dir_target_impl(request.target)
        or _path_helpers.resolve_candidate_path_impl(request.reaction_dir)
        or _path_helpers.resolve_candidate_path_impl(
            _local_lookup.queue_entry_metadata_value_impl(context.queue_entry, "reaction_dir")
        )
    )


def load_context_payloads(context: LoaderContext) -> None:
    raw_state, raw_report = current_generation_payloads(
        context.queue_entry,
        context.state,
        context.report,
    )
    # Validate every identity alias while the normalized outer ``job`` and
    # inner ``engine_payload`` mappings are both still present. Flattening
    # first would discard a conflicting outer identity when the inner payload
    # already supplied the same queue-facing ``job_id`` for both artifacts.
    context.state = _flatten_orca_engine_payload(raw_state)
    context.report = _flatten_orca_engine_payload(raw_report)


def resolve_run_id(request: LoadRequest, context: LoaderContext) -> str:
    queue = context.queue_entry or {}
    return (
        normalize_text(request.run_id)
        or normalize_text(context.state.get("run_id"))
        or normalize_text(context.report.get("run_id"))
        or normalize_text(_local_lookup.queue_entry_metadata_value_impl(queue, "run_id"))
    )


def _flatten_orca_engine_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    if int(payload.get("schema_version", 0) or 0) != 1:
        return {}
    if str(payload.get("engine", "")).strip() != "orca":
        return {}
    job = _mapping(payload.get("job"))
    status = _mapping(payload.get("status"))
    input_payload = _mapping(payload.get("input"))
    timestamps = _mapping(payload.get("timestamps"))
    artifacts = _mapping(payload.get("artifacts"))
    engine_payload = _mapping(payload.get("engine_payload"))
    flattened = dict(engine_payload)
    flattened.setdefault("job_id", str(job.get("id", "")).strip())
    flattened.setdefault("reaction_dir", str(job.get("dir", "")).strip())
    flattened.setdefault("selected_inp", str(input_payload.get("primary_path", "")).strip())
    flattened.setdefault(
        "selected_input_xyz",
        str(input_payload.get("selected_xyz_path", "")).strip(),
    )
    flattened.setdefault("status", str(status.get("state", "")).strip())
    flattened.setdefault("started_at", str(timestamps.get("started_at", "")).strip())
    flattened.setdefault("updated_at", str(timestamps.get("updated_at", "")).strip())
    final_result = flattened.get("final_result")
    if isinstance(final_result, dict):
        final_result.setdefault("last_out_path", str(artifacts.get("last_out_path", "")).strip())
        final_result.setdefault("completed_at", str(timestamps.get("finished_at", "")).strip())
    return flattened


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "LoadRequest",
    "LoadRoots",
    "LoaderContext",
    "load_context",
    "load_context_payloads",
    "resolve_roots",
    "resolve_run_id",
    "set_current_dir",
]
