from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orca_auto.core.indexing import JobLocationRecord
from orca_auto.orca.job_locations import _runtime_context as runtime_context


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


def test_matching_tracked_job_dirs_matches_artifacts_and_deduplicates(
    tmp_path: Path,
) -> None:
    organized_dir = tmp_path / "organized"
    original_dir = tmp_path / "original"
    organized_dir.mkdir()
    original_dir.mkdir()
    records = (
        _record("job-1", organized_dir, original_dir=original_dir),
        _record("job-duplicate", organized_dir, original_dir=original_dir),
    )

    deps = SimpleNamespace(
        normalize_text=lambda value: str(value or "").strip(),
        list_job_location_records=lambda _index_root: records,
        resolve_record_job_dir=lambda _record: organized_dir.resolve(),
        load_state=lambda _job_dir: {"job_id": "state-job"},
        load_report_json=lambda _job_dir: {"run_id": "report-run"},
        resolve_existing_job_dir=lambda value: (
            Path(value).resolve() if value and Path(value).exists() else None
        ),
    )

    assert runtime_context.matching_tracked_job_dirs(tmp_path, "", deps=deps) == []
    assert runtime_context.matching_tracked_job_dirs(tmp_path, "report-run", deps=deps) == [
        organized_dir.resolve()
    ]
