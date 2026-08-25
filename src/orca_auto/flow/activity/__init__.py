from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.core.statuses import WORKFLOW_TERMINAL_STATUSES

from ..engine_options import WorkflowEngineOptions
from . import _cancel as _activity_cancel
from . import _clear as _activity_clear
from . import _collectors as _activity_collectors
from . import _list as _activity_list
from . import _sources as _activity_sources
from ._model import (
    ActivityCancelRequest,
    ActivityListRequest,
    ActivityRecord,
    ActivitySourceRequest,
    ResolvedActivitySources,
)

_ACTIVITY_CLEARABLE_TERMINAL_STATUSES = WORKFLOW_TERMINAL_STATUSES


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
    return _activity_list.list_activities(
        workflow_root=workflow_root,
        shared_config=shared_config,
        refresh=refresh,
        limit=limit,
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
    )


def clear_activities(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
) -> dict[str, Any]:
    return _activity_clear.clear_activities(
        workflow_root=workflow_root,
        shared_config=shared_config,
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
        clearable_terminal_statuses=_ACTIVITY_CLEARABLE_TERMINAL_STATUSES,
    )


def cancel_activity(
    *,
    target: str,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    orca_repo_root: str | None = None,
) -> dict[str, Any]:
    request = ActivityCancelRequest(
        target=target,
        sources=ActivitySourceRequest(
            workflow_root=workflow_root,
            shared_config=shared_config,
            crest_config=crest_config,
            xtb_config=xtb_config,
            orca_config=orca_config,
        ),
        engine_options=WorkflowEngineOptions.from_values(
            shared_config=shared_config,
            crest_config=crest_config,
            xtb_config=xtb_config,
            orca_config=orca_config,
            orca_repo_root=orca_repo_root,
        ),
    )
    resolved = _activity_sources.resolve_activity_source_request(request.sources)
    record = _activity_cancel.match_activity_record(
        _activity_collectors.collect_activity_records(
            workflow_root=resolved.workflow_root,
            refresh=False,
            crest_config=resolved.crest_config,
            xtb_config=resolved.xtb_config,
            orca_config=resolved.orca_config,
        ),
        request.target,
    )

    if record.kind == "workflow":
        result = _activity_cancel.cancel_workflow_activity(record, resolved, request)
        return _activity_cancel.cancel_activity_payload(
            record,
            result,
            fallback_status="cancelled",
        )

    result = _activity_cancel.cancel_non_workflow_activity(record, resolved, request)
    return _activity_cancel.cancel_activity_payload(record, result, fallback_status="failed")


__all__ = [
    "ActivityCancelRequest",
    "ActivityListRequest",
    "ActivityRecord",
    "ActivitySourceRequest",
    "ResolvedActivitySources",
    "cancel_activity",
    "clear_activities",
    "list_activities",
]
