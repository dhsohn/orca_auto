from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.commands.run_dir import validate_production_run_dir_target
from orca_auto.core.paths import (
    iter_production_runs_artifacts,
    should_exclude_from_production_runs_scan,
)


def test_normal_runs_path_symlink_escape_fails_closed(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    production_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escaping_alias = production_root / "escaping-job"
    escaping_alias.symlink_to(outside, target_is_directory=True)

    assert should_exclude_from_production_runs_scan(escaping_alias, production_root) is True


def test_symlink_loop_fails_closed(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    production_root.mkdir()
    looping_alias = production_root / "loop"
    looping_alias.symlink_to(looping_alias, target_is_directory=True)

    assert should_exclude_from_production_runs_scan(looping_alias, production_root) is True


def test_visible_execution_generation_is_not_indexed_as_a_second_job(tmp_path: Path) -> None:
    production_root = tmp_path / "runs"
    job_dir = production_root / "TS8(NEB-TS)"
    generation_dir = job_dir / "20260714-224054-959479f2"
    generation_dir.mkdir(parents=True)
    public_state = job_dir / "job_state.json"
    generation_state = generation_dir / "job_state.json"
    public_state.write_text("{}", encoding="utf-8")
    generation_state.write_text("{}", encoding="utf-8")

    assert should_exclude_from_production_runs_scan(generation_state, production_root) is True
    assert list(iter_production_runs_artifacts(production_root, "job_state.json")) == [public_state]
    with pytest.raises(ValueError, match="public parent job directory"):
        validate_production_run_dir_target(generation_dir, production_root)
