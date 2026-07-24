from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.config import CommonRuntimeConfig
from orca_auto.core.config.engines import WorkflowEngineAppConfig as AppConfig
from orca_auto.core.indexing import JobLocationRecord
from orca_auto.core.indexing import engine_job_locations as shared_job_locations
from orca_auto.flow.engines.crest import job_locations as crest_job_locations
from orca_auto.flow.engines.xtb import job_locations as xtb_job_locations

ENGINE_CASES = [
    (
        xtb_job_locations,
        "orca_auto_xtb",
        {"job_type": "xtb_path_search", "molecule_key": "rxn-2"},
    ),
    (
        crest_job_locations,
        "orca_auto_crest",
        {"job_type": "crest_standard_conformer_search", "molecule_key": "water"},
    ),
]


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


@pytest.mark.parametrize(("module", "app_name", "extras"), ENGINE_CASES)
def test_resolve_latest_job_dir_falls_back_to_existing_target_directory(
    tmp_path: Path,
    module: Any,
    app_name: str,
    extras: dict[str, str],
) -> None:
    index_root = tmp_path / "allowed"
    direct_dir = tmp_path / "orphan-job"
    index_root.mkdir()
    direct_dir.mkdir()

    assert module.resolve_latest_job_dir(index_root, str(direct_dir)) == direct_dir.resolve()
    assert module.resolve_latest_job_dir(index_root, str(tmp_path / "missing")) is None


@pytest.mark.parametrize(("module", "app_name", "extras"), ENGINE_CASES)
def test_resolve_latest_job_dir_returns_none_when_direct_target_cannot_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    app_name: str,
    extras: dict[str, str],
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

    assert module.resolve_latest_job_dir(index_root, str(broken_target)) is None


@pytest.mark.parametrize(("module", "app_name", "extras"), ENGINE_CASES)
def test_resolve_latest_job_dir_skips_blank_and_unresolvable_indexed_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    app_name: str,
    extras: dict[str, str],
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
            app_name=app_name,
            job_type=extras["job_type"],
            status="completed",
            original_run_dir=str(fallback_dir),
            molecule_key=extras["molecule_key"],
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

    assert module.resolve_latest_job_dir(index_root, "job-124") == fallback_dir.resolve()


@pytest.mark.parametrize(("module", "app_name", "extras"), ENGINE_CASES)
def test_resolve_latest_job_dir_returns_none_when_indexed_candidates_are_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    app_name: str,
    extras: dict[str, str],
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
            app_name=app_name,
            job_type=extras["job_type"],
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

    assert module.resolve_latest_job_dir(index_root, "job-404") is None


@pytest.mark.parametrize(("module", "app_name", "extras"), ENGINE_CASES)
def test_record_from_artifacts_returns_none_without_job_id(
    tmp_path: Path,
    module: Any,
    app_name: str,
    extras: dict[str, str],
) -> None:
    assert (
        module.record_from_artifacts(
            job_dir=tmp_path / "job-without-id",
            state={},
            existing=None,
        )
        is None
    )
