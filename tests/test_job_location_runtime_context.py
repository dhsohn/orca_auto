from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.indexing import JobLocationRecord
from orca_auto.orca.job_locations import _tracking as tracking


def _record(job_id: str, job_dir: Path, *, original_dir: Path | None = None) -> JobLocationRecord:
    return JobLocationRecord(
        job_id=job_id,
        app_name="orca_auto_orca",
        job_type="orca_opt",
        status="completed",
        original_run_dir=str((original_dir or job_dir).resolve()),
        molecule_key="sample",
        selected_input_xyz="",
        latest_known_path=str(job_dir.resolve()),
        resource_request={},
        resource_actual={},
    )


def test_matching_tracked_job_dirs_matches_state_and_deduplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organized_dir = tmp_path / "organized"
    original_dir = tmp_path / "original"
    organized_dir.mkdir()
    original_dir.mkdir()
    records = (
        _record("job-1", organized_dir, original_dir=original_dir),
        _record("job-duplicate", organized_dir, original_dir=original_dir),
    )

    monkeypatch.setattr(tracking, "list_job_location_records", lambda _index_root: records)
    monkeypatch.setattr(tracking, "resolve_record_job_dir", lambda _record: organized_dir.resolve())
    monkeypatch.setattr(tracking, "load_state", lambda _job_dir: {"job_id": "state-job"})

    assert tracking.matching_tracked_job_dirs(tmp_path, "") == []
    assert tracking.matching_tracked_job_dirs(tmp_path, "state-job") == [organized_dir.resolve()]
    assert tracking.matching_tracked_job_dirs(tmp_path, "report-run") == []
