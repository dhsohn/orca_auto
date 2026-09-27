"""``clear_activities``: the ``queue list clear`` payload over the ORCA cleanup."""

from __future__ import annotations

from typing import Any

from orca_auto.orca.engine_runtime import engine_runtime_paths
from orca_auto.orca.run_cleanup import clear_terminal_records

from ._list import resolve_activity_config


def clear_activities(*, config_path: str | None = None) -> dict[str, Any]:
    resolved = resolve_activity_config(config_path)
    root = engine_runtime_paths(resolved)["allowed_root"]
    counts = clear_terminal_records(root)
    return {
        "total_cleared": counts.queue_entries + counts.run_states,
        "cleared": {
            "orca_queue_entries": counts.queue_entries,
            "orca_run_states": counts.run_states,
        },
        "removed_worker_logs": counts.worker_logs,
        "sources": {"orca_config": resolved},
    }
