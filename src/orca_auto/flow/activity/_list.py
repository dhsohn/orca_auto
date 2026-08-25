from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.engine_catalog import activity_engine_entries
from orca_auto.core.utils import normalize_text

from . import _sources
from ._collectors import collect_activity_records
from ._model import ActivityListRequest, ActivitySourceRequest


def list_activities(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    refresh: bool = False,
    limit: int = 0,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
) -> dict[str, Any]:
    request = ActivityListRequest(
        sources=ActivitySourceRequest(
            workflow_root=workflow_root,
            shared_config=shared_config,
            crest_config=crest_config,
            xtb_config=xtb_config,
            orca_config=orca_config,
        ),
        refresh=refresh,
        limit=limit,
    )
    resolved = _sources.resolve_activity_source_request(request.sources)
    records = collect_activity_records(
        workflow_root=resolved.workflow_root,
        refresh=request.refresh,
        crest_config=resolved.crest_config,
        xtb_config=resolved.xtb_config,
        orca_config=resolved.orca_config,
    )
    if request.limit > 0:
        records = records[: request.limit]
    workflow_root_text = normalize_text(resolved.workflow_root)
    return {
        "count": len(records),
        "activities": [record.to_dict() for record in records],
        "sources": {
            "workflow_root": str(Path(workflow_root_text).expanduser().resolve())
            if workflow_root_text
            else "",
            **{
                f"{entry.engine_id}_config": normalize_text(
                    resolved.config_for_engine(entry.engine_id)
                )
                for entry in activity_engine_entries()
            },
        },
    }


__all__ = [
    "list_activities",
]
