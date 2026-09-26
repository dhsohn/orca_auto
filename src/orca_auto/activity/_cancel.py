from __future__ import annotations

from typing import Any

from orca_auto.activity.model import (
    ActivityCancelRequest,
    ActivityRecord,
    ResolvedActivitySources,
    path_aliases,
    sort_key,
)
from orca_auto.core.statuses import is_queue_active_status
from orca_auto.core.utils import normalize_text
from orca_auto.orca.direct_cancel import cancel_target as cancel_orca_target


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

    # Every generation of a directory shares its path aliases. As in the queue
    # adapter's find_entry_by_target, the active generation takes precedence and
    # a retry with none observes the newest terminal row. Matches spanning
    # several or unknown directories stay ambiguous.
    alias_matches = [record for record in records if normalized_target in set(record.aliases)]
    directories = {
        path_aliases(normalize_text(record.metadata.get("reaction_dir")))[:1]
        for record in alias_matches
    }
    if len(alias_matches) > 1 and (len(directories) > 1 or () in directories):
        raise ValueError(
            f"Ambiguous activity target: {normalized_target}. Matches: "
            + ", ".join(sorted(record.activity_id for record in alias_matches))
        )
    active_matches = [record for record in alias_matches if is_queue_active_status(record.status)]
    if len(active_matches) > 1:
        raise ValueError(
            f"Ambiguous activity target: {normalized_target}. Matches: "
            + ", ".join(sorted(record.activity_id for record in active_matches))
        )
    if active_matches:
        return active_matches[0]
    if alias_matches:
        return max(alias_matches, key=sort_key)
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


def cancel_orca_activity(
    record: ActivityRecord,
    resolved: ResolvedActivitySources,
    request: ActivityCancelRequest,
) -> dict[str, Any]:
    if record.engine != "orca" or record.source != "orca_auto_orca":
        raise ValueError(f"Unsupported activity source: {record.source}")
    config_path = normalize_text(resolved.orca_config)
    if not config_path:
        raise ValueError("orca_auto_config is required to cancel orca_auto ORCA activities.")
    return cancel_orca_target(
        target=record.cancel_target,
        config_path=config_path,
    )
