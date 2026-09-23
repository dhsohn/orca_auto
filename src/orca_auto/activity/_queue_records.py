from __future__ import annotations

from orca_auto.core.activity import ActivityListRequest, ActivityRecord, ResolvedActivitySources
from orca_auto.core.engine_runtime import engine_runtime_paths
from orca_auto.core.utils import normalize_text

from . import _orca


def collect_orca_activity(
    resolved: ResolvedActivitySources,
    request: ActivityListRequest,
) -> list[ActivityRecord]:
    config_path = normalize_text(resolved.orca_config)
    if not config_path:
        return []
    if request.indexed:
        from ._orca_index import query_records

        root = engine_runtime_paths(config_path, engine="orca")["allowed_root"]
        indexed = query_records(root, request)
        if not request.refresh:
            return indexed
        # Explicit unindexed discoveries remain local to this one query.
    return _orca.orca_records(
        config_path=config_path,
        refresh=request.refresh,
    )
