from __future__ import annotations

from pathlib import Path

import pytest

import orca_auto.flow.engines.xtb.job_locations as job_locations_module
from orca_auto.core.config import CommonRuntimeConfig
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.indexing import JobLocationRecord, get_job_location, upsert_job_location
from orca_auto.core.indexing import engine_job_locations as shared_job_locations
from orca_auto.core.indexing.engines import resource_dict
from orca_auto.flow.engines.xtb.engine import ENGINE_DEFINITION
from orca_auto.flow.engines.xtb.job_locations import (
    build_job_location_record,
    normalize_key,
    reaction_key_from_job_dir,
    record_from_artifacts,
    resolve_latest_job_dir,
    upsert_job_record,
)
from orca_auto.flow.engines.xtb.state import write_report_json, write_state
from orca_auto.flow.state import write_workflow_payload


def _make_cfg(tmp_path: Path) -> tuple[AppConfig, Path]:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    return (
        AppConfig(
            runtime=CommonRuntimeConfig(
                allowed_root=str(allowed_root),
            )
        ),
        allowed_root.resolve(),
    )


def _write_xyz(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\nexample\nH 0.0 0.0 0.0\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Reaction Step 1  ", "reaction_step_1"),
        ("A.B-C++ sample", "a.b-c_sample"),
        ("---", "unknown_key"),
    ],
)
def test_normalize_key_sanitizes_and_defaults(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


def test_reaction_key_from_job_dir_normalizes_name(tmp_path: Path) -> None:
    job_dir = tmp_path / "Screening Batch"

    assert reaction_key_from_job_dir(job_dir) == "screening_batch"


def test_resource_dict_and_terminal_status_helpers() -> None:
    assert resource_dict(0, -2) == {"max_cores": 1, "max_memory_gb": 1}


def test_runtime_roots_for_cfg_skips_workflows_without_xtb_stages(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflow_root"
    conformer_workspace = workflow_root / "wf_conformer"
    reaction_workspace = workflow_root / "wf_reaction"
    cfg = AppConfig(
        runtime=CommonRuntimeConfig(
            allowed_root=str(workflow_root),
        ),
        workflow_root=str(workflow_root),
    )
    write_workflow_payload(
        conformer_workspace,
        {
            "workflow_id": "wf_conformer",
            "stages": [{"stage_id": "crest_conformer_01", "task": {"engine": "crest"}}],
        },
    )
    write_workflow_payload(
        reaction_workspace,
        {
            "workflow_id": "wf_reaction",
            "stages": [{"stage_id": "xtb_path_search_01", "task": {"engine": "xtb"}}],
        },
    )

    roots = job_locations_module.runtime_roots_for_cfg(cfg)

    assert roots == ((reaction_workspace / "02_xtb").resolve(),)
    assert ENGINE_DEFINITION.build_queue_runtime().queue_roots(cfg) == roots
    assert not (conformer_workspace / "02_xtb").exists()


def test_build_job_location_record_merges_existing_fields(
    tmp_path: Path,
) -> None:
    original_dir = tmp_path / "runs" / "job-001"
    selected_xyz = _write_xyz(original_dir / "Reaction.xyz")
    existing = JobLocationRecord(
        job_id="job-001",
        app_name="orca_auto_xtb",
        job_type="xtb_ranking",
        status="running",
        original_run_dir=str(original_dir.resolve()),
        molecule_key="rxn-1",
        selected_input_xyz=str(selected_xyz.resolve()),
        latest_known_path=str(original_dir.resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        resource_actual={},
    )

    record = build_job_location_record(
        existing=existing,
        job_id=" job-001 ",
        status=" completed ",
        job_dir=original_dir,
        job_type="opt",
        selected_input_xyz="",
        resource_request={"max_cores": 6, "max_memory_gb": 12},
    )

    assert record.job_id == "job-001"
    assert record.status == "completed"
    assert record.job_type == "xtb_opt"
    assert record.original_run_dir == str(original_dir.resolve())
    assert record.selected_input_xyz == str(selected_xyz.resolve())
    assert record.molecule_key == "rxn-1"
    assert record.latest_known_path == str(original_dir.resolve())
    assert record.resource_request == {"max_cores": 6, "max_memory_gb": 12}
    assert record.resource_actual == {"max_cores": 6, "max_memory_gb": 12}


def test_build_job_location_record_defaults_reaction_key_and_actual_resources(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "Fresh Reaction"
    job_dir.mkdir()

    record = build_job_location_record(
        job_id="job-002",
        status="queued",
        job_dir=job_dir,
        job_type="ranking",
        selected_input_xyz="",
        reaction_key="",
        resource_request=resource_dict(0, 0),
    )

    assert record.job_type == "xtb_ranking"
    assert record.molecule_key == "fresh_reaction"
    assert record.latest_known_path == str(job_dir.resolve())
    assert record.resource_request == {"max_cores": 1, "max_memory_gb": 1}
    assert record.resource_actual == {"max_cores": 1, "max_memory_gb": 1}


def test_resolve_latest_job_dir_prefers_indexed_candidates_and_direct_lookup(
    tmp_path: Path,
) -> None:
    index_root = tmp_path / "allowed"
    job_dir = tmp_path / "runs" / "job-123"
    index_root.mkdir()
    job_dir.mkdir(parents=True)

    upsert_job_location(
        index_root,
        JobLocationRecord(
            job_id="job-123",
            app_name="orca_auto_xtb",
            job_type="xtb_ranking",
            status="completed",
            original_run_dir=str(job_dir.resolve()),
            molecule_key="rxn-1",
            selected_input_xyz="",
            latest_known_path=str(job_dir.resolve()),
            resource_request={},
            resource_actual={},
        ),
    )

    assert resolve_latest_job_dir(index_root, "job-123") == job_dir.resolve()
    assert resolve_latest_job_dir(index_root, str(job_dir.resolve())) == job_dir.resolve()


def test_resolve_latest_job_dir_falls_back_to_existing_target_directory(tmp_path: Path) -> None:
    index_root = tmp_path / "allowed"
    direct_dir = tmp_path / "orphan-job"
    index_root.mkdir()
    direct_dir.mkdir()

    assert resolve_latest_job_dir(index_root, str(direct_dir)) == direct_dir.resolve()
    assert resolve_latest_job_dir(index_root, str(tmp_path / "missing")) is None


def test_resolve_latest_job_dir_returns_none_when_direct_target_cannot_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_root = tmp_path / "allowed"
    broken_target = tmp_path / "broken-target"
    index_root.mkdir()
    real_resolve = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == broken_target:
            raise OSError("cannot resolve path")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    assert resolve_latest_job_dir(index_root, str(broken_target)) is None


def test_resolve_latest_job_dir_skips_blank_and_unresolvable_indexed_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_root = tmp_path / "allowed"
    fallback_dir = tmp_path / "runs" / "job-124"
    broken_indexed_path = tmp_path / "broken-indexed"
    index_root.mkdir()
    fallback_dir.mkdir(parents=True)
    real_resolve = Path.resolve

    monkeypatch.setattr(
        shared_job_locations,
        "resolve_job_location",
        lambda root, target: JobLocationRecord(
            job_id="job-124",
            app_name="orca_auto_xtb",
            job_type="xtb_path_search",
            status="completed",
            original_run_dir=str(fallback_dir),
            molecule_key="rxn-2",
            selected_input_xyz="",
            latest_known_path="",
            resource_request={},
            resource_actual={},
        ),
    )

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == broken_indexed_path:
            raise OSError("cannot resolve indexed path")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    assert resolve_latest_job_dir(index_root, "job-124") == fallback_dir.resolve()


def test_resolve_latest_job_dir_returns_none_when_indexed_candidates_are_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_root = tmp_path / "allowed"
    broken_indexed_path = tmp_path / "broken-indexed"
    missing_dir = tmp_path / "missing-dir"
    index_root.mkdir()
    real_resolve = Path.resolve

    monkeypatch.setattr(
        shared_job_locations,
        "resolve_job_location",
        lambda root, target: JobLocationRecord(
            job_id="job-404",
            app_name="orca_auto_xtb",
            job_type="xtb_path_search",
            status="failed",
            original_run_dir=str(missing_dir),
            molecule_key="sample",
            selected_input_xyz="",
            latest_known_path="",
            resource_request={},
            resource_actual={},
        ),
    )

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == broken_indexed_path:
            raise OSError("cannot resolve indexed path")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    assert resolve_latest_job_dir(index_root, "job-404") is None


def test_load_job_artifacts_reads_files_for_resolved_job(tmp_path: Path) -> None:
    index_root = tmp_path / "allowed"
    job_dir = tmp_path / "runs" / "job-200"
    index_root.mkdir()
    job_dir.mkdir(parents=True)

    selected_xyz = _write_xyz(job_dir / "sample.xyz")
    state_payload = {
        "job_id": "job-200",
        "status": "running",
        "selected_input_xyz": str(selected_xyz.resolve()),
    }
    report_payload = {
        "job_id": "job-200",
        "status": "completed",
        "reaction_key": "sample",
    }
    write_state(job_dir, state_payload)
    write_report_json(job_dir, report_payload)
    upsert_job_location(
        index_root,
        JobLocationRecord(
            job_id="job-200",
            app_name="orca_auto_xtb",
            job_type="xtb_sp",
            status="completed",
            original_run_dir=str(job_dir.resolve()),
            molecule_key="sample",
            selected_input_xyz=str(selected_xyz.resolve()),
            latest_known_path=str(job_dir.resolve()),
            resource_request={},
            resource_actual={},
        ),
    )

    resolved_job_dir, state, report = job_locations_module.load_job_artifacts(index_root, "job-200")

    assert resolved_job_dir == job_dir.resolve()
    assert state == state_payload
    assert report == report_payload
    assert job_locations_module.load_job_artifacts(index_root, "missing") == (None, None, None)


def test_record_from_artifacts_merges_sources_and_existing_values(tmp_path: Path) -> None:
    job_dir = tmp_path / "runs" / "job-300"
    selected_xyz = _write_xyz(tmp_path / "inputs" / "Fancy Name.xyz")
    job_dir.mkdir(parents=True)
    existing = JobLocationRecord(
        job_id="job-old",
        app_name="orca_auto_xtb",
        job_type="xtb_path_search",
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
        state={"job_id": "job-300", "status": "completed"},
        report={
            "job_type": "opt",
            "original_run_dir": str(job_dir.resolve()),
            "resource_request": {"max_cores": "8", "max_memory_gb": "16"},
            "selected_input_xyz": str(selected_xyz.resolve()),
            "resource_actual": "invalid",
        },
        existing=existing,
    )

    assert record is not None
    assert record.job_id == "job-300"
    assert record.status == "completed"
    assert record.job_type == "xtb_opt"
    assert record.original_run_dir == str(job_dir.resolve())
    assert record.selected_input_xyz == str(selected_xyz.resolve())
    assert record.molecule_key == "job-300"
    assert record.latest_known_path == str(job_dir.resolve())
    assert record.resource_request == {"max_cores": 8, "max_memory_gb": 16}
    assert record.resource_actual == {"max_cores": 2, "max_memory_gb": 3}


def test_record_from_artifacts_returns_none_without_job_id(tmp_path: Path) -> None:
    assert (
        record_from_artifacts(
            job_dir=tmp_path / "job-without-id",
            state={},
            report={},
            existing=None,
        )
        is None
    )


def test_record_from_artifacts_defaults_invalid_resources_without_existing(
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
            "resource_actual": "invalid",
        },
        report={"selected_input_xyz": str(selected_xyz.resolve())},
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
        job_type="ranking",
        selected_input_xyz=str(selected_xyz.resolve()),
        resource_request={"max_cores": 4, "max_memory_gb": 8},
    )
    updated = upsert_job_record(
        cfg,
        job_id="job-500",
        status="completed",
        job_dir=job_dir,
        job_type="opt",
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
    assert stored.job_type == "xtb_opt"
    assert stored.original_run_dir == str(job_dir.resolve())
    assert stored.selected_input_xyz == str(selected_xyz.resolve())
    assert stored.molecule_key == "job-500"
    assert stored.latest_known_path == str(job_dir.resolve())
    assert stored.resource_request == {"max_cores": 6, "max_memory_gb": 12}
    assert stored.resource_actual == {"max_cores": 5, "max_memory_gb": 10}
