from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.core.geometry_limits import (
    MAX_ADMISSION_ATOMS,
    MAX_HESSIAN_ADMISSION_ATOMS,
)
from orca_auto.flow import xyz_utils
from orca_auto.flow.engines.crest import submission as crest_submission
from orca_auto.flow.engines.xtb import job_inputs as xtb_job_inputs
from orca_auto.flow.engines.xtb import submission as xtb_submission
from orca_auto.flow.orchestration.workflow_builders import _copy_input_impl


def _write_xyz(path: Path, atom_count: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{atom_count}\ninput\n" + "H 0 0 0\n" * atom_count,
        encoding="utf-8",
    )
    return path


def test_xyz_parser_rejects_declared_million_atoms_before_line_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "huge.xyz"
    source.write_text("1000000\ninput\n", encoding="utf-8")
    monkeypatch.setattr(
        xyz_utils,
        "_parse_xyz_frames",
        lambda *_args, **_kwargs: pytest.fail("oversized XYZ was materialized"),
    )

    result = xyz_utils.parse_xyz_file(source)

    assert result.error_reason == "atom_count_exceeds_limit"


def test_xtb_admission_rejects_server_atom_cap(tmp_path: Path) -> None:
    job_dir = tmp_path / "xtb"
    _write_xyz(job_dir / "input.xyz", MAX_ADMISSION_ATOMS + 1)

    with pytest.raises(ValueError, match="valid finite XYZ"):
        xtb_job_inputs.resolve_job_inputs(
            job_dir,
            {"job_type": "opt", "input_xyz": "input.xyz"},
        )


def test_crest_admission_rejects_server_atom_cap_before_executable_resolution(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "crest"
    _write_xyz(job_dir / "input.xyz", MAX_ADMISSION_ATOMS + 1)

    with pytest.raises(ValueError, match="valid finite XYZ"):
        crest_submission._build_submission(
            object(),
            job_dir,
            {"input_xyz": "input.xyz"},
            object(),
        )


def test_xtb_hessian_admission_uses_stricter_atom_cap(tmp_path: Path) -> None:
    job_dir = tmp_path / "xtb-hessian"
    _write_xyz(job_dir / "input.xyz", MAX_HESSIAN_ADMISSION_ATOMS + 1)

    with pytest.raises(ValueError, match="server atom-count limit"):
        xtb_submission._build_submission_impl(
            object(),
            job_dir,
            {"job_type": "hess", "input_xyz": "input.xyz"},
            object(),
            job_id="hessian-limit-test",
            snapshot_namespace="hessian-limit-test-snapshot",
        )


def test_workflow_input_copy_rejects_server_atom_cap_before_materialization(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.xyz"
    source.write_text(f"{MAX_ADMISSION_ATOMS + 1}\ninput\n", encoding="utf-8")
    target = tmp_path / "workflow" / "inputs" / "source.xyz"

    with pytest.raises(ValueError, match="server atom-count limit"):
        _copy_input_impl(str(source), target)

    assert not target.exists()
