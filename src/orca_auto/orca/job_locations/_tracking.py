from __future__ import annotations

from pathlib import Path

from ..state import load_state
from ._records import list_job_location_records, resolve_record_job_dir
from ._utils import normalize_text


def matching_tracked_job_dirs(index_root: str | Path, target: str) -> list[Path]:
    target_text = normalize_text(target)
    if not target_text:
        return []

    candidates: list[Path] = []
    seen: set[Path] = set()
    for record in list_job_location_records(index_root):
        job_dir = resolve_record_job_dir(record)
        if job_dir is None or job_dir in seen:
            continue

        state = load_state(job_dir)
        state = state if isinstance(state, dict) else {}

        lookup_values = (
            record.job_id,
            state.get("job_id"),
            state.get("run_id"),
        )
        if any(normalize_text(value) == target_text for value in lookup_values):
            seen.add(job_dir)
            candidates.append(job_dir)

    return candidates


__all__ = ["matching_tracked_job_dirs"]
