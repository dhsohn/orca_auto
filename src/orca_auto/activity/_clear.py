"""``clear_activities``: the ``queue list clear`` payload over the ORCA cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orca_auto.orca.run_cleanup import clear_terminal_records


def clear_activities(*, config_path: str, runs_root: Path) -> dict[str, Any]:
    counts = clear_terminal_records(runs_root)
    return {
        "total_cleared": counts.queue_entries + counts.run_states,
        "cleared": {
            "orca_queue_entries": counts.queue_entries,
            "orca_run_states": counts.run_states,
        },
        "removed_worker_logs": counts.worker_logs,
        "sources": {"orca_config": config_path},
    }
