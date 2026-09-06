from __future__ import annotations

import os
from pathlib import Path

from orca_auto.core.indexing.location import JobLocationRecord
from orca_auto.core.indexing.store import (
    JOB_LOCATION_INDEX_FILE_NAME,
    list_job_locations,
    upsert_job_location,
)


def _record(job_id: str, *, status: str = "queued") -> JobLocationRecord:
    return JobLocationRecord(
        job_id=job_id,
        app_name="orca",
        job_type="sp",
        status=status,
        original_run_dir=f"/runs/{job_id}",
        resource_request={"max_cores": 4},
    )


def _identity(path: Path) -> tuple[int, int]:
    status = os.stat(path)
    return status.st_ino, status.st_mtime_ns


def test_upserting_an_identical_row_does_not_rewrite_the_index(tmp_path: Path) -> None:
    index = tmp_path / JOB_LOCATION_INDEX_FILE_NAME
    upsert_job_location(tmp_path, _record("a"))
    upsert_job_location(tmp_path, _record("b"))
    before = _identity(index)
    text_before = index.read_text(encoding="utf-8")

    returned = upsert_job_location(tmp_path, _record("a"))

    assert returned == _record("a")
    assert _identity(index) == before
    assert index.read_text(encoding="utf-8") == text_before
    assert [row.job_id for row in list_job_locations(tmp_path)] == ["a", "b"]


def test_upserting_a_changed_or_new_row_still_rewrites_the_index(tmp_path: Path) -> None:
    index = tmp_path / JOB_LOCATION_INDEX_FILE_NAME
    upsert_job_location(tmp_path, _record("a"))
    before = _identity(index)

    upsert_job_location(tmp_path, _record("a", status="completed"))
    changed = _identity(index)
    assert changed != before
    assert [(row.job_id, row.status) for row in list_job_locations(tmp_path)] == [
        ("a", "completed")
    ]

    upsert_job_location(tmp_path, _record("c"))
    assert _identity(index) != changed
    assert [row.job_id for row in list_job_locations(tmp_path)] == ["a", "c"]


def test_unchanged_detection_compares_the_normalized_row(tmp_path: Path) -> None:
    index = tmp_path / JOB_LOCATION_INDEX_FILE_NAME
    upsert_job_location(tmp_path, _record("a"))
    before = _identity(index)

    # Whitespace and resource-value coercion are normalized before comparison,
    # so a row that only differs before normalization is still "unchanged".
    denormalized = JobLocationRecord(
        job_id=" a ",
        app_name="orca",
        job_type="sp",
        status="queued",
        original_run_dir="/runs/a",
        resource_request={"max_cores": "4"},  # type: ignore[dict-item]
    )
    upsert_job_location(tmp_path, denormalized)

    assert _identity(index) == before
