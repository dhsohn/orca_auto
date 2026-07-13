from __future__ import annotations

from typing import Any

from orca_auto.core.engine_catalog import (
    EngineCatalogEntry,
    find_engine_catalog_entry_by_source_id,
    get_engine_catalog_entry,
)
from orca_auto.core.utils import normalize_text

from ..orchestration import cancel_materialized_workflow
from ._model import ActivityCancelRequest, ActivityRecord, ResolvedActivitySources


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


def cancel_crest_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
    *,
    deps: Any,
) -> dict[str, Any]:
    return cancel_workflow_stage_engine_activity(
        get_engine_catalog_entry("crest"),
        record,
        resolved,
        request,
        deps=deps,
    )


def cancel_xtb_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
    *,
    deps: Any,
) -> dict[str, Any]:
    return cancel_workflow_stage_engine_activity(
        get_engine_catalog_entry("xtb"),
        record,
        resolved,
        request,
        deps=deps,
    )


def cancel_workflow_stage_engine_activity(
    entry: EngineCatalogEntry,
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
    *,
    deps: Any,
) -> dict[str, Any]:
    del request
    config_path = normalize_text(resolved.config_for_engine(entry.engine_id))
    if not config_path:
        raise ValueError(
            f"{entry.engine_id}_config is required to cancel {entry.engine_id} activities."
        )
    cancel_target = deps.cancel_engine_targets.get(entry.engine_id)
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
    *,
    deps: Any,
) -> dict[str, Any]:
    config_path = normalize_text(resolved.orca_config)
    if not config_path:
        raise ValueError("orca_auto_config is required to cancel orca_auto ORCA activities.")
    return deps.cancel_orca_target(
        target=record.cancel_target,
        config_path=config_path,
        repo_root=deps._discover_orca_repo_root(request.engine_options.orca.repo_root),
    )


def cancel_non_workflow_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
    *,
    deps: Any,
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
            deps=deps,
        )
    if entry.engine_id == "orca":
        return cancel_orca_activity(record, resolved, request, deps=deps)
    raise ValueError(f"Unsupported activity source: {record.source}")
