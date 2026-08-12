from __future__ import annotations

from functools import partial
from typing import Any

from orca_auto.core.engine_catalog import (
    EngineCatalogEntry,
    find_engine_catalog_entry_by_source_id,
)
from orca_auto.core.engines import entry_matches_engine_identity, get_engine_definition
from orca_auto.core.queue import list_queue, request_cancel
from orca_auto.core.queue.generation import queue_entries_same_generation
from orca_auto.core.utils import normalize_text

from ..orchestration import cancel_materialized_workflow
from ..submitters.crest import cancel_target as cancel_crest_target
from ..submitters.orca import cancel_target as cancel_orca_target
from ..submitters.xtb import cancel_target as cancel_xtb_target
from . import _sources
from ._model import ActivityCancelRequest, ActivityRecord, ResolvedActivitySources
from ._queue_records import engine_queue_roots

_CANCEL_ENGINE_TARGETS = {
    "crest": cancel_crest_target,
    "xtb": cancel_xtb_target,
}


def match_activity_record(records: list[ActivityRecord], target: str) -> ActivityRecord:
    normalized_target = normalize_text(target)
    if not normalized_target:
        raise ValueError("Cancel target is empty.")

    exact_matches = [
        record
        for record in records
        if normalized_target in {record.activity_id, record.cancel_target}
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise ValueError(
            f"Ambiguous activity target: {normalized_target}. Matches: "
            + ", ".join(sorted(record.activity_id for record in exact_matches))
        )

    alias_matches = [record for record in records if normalized_target in set(record.aliases)]
    if len(alias_matches) == 1:
        return alias_matches[0]
    if len(alias_matches) > 1:
        raise ValueError(
            f"Ambiguous activity target: {normalized_target}. Matches: "
            + ", ".join(sorted(record.activity_id for record in alias_matches))
        )
    raise LookupError(f"Activity target not found: {normalized_target}")


def cancel_activity_payload(
    record: ActivityRecord,
    result: dict[str, Any],
    *,
    fallback_status: str,
) -> dict[str, Any]:
    return {
        "activity_id": record.activity_id,
        "kind": record.kind,
        "engine": record.engine,
        "source": record.source,
        "label": record.label,
        "status": normalize_text(result.get("status")) or fallback_status,
        "cancel_target": record.cancel_target,
        "result": result,
    }


def cancel_workflow_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
) -> dict[str, Any]:
    return cancel_materialized_workflow(
        target=record.cancel_target,
        workflow_root=resolved.workflow_root or "",
        crest_config=resolved.crest_config,
        xtb_config=resolved.xtb_config,
        orca_config=resolved.orca_config,
        orca_repo_root=request.engine_options.orca.repo_root,
    )


def cancel_workflow_stage_engine_activity(
    entry: EngineCatalogEntry,
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
) -> dict[str, Any]:
    del request
    config_path = normalize_text(resolved.config_for_engine(entry.engine_id))
    if not config_path:
        raise ValueError(
            f"{entry.engine_id}_config is required to cancel {entry.engine_id} activities."
        )
    cancel_target = _CANCEL_ENGINE_TARGETS.get(entry.engine_id)
    if cancel_target is None:
        raise ValueError(f"No cancellation handler for catalog engine: {entry.engine_id}")
    return cancel_target(
        target=record.cancel_target,
        config_path=config_path,
    )


def cancel_orca_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
) -> dict[str, Any]:
    config_path = normalize_text(resolved.orca_config)
    if not config_path:
        raise ValueError("orca_auto_config is required to cancel orca_auto ORCA activities.")
    return cancel_orca_target(
        target=record.cancel_target,
        config_path=config_path,
        repo_root=_sources.discover_orca_repo_root(request.engine_options.orca.repo_root),
    )


def _queue_status_text(entry: Any) -> str:
    return normalize_text(
        getattr(getattr(entry, "status", None), "value", None) or getattr(entry, "status", None)
    ).lower()


def _cancelled_queue_status(entry: Any) -> str:
    status = _queue_status_text(entry)
    if status == "running" and bool(getattr(entry, "cancel_requested", False)):
        return "cancel_requested"
    return status


def _cancelled_same_generation_queue_entry(
    queue_root: Any,
    queued: Any,
    *,
    engine_id: str,
) -> Any | None:
    recovered = [
        current
        for current in list_queue(queue_root)
        if normalize_text(getattr(current, "queue_id", ""))
        == normalize_text(getattr(queued, "queue_id", ""))
        and entry_matches_engine_identity(current, engine_id)
        and queue_entries_same_generation(current, queued)
    ]
    if len(recovered) != 1 or _cancelled_queue_status(recovered[0]) not in {
        "cancelled",
        "cancel_requested",
    }:
        return None
    return recovered[0]


def cancel_standalone_queue_engine_activity(
    entry: EngineCatalogEntry,
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
) -> dict[str, Any]:
    del request
    config_path = normalize_text(resolved.config_for_engine(entry.engine_id))
    if not config_path:
        raise ValueError(
            f"{entry.engine_id}_config is required to cancel {entry.engine_id} activities."
        )

    target = normalize_text(record.cancel_target) or normalize_text(record.activity_id)
    matches: list[tuple[Any, Any]] = []
    for queue_root in engine_queue_roots(config_path, engine=entry.engine_id):
        for queued in list_queue(queue_root):
            if not entry_matches_engine_identity(queued, entry.engine_id):
                continue
            if target not in {
                normalize_text(getattr(queued, "queue_id", "")),
                normalize_text(getattr(queued, "task_id", "")),
            }:
                continue
            matches.append((queue_root, queued))

    if len(matches) != 1:
        reason = "queue_target_not_found" if not matches else "ambiguous_queue_target"
        return {
            "status": "failed",
            "reason": reason,
            "queue_id": target,
        }

    queue_root, queued = matches[0]
    definition = get_engine_definition(entry.engine_id)
    cancellation_hooks = getattr(definition, "cancellation_hooks", None)
    before_pending_cancel = getattr(cancellation_hooks, "before_pending_cancel", None)
    before_pending_cancel_fn = (
        partial(before_pending_cancel, config_path=config_path)
        if before_pending_cancel is not None
        else None
    )
    try:
        updated = request_cancel(
            queue_root,
            normalize_text(getattr(queued, "queue_id", "")),
            accept_entry_fn=lambda current: entry_matches_engine_identity(current, entry.engine_id),
            expected_entry=queued,
            before_pending_cancel_fn=before_pending_cancel_fn,
        )
    except Exception as cancel_exc:
        try:
            updated = _cancelled_same_generation_queue_entry(
                queue_root,
                queued,
                engine_id=entry.engine_id,
            )
        except Exception as reload_exc:
            raise cancel_exc from reload_exc
        if updated is None:
            raise

    if updated is None:
        updated = _cancelled_same_generation_queue_entry(
            queue_root,
            queued,
            engine_id=entry.engine_id,
        )
    if updated is None:
        return {
            "status": "failed",
            "reason": "queue_target_already_terminal",
            "queue_id": normalize_text(getattr(queued, "queue_id", "")),
            "job_id": normalize_text(getattr(queued, "task_id", "")),
        }
    return {
        "status": _cancelled_queue_status(updated),
        "queue_id": normalize_text(getattr(updated, "queue_id", "")),
        "job_id": normalize_text(getattr(updated, "task_id", "")),
    }


def cancel_non_workflow_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
) -> dict[str, Any]:
    entry = find_engine_catalog_entry_by_source_id(record.source)
    if entry is None:
        raise ValueError(f"Unsupported activity source: {record.source}")
    if entry.workflow_stage_role == "workflow-stage":
        return cancel_workflow_stage_engine_activity(
            entry,
            record,
            resolved,
            request,
        )
    if entry.activity_role == "orca-run":
        return cancel_orca_activity(record, resolved, request)
    if entry.activity_role == "engine-queue":
        return cancel_standalone_queue_engine_activity(
            entry,
            record,
            resolved,
            request,
        )
    raise ValueError(f"Unsupported activity role for source {record.source}: {entry.activity_role}")
