from __future__ import annotations

from typing import Any

from orca_auto.core.activity import ActivitySourceRequest
from orca_auto.core.engine_runtime import engine_runtime_paths
from orca_auto.core.utils import normalize_text
from orca_auto.orca.run_cleanup import clear_terminal_entries

from ._list import resolve_activity_sources


def clear_activities(
    *,
    shared_config: str | None = None,
    orca_config: str | None = None,
) -> dict[str, Any]:
    resolved = resolve_activity_sources(
        ActivitySourceRequest(shared_config=shared_config, orca_config=orca_config)
    )
    config_path = normalize_text(resolved.orca_config)
    queue_count, run_count = (0, 0)
    if config_path:
        root = engine_runtime_paths(config_path)["allowed_root"]
        queue_count, run_count = clear_terminal_entries(root)
    return {
        "total_cleared": queue_count + run_count,
        "cleared": {"orca_queue_entries": queue_count, "orca_run_states": run_count},
        "sources": {"orca_config": config_path},
    }
