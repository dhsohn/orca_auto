from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.core.indexing import store as indexing_store
from orca_auto.core.indexing.location import JobLocationRecord
from orca_auto.core.indexing.store import (
    JOB_LOCATION_INDEX_FILE_NAME,
    JobLocationIndexCorruptError,
    _normalize_resource_payload,
    _resolve_candidate_path,
    get_job_location,
    list_job_locations,
    resolve_job_location,
    upsert_job_location,
)


def _record(
    job_id: str,
    *,
    app_name: str = "app",
    job_type: str = "type",
    status: str = "queued",
    original_run_dir: str = "",
    molecule_key: str = "",
    selected_input_xyz: str = "",
    latest_known_path: str = "",
) -> JobLocationRecord:
    return JobLocationRecord(
        job_id=job_id,
        app_name=app_name,
        job_type=job_type,
        status=status,
        original_run_dir=original_run_dir,
        molecule_key=molecule_key,
        selected_input_xyz=selected_input_xyz,
        latest_known_path=latest_known_path,
    )


def _index_path(root: Path) -> Path:
    return root / JOB_LOCATION_INDEX_FILE_NAME


def test_list_job_locations_missing_index_returns_empty_list(tmp_path: Path) -> None:
    assert list_job_locations(tmp_path) == []
    assert get_job_location(tmp_path, "missing") is None


def test_list_job_locations_invalid_json_raises_corrupt_error(tmp_path: Path) -> None:
    corrupt_text = "{not valid json"
    _index_path(tmp_path).write_text(corrupt_text, encoding="utf-8")

    with pytest.raises(JobLocationIndexCorruptError):
        list_job_locations(tmp_path)
    with pytest.raises(JobLocationIndexCorruptError):
        resolve_job_location(tmp_path, "anything")
    with pytest.raises(JobLocationIndexCorruptError):
        upsert_job_location(tmp_path, _record("job-1"))
    assert _index_path(tmp_path).read_text(encoding="utf-8") == corrupt_text


def test_list_job_locations_non_list_json_raises_corrupt_error(tmp_path: Path) -> None:
    corrupt_text = '{"job_id": "job-1"}'
    _index_path(tmp_path).write_text(corrupt_text, encoding="utf-8")

    with pytest.raises(JobLocationIndexCorruptError):
        list_job_locations(tmp_path)
    with pytest.raises(JobLocationIndexCorruptError):
        resolve_job_location(tmp_path, "job-1")
    with pytest.raises(JobLocationIndexCorruptError):
        upsert_job_location(tmp_path, _record("job-1"))
    assert _index_path(tmp_path).read_text(encoding="utf-8") == corrupt_text


def test_get_job_location_blank_id_returns_none(tmp_path: Path) -> None:
    assert get_job_location(tmp_path, "   ") is None


def test_resolve_job_location_blank_lookup_returns_none(tmp_path: Path) -> None:
    assert resolve_job_location(tmp_path, " \t ") is None


def test_resolve_job_location_non_path_miss_returns_none(tmp_path: Path) -> None:
    record = _record("job-789", original_run_dir=str(tmp_path / "runs" / "job-789"))
    upsert_job_location(tmp_path, record)

    assert resolve_job_location(tmp_path, "missing-job-or-path") is None


def test_resolve_job_location_returns_none_when_candidate_path_is_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record("job-789", original_run_dir=str(tmp_path / "runs" / "job-789"))
    upsert_job_location(tmp_path, record)

    def fake_resolve_candidate_path(path_text: str) -> Path | None:
        assert path_text == "nonblank-lookup"
        return None

    monkeypatch.setattr(indexing_store, "_resolve_candidate_path", fake_resolve_candidate_path)

    assert resolve_job_location(tmp_path, "nonblank-lookup") is None


def test_normalize_resource_payload_handles_non_dict_and_edge_values() -> None:
    assert _normalize_resource_payload(None) == {}
    assert _normalize_resource_payload([]) == {}
    assert _normalize_resource_payload(
        {
            "": 1,
            "   ": 2,
            "gpu": None,
            "threads": "8",
            "nodes": 3.2,
            "cores": True,
            "skip": "not-an-int",
        }
    ) == {
        "gpu": 0,
        "threads": 8,
        "nodes": 3,
        "cores": 1,
        "skip": 0,
    }


def test_resolve_candidate_path_returns_none_when_resolve_raises_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingPath:
        def __init__(self, value: str) -> None:
            self.value = value

        def expanduser(self) -> ExplodingPath:
            return self

        def resolve(self) -> Path:
            raise OSError("cannot resolve")

    monkeypatch.setattr(indexing_store, "Path", ExplodingPath)

    assert _resolve_candidate_path("  /tmp/example  ") is None


def test_upsert_replaces_existing_record_by_job_id(tmp_path: Path) -> None:
    initial = _record(
        " job-123 ",
        app_name="alpha",
        status="queued",
        original_run_dir=str(tmp_path / "runs" / "first"),
    )
    replacement = _record(
        "job-123",
        app_name="beta",
        job_type="analysis",
        status="complete",
        original_run_dir=str(tmp_path / "runs" / "second"),
        molecule_key="mol-7",
    )

    upsert_job_location(tmp_path, initial)
    result = upsert_job_location(tmp_path, replacement)

    assert result == replacement
    assert get_job_location(tmp_path, "job-123") == replacement
    assert list_job_locations(tmp_path) == [replacement]
    stored = json.loads(_index_path(tmp_path).read_text(encoding="utf-8"))
    assert len(stored) == 1
    assert stored[0]["job_id"] == "job-123"
    assert stored[0]["app_name"] == "beta"


def test_resolve_job_location_prefers_job_id_over_path_match(tmp_path: Path) -> None:
    shared_path = tmp_path / "shared"
    shared_path.mkdir()

    job_id_match = _record(
        str(shared_path),
        app_name="job-id-match",
        original_run_dir=str(tmp_path / "job-id-run"),
    )
    path_match = _record(
        "other-job",
        app_name="path-match",
        original_run_dir=str(shared_path),
    )

    upsert_job_location(tmp_path, job_id_match)
    upsert_job_location(tmp_path, path_match)

    assert resolve_job_location(tmp_path, str(shared_path)) == job_id_match


def test_resolve_job_location_matches_canonicalized_path(tmp_path: Path) -> None:
    real_dir = tmp_path / "real" / "run"
    real_dir.mkdir(parents=True)
    alias_parent = tmp_path / "aliases"
    alias_parent.mkdir()
    alias_dir = alias_parent / "run"
    alias_dir.symlink_to(real_dir, target_is_directory=True)

    record = _record(
        "job-456",
        app_name="path-match",
        latest_known_path=str(real_dir),
    )

    upsert_job_location(tmp_path, record)

    assert resolve_job_location(tmp_path, str(real_dir)) == record
