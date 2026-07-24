from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.indexing import JobLocationRecord, get_job_location, upsert_job_location
from orca_auto.flow.engines.crest.job_locations import (
    build_job_location_record,
    molecule_key_from_selected_xyz,
    normalize_molecule_key,
    record_from_artifacts,
    resolve_latest_job_dir,
    upsert_job_record,
)
from tests.flow.test_job_locations_common_engines import _make_cfg, _write_xyz


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Water Molecule  ", "water_molecule"),
        ("A.B-C++ sample", "a.b-c_sample"),
        ("---", "unknown_molecule"),
    ],
)
def test_normalize_molecule_key_sanitizes_and_defaults(raw: str, expected: str) -> None:
    assert normalize_molecule_key(raw) == expected


def test_molecule_key_from_selected_xyz_uses_selected_name_or_job_dir(tmp_path: Path) -> None:
    job_dir = tmp_path / "Job Folder"

    assert molecule_key_from_selected_xyz("/tmp/My Input File.xyz", job_dir) == "my_input_file"
    assert molecule_key_from_selected_xyz("   ", job_dir) == "job_folder"


def test_build_job_location_record_merges_existing_fields_and_defaults_actual_resources(
    tmp_path: Path,
) -> None:
    original_dir = tmp_path / "runs" / "job-001"
    rerun_dir = tmp_path / "reruns" / "job-001"
    selected_xyz = _write_xyz(original_dir / "Water.xyz")
    existing = JobLocationRecord(
        job_id="job-001",
        app_name="orca_auto_crest",
        job_type="crest_standard_conformer_search",
        status="running",
        original_run_dir=str(original_dir.resolve()),
        molecule_key="water",
        selected_input_xyz=str(selected_xyz.resolve()),
        latest_known_path=str(original_dir.resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={},
    )

    record = build_job_location_record(
        existing=existing,
        job_id=" job-001 ",
        status=" completed ",
        job_dir=rerun_dir,
        mode="nci",
        selected_input_xyz="",
        resource_request={"max_cores": 6, "max_memory_gb": 12},
    )

    assert record.job_id == "job-001"
    assert record.status == "completed"
    assert record.job_type == "crest_nci_conformer_search"
    assert record.original_run_dir == str(original_dir.resolve())
    assert record.selected_input_xyz == str(selected_xyz.resolve())
    assert record.molecule_key == "water"
    assert record.latest_known_path == str(rerun_dir.resolve())
    assert record.resource_request == {"max_cores": 6, "max_memory_gb": 12}
    assert record.resource_actual == {"max_cores": 6, "max_memory_gb": 12}


def test_resolve_latest_job_dir_prefers_indexed_candidates_and_path_lookup(tmp_path: Path) -> None:
    index_root = tmp_path / "allowed"
    index_root.mkdir()
    original_dir = tmp_path / "runs" / "job-123"
    original_dir.mkdir(parents=True)

    upsert_job_location(
        index_root,
        JobLocationRecord(
            job_id="job-123",
            app_name="orca_auto_crest",
            job_type="crest_standard_conformer_search",
            status="completed",
            original_run_dir=str(original_dir.resolve()),
            molecule_key="water",
            selected_input_xyz="",
            latest_known_path=str((tmp_path / "missing" / "job-123").resolve()),
            resource_request={},
            resource_actual={},
        ),
    )

    assert resolve_latest_job_dir(index_root, "job-123") == original_dir.resolve()
    assert resolve_latest_job_dir(index_root, str(original_dir.resolve())) == original_dir.resolve()


def test_record_from_artifacts_merges_sources_and_existing_values(tmp_path: Path) -> None:
    job_dir = tmp_path / "runs" / "job-300"
    selected_xyz = _write_xyz(tmp_path / "inputs" / "Fancy Name.xyz")
    job_dir.mkdir(parents=True)
    existing = JobLocationRecord(
        job_id="job-old",
        app_name="orca_auto_crest",
        job_type="crest_standard_conformer_search",
        status="queued",
        original_run_dir="",
        molecule_key="",
        selected_input_xyz="",
        latest_known_path="",
        resource_request={},
        resource_actual={"max_cores": 2, "max_memory_gb": 3},
    )

    record = record_from_artifacts(
        job_dir=job_dir,
        state={
            "job_id": "job-300",
            "status": "completed",
            "mode": "nci",
            "original_run_dir": str(job_dir.resolve()),
            "selected_input_xyz": str(selected_xyz.resolve()),
            "resource_request": {"max_cores": "8", "max_memory_gb": "16"},
        },
        existing=existing,
    )

    assert record is not None
    assert record.job_id == "job-300"
    assert record.status == "completed"
    assert record.job_type == "crest_nci_conformer_search"
    assert record.original_run_dir == str(job_dir.resolve())
    assert record.selected_input_xyz == str(selected_xyz.resolve())
    assert record.molecule_key == "fancy_name"
    assert record.latest_known_path == str(job_dir.resolve())
    assert record.resource_request == {"max_cores": 8, "max_memory_gb": 16}
    assert record.resource_actual == {"max_cores": 2, "max_memory_gb": 3}


def test_record_from_artifacts_defaults_invalid_resource_request_without_existing(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "job-301"
    selected_xyz = _write_xyz(job_dir / "input.xyz")

    record = record_from_artifacts(
        job_dir=job_dir,
        state={
            "job_id": "job-301",
            "status": "queued",
            "resource_request": "invalid",
            "selected_input_xyz": str(selected_xyz.resolve()),
        },
        existing=None,
    )

    assert record is not None
    assert record.resource_request == {}
    assert record.resource_actual == {}


def test_upsert_job_record_writes_and_updates_existing_index_entry(tmp_path: Path) -> None:
    cfg, allowed_root = _make_cfg(tmp_path)
    job_dir = allowed_root / "runs" / "job-500"
    selected_xyz = _write_xyz(job_dir / "Water Sample.xyz")

    first = upsert_job_record(
        cfg,
        job_id="job-500",
        status="running",
        job_dir=job_dir,
        mode="standard",
        selected_input_xyz=str(selected_xyz.resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
    )
    updated = upsert_job_record(
        cfg,
        job_id="job-500",
        status="completed",
        job_dir=job_dir,
        mode="nci",
        selected_input_xyz="",
        resource_request={"max_cores": 6, "max_memory_gb": 12},
        resource_actual={"max_cores": 5, "max_memory_gb": 10},
    )
    stored = get_job_location(allowed_root, "job-500")

    assert first.original_run_dir == str(job_dir.resolve())
    assert first.latest_known_path == str(job_dir.resolve())
    assert first.resource_actual == {"max_cores": 4, "max_memory_gb": 8}
    assert stored == updated
    assert stored is not None
    assert stored.status == "completed"
    assert stored.job_type == "crest_nci_conformer_search"
    assert stored.original_run_dir == str(job_dir.resolve())
    assert stored.selected_input_xyz == str(selected_xyz.resolve())
    assert stored.molecule_key == "water_sample"
    assert stored.latest_known_path == str(job_dir.resolve())
    assert stored.resource_request == {"max_cores": 6, "max_memory_gb": 12}
    assert stored.resource_actual == {"max_cores": 5, "max_memory_gb": 10}
