from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TrackedJobDirDeps:
    normalize_text: Callable[[Any], str]
    list_job_location_records: Callable[[str | Path], list[Any]]
    resolve_record_job_dir: Callable[[Any], Path | None]
    load_state: Callable[[Path], Any]
    load_report_json: Callable[[Path], Any]
    resolve_existing_job_dir: Callable[[Any], Path | None]


def matching_tracked_job_dirs(
    index_root: str | Path,
    target: str,
    *,
    deps: TrackedJobDirDeps,
) -> list[Path]:
    target_text = deps.normalize_text(target)
    if not target_text:
        return []

    candidates: list[Path] = []
    seen: set[Path] = set()
    for record in deps.list_job_location_records(index_root):
        job_dir = deps.resolve_record_job_dir(record)
        if job_dir is None or job_dir in seen:
            continue

        state = deps.load_state(job_dir)
        report = deps.load_report_json(job_dir)
        state = state if isinstance(state, dict) else {}
        report = report if isinstance(report, dict) else {}

        lookup_values = (
            record.job_id,
            report.get("job_id"),
            state.get("job_id"),
            report.get("run_id"),
            state.get("run_id"),
        )
        if any(deps.normalize_text(value) == target_text for value in lookup_values):
            seen.add(job_dir)
            candidates.append(job_dir)

    return candidates


__all__ = ["TrackedJobDirDeps", "matching_tracked_job_dirs"]
