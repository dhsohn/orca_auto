from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
        organized_output_dir=str(job_dir.resolve()),
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

    def load_organized_ref(path: Path) -> dict[str, Any]:
        return {"run_id": "target-run"} if path == original_dir.resolve() else {}

    deps = SimpleNamespace(
        normalize_text=lambda value: str(value or "").strip(),
        list_job_location_records=lambda _index_root: records,
        resolve_record_job_dir=lambda _record: organized_dir.resolve(),
        load_state=lambda _job_dir: {"job_id": "state-job"},
        load_report_json=lambda _job_dir: {"run_id": "report-run"},
        load_organized_ref=load_organized_ref,
        resolve_existing_job_dir=lambda value: (
            Path(value).resolve() if value and Path(value).exists() else None
        ),
    )

    assert runtime_context.matching_tracked_job_dirs(tmp_path, "", deps=deps) == []
    assert runtime_context.matching_tracked_job_dirs(tmp_path, "target-run", deps=deps) == [
        organized_dir.resolve()
    ]


def test_needs_organized_refresh_when_current_dir_is_missing_or_empty(
    tmp_path: Path,
) -> None:
    organized_dir = tmp_path / "organized"
    current_dir = tmp_path / "missing"
    organized_dir.mkdir()

    assert runtime_context._needs_organized_refresh(
        organized_dir=organized_dir,
        current_dir=current_dir,
        state={},
        report={},
    )
    assert not runtime_context._needs_organized_refresh(
        organized_dir=organized_dir,
        current_dir=organized_dir,
        state={"status": "running"},
        report={},
    )
    assert not runtime_context._needs_organized_refresh(
        organized_dir=None,
        current_dir=None,
        state={},
        report={},
    )
