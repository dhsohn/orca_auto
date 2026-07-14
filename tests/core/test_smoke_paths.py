from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from orca_auto.core.commands.run_dir import validate_production_run_dir_target
from orca_auto.core.paths import (
    SMOKE_RESULTS_DIRNAME,
    is_path_in_reserved_smoke_tree,
    iter_production_runs_artifacts,
    should_exclude_from_production_runs_scan,
)


def test_reserved_smoke_tree_is_relative_to_the_supplied_runs_root(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    case_runs_root = (
        production_root / SMOKE_RESULTS_DIRNAME / "batch-1" / "case-1" / "runtime" / "runs"
    )
    job_dir = case_runs_root / "job-1"
    job_dir.mkdir(parents=True)

    assert is_path_in_reserved_smoke_tree(job_dir, production_root) is True
    assert is_path_in_reserved_smoke_tree(job_dir, case_runs_root) is False
    assert (
        is_path_in_reserved_smoke_tree(
            production_root / "job-1" / SMOKE_RESULTS_DIRNAME,
            production_root,
        )
        is False
    )


def test_reserved_smoke_tree_checks_lexical_and_resolved_symlink_paths(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    reserved_job = production_root / SMOKE_RESULTS_DIRNAME / "batch-1" / "job-1"
    reserved_job.mkdir(parents=True)

    alias_to_reserved = tmp_path / "alias-to-reserved"
    alias_to_reserved.symlink_to(reserved_job, target_is_directory=True)
    assert is_path_in_reserved_smoke_tree(alias_to_reserved, production_root) is True

    outside = tmp_path / "outside"
    outside.mkdir()
    reserved_alias = production_root / SMOKE_RESULTS_DIRNAME / "outside-alias"
    reserved_alias.symlink_to(outside, target_is_directory=True)
    assert is_path_in_reserved_smoke_tree(reserved_alias, production_root) is True


def test_normal_runs_path_symlink_escape_fails_closed(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    production_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escaping_alias = production_root / "escaping-job"
    escaping_alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes runs_root"):
        is_path_in_reserved_smoke_tree(escaping_alias, production_root)
    assert should_exclude_from_production_runs_scan(escaping_alias, production_root) is True


def test_symlink_loop_fails_closed(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    production_root.mkdir()
    looping_alias = production_root / "loop"
    looping_alias.symlink_to(looping_alias, target_is_directory=True)

    with pytest.raises(ValueError, match="Could not safely resolve path"):
        is_path_in_reserved_smoke_tree(looping_alias, production_root)
    assert should_exclude_from_production_runs_scan(looping_alias, production_root) is True


def test_production_submission_gate_rejects_only_its_reserved_tree(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    case_runs_root = production_root / SMOKE_RESULTS_DIRNAME / "batch" / "case" / "runs"
    job_dir = case_runs_root / "job"
    job_dir.mkdir(parents=True)

    with pytest.raises(ValueError, match="reserved smoke-results subtree"):
        validate_production_run_dir_target(job_dir, production_root)

    validate_production_run_dir_target(job_dir, case_runs_root)


def test_production_artifact_iterator_prunes_reserved_top_level_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_root = tmp_path / "runs"
    production_job = production_root / "job"
    reserved_job = production_root / SMOKE_RESULTS_DIRNAME / "batch" / "job"
    production_job.mkdir(parents=True)
    reserved_job.mkdir(parents=True)
    production_state = production_job / "job_state.json"
    reserved_state = reserved_job / "job_state.json"
    production_state.write_text("{}", encoding="utf-8")
    reserved_state.write_text("{}", encoding="utf-8")
    traversed_roots: list[Path] = []
    original_rglob = Path.rglob

    def _tracking_rglob(path: Path, pattern: str) -> Iterator[Path]:
        traversed_roots.append(path)
        return original_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", _tracking_rglob)

    assert list(iter_production_runs_artifacts(production_root, "job_state.json")) == [
        production_state
    ]
    assert production_job in traversed_roots
    assert production_root / SMOKE_RESULTS_DIRNAME not in traversed_roots
