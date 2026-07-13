from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.engine_catalog import (
    activity_engine_entries,
    get_engine_catalog_entry,
    workflow_stage_engine_entries,
)
from orca_auto.core.queue.internal_engine import own_engine_accept_entry
from orca_auto.core.utils import normalize_text

from ._model import ActivitySourceRequest, ResolvedActivitySources


@dataclass(frozen=True)
class ActivityClearDeps:
    _resolved_activity_sources_for_request: Callable[
        [ActivitySourceRequest], ResolvedActivitySources
    ]
    clear_terminal_workflow_registry: Callable[..., int]
    clear_queue_terminal: Callable[..., int]
    _engine_queue_roots: Callable[..., tuple[Path, ...]]
    engine_runtime_paths: Callable[..., dict[str, Path]]


_ENGINE_QUEUE_CLEAR_SOURCES = tuple(
    (entry.engine_id, f"{entry.engine_id}_queue_entries")
    for entry in (
        *workflow_stage_engine_entries(),
        *(
            candidate
            for candidate in activity_engine_entries()
            if candidate.activity_role == "engine-queue"
            and candidate.workflow_stage_role != "workflow-stage"
        ),
    )
)


def _clear_counts() -> dict[str, int]:
    cleared = {"workflows": 0}
    for entry in activity_engine_entries():
        cleared[f"{entry.engine_id}_queue_entries"] = 0
    cleared["orca_run_states"] = 0
    return cleared


def _clear_engine_queue(
    resolved: ResolvedActivitySources,
    *,
    engine: str,
    deps: ActivityClearDeps,
) -> int:
    config_path = normalize_text(resolved.config_for_engine(engine))
    if not config_path:
        return 0
    entry = get_engine_catalog_entry(engine)
    if entry.workflow_stage_role == "workflow-stage" and not normalize_text(resolved.workflow_root):
        return 0
    return sum(
        deps.clear_queue_terminal(
            root,
            select_entry_fn=own_engine_accept_entry(engine),
        )
        for root in deps._engine_queue_roots(config_path, engine=engine)
    )


def _clear_workflow_registry(
    resolved: ResolvedActivitySources,
    *,
    clearable_terminal_statuses: Iterable[str],
    deps: ActivityClearDeps,
) -> int:
    if not normalize_text(resolved.workflow_root):
        return 0
    return deps.clear_terminal_workflow_registry(
        str(resolved.workflow_root),
        statuses=clearable_terminal_statuses,
    )


def _clear_engine_queues(
    resolved: ResolvedActivitySources,
    *,
    deps: ActivityClearDeps,
) -> dict[str, int]:
    cleared = {cleared_key: 0 for _, cleared_key in _ENGINE_QUEUE_CLEAR_SOURCES}
    for engine, cleared_key in _ENGINE_QUEUE_CLEAR_SOURCES:
        cleared[cleared_key] += _clear_engine_queue(
            resolved,
            engine=engine,
            deps=deps,
        )
    return cleared


def _clear_orca_terminal_entries(
    resolved: ResolvedActivitySources,
    *,
    deps: ActivityClearDeps,
) -> tuple[int, int]:
    if not normalize_text(resolved.orca_config):
        return 0, 0
    from orca_auto.orca.run_cleanup import (
        clear_terminal_entries as clear_orca_terminal_entries,
    )

    allowed_root = deps.engine_runtime_paths(str(resolved.orca_config), engine="orca")[
        "allowed_root"
    ]
    return clear_orca_terminal_entries(allowed_root)


def _clear_sources_payload(resolved: ResolvedActivitySources) -> dict[str, str]:
    workflow_root_text = normalize_text(resolved.workflow_root)
    return {
        "workflow_root": str(Path(workflow_root_text).expanduser().resolve())
        if workflow_root_text
        else "",
        **{
            f"{entry.engine_id}_config": normalize_text(resolved.config_for_engine(entry.engine_id))
            for entry in activity_engine_entries()
        },
    }


def clear_activities(
    *,
    workflow_root: str | Path | None = None,
    shared_config: str | None = None,
    crest_config: str | None = None,
    xtb_config: str | None = None,
    orca_config: str | None = None,
    clearable_terminal_statuses: Iterable[str],
    deps: ActivityClearDeps,
) -> dict[str, Any]:
    source_request = ActivitySourceRequest(
        workflow_root=workflow_root,
        shared_config=shared_config,
        crest_config=crest_config,
        xtb_config=xtb_config,
        orca_config=orca_config,
    )
    resolved = deps._resolved_activity_sources_for_request(source_request)

    cleared = _clear_counts()
    cleared["workflows"] = _clear_workflow_registry(
        resolved,
        clearable_terminal_statuses=clearable_terminal_statuses,
        deps=deps,
    )
    for key, count in _clear_engine_queues(resolved, deps=deps).items():
        cleared[key] += count
    queue_count, run_count = _clear_orca_terminal_entries(resolved, deps=deps)
    cleared["orca_queue_entries"] += queue_count
    cleared["orca_run_states"] += run_count

    return {
        "total_cleared": sum(int(value) for value in cleared.values()),
        "cleared": cleared,
        "sources": _clear_sources_payload(resolved),
    }
