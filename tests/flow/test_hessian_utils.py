from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.flow._orca_stage_materialization import (
    OrcaStageMaterializationRequest,
    inhess_geom_block,
    materialize_orca_stage_from_request,
)
from orca_auto.flow.hessian_utils import (
    ANGSTROM_TO_BOHR,
    HessianConversionError,
    parse_xtb_hessian,
    write_orca_hess_from_xtb,
)

_XTB_HESSIAN_6 = """$hessian
  0.11 0.12 0.13 0.14 0.15 0.16
  0.12 0.22 0.23 0.24 0.25 0.26
  0.13 0.23 0.33 0.34 0.35 0.36
  0.14 0.24 0.34 0.44 0.45 0.46
  0.15 0.25 0.35 0.45 0.55 0.56
  0.16 0.26 0.36 0.46 0.56 0.66
"""

_XYZ_2_ATOMS = """2
comment
O   0.000000   0.000000   0.000000
H   0.000000   0.000000   0.970000
"""


def _write_pair(tmp_path: Path) -> tuple[Path, Path]:
    hessian = tmp_path / "hessian"
    hessian.write_text(_XTB_HESSIAN_6, encoding="utf-8")
    xyz = tmp_path / "geom.xyz"
    xyz.write_text(_XYZ_2_ATOMS, encoding="utf-8")
    return hessian, xyz


def test_parse_xtb_hessian_reads_square_matrix(tmp_path: Path) -> None:
    hessian, _ = _write_pair(tmp_path)
    matrix = parse_xtb_hessian(hessian)
    assert len(matrix) == 6
    assert matrix[0][0] == pytest.approx(0.11)
    assert matrix[5][5] == pytest.approx(0.66)
    assert matrix[2][4] == matrix[4][2]


def test_parse_xtb_hessian_rejects_non_square(tmp_path: Path) -> None:
    hessian = tmp_path / "hessian"
    hessian.write_text("$hessian\n0.1 0.2 0.3\n", encoding="utf-8")
    with pytest.raises(HessianConversionError):
        parse_xtb_hessian(hessian)


def test_write_orca_hess_from_xtb_layout(tmp_path: Path) -> None:
    hessian, xyz = _write_pair(tmp_path)
    target = tmp_path / "seed.hess"
    write_orca_hess_from_xtb(xtb_hessian_path=hessian, xyz_path=xyz, target_path=target)
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "$orca_hessian_file"
    hess_index = lines.index("$hessian")
    assert lines[hess_index + 1] == "6"
    atoms_index = lines.index("$atoms")
    assert lines[atoms_index + 1] == "2"
    hydrogen = lines[atoms_index + 3].split()
    assert hydrogen[0] == "H"
    assert float(hydrogen[4]) == pytest.approx(0.97 * ANGSTROM_TO_BOHR)
    assert "$end" in lines
    # first hessian data row carries the row index then five columns
    first_row = lines[hess_index + 3].split()
    assert first_row[0] == "0"
    assert float(first_row[1]) == pytest.approx(0.11)


def test_write_orca_hess_dimension_mismatch(tmp_path: Path) -> None:
    hessian, _ = _write_pair(tmp_path)
    xyz = tmp_path / "one_atom.xyz"
    xyz.write_text("1\ncomment\nO 0.0 0.0 0.0\n", encoding="utf-8")
    with pytest.raises(HessianConversionError):
        write_orca_hess_from_xtb(
            xtb_hessian_path=hessian,
            xyz_path=xyz,
            target_path=tmp_path / "seed.hess",
        )


def _materialization_request(
    tmp_path: Path, *, inhess_source_path: str
) -> OrcaStageMaterializationRequest:
    source_xyz = tmp_path / "candidate.xyz"
    source_xyz.write_text(_XYZ_2_ATOMS, encoding="utf-8")
    return OrcaStageMaterializationRequest(
        workspace_dir=tmp_path / "workspace",
        stage_root_name="",
        stage_key="01_ts_guess",
        source_artifact_path=str(source_xyz),
        candidate_kind="ts_guess",
        route_line="! OptTS B3LYP def2-SVP Freq",
        charge=0,
        multiplicity=1,
        max_cores=4,
        max_memory_gb=8,
        xyz_filename="ts_guess.xyz",
        inp_filename="ts_guess.inp",
        inhess_source_path=inhess_source_path,
    )


def test_materialize_orca_stage_writes_inhess_block(tmp_path: Path) -> None:
    hessian, _ = _write_pair(tmp_path)
    request = _materialization_request(tmp_path, inhess_source_path=str(hessian))
    materialized = materialize_orca_stage_from_request(request)
    reaction_dir = Path(materialized.reaction_dir)
    inp_text = (reaction_dir / "ts_guess.inp").read_text(encoding="utf-8")
    assert inhess_geom_block("ts_guess.inhess.hess") in inp_text
    assert (reaction_dir / "ts_guess.inhess.hess").exists()
    # `<inp stem>.hess` is what ORCA itself writes under a Freq route; the
    # execution-snapshot binding rejects referenced inputs with that name.
    assert not (reaction_dir / "ts_guess.hess").exists()
    source_payload = (reaction_dir / "source_candidate.json").read_text(encoding="utf-8")
    assert "hessian_handoff" in source_payload
    assert "hess_path" in source_payload


def test_materialized_inhess_survives_freq_snapshot_binding(tmp_path: Path) -> None:
    """The reaction handoff shape must pass the ORCA execution-snapshot gate.

    Regression for the run where every OptTS+Freq candidate submission was
    rejected with `ORCA referenced input basename conflicts with a generation
    runtime/output file: ts_guess.hess`.
    """
    from orca_auto.orca.execution_binding import build_orca_execution_snapshot

    hessian, _ = _write_pair(tmp_path)
    request = _materialization_request(tmp_path, inhess_source_path=str(hessian))
    materialized = materialize_orca_stage_from_request(request)
    reaction_dir = Path(materialized.reaction_dir)
    executable = tmp_path / "orca"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    snapshot = build_orca_execution_snapshot(
        reaction_dir,
        Path(materialized.selected_inp),
        selected_input_xyz=materialized.selected_xyz,
        resource_request={"max_cores": 4, "max_memory_gb": 8},
        max_retries=2,
        orca_executable=executable,
    )
    dependency_names = {Path(path).name for path in snapshot["dependency_paths"]}
    assert "ts_guess.inhess.hess" in dependency_names


def test_materialize_orca_stage_falls_back_without_hessian(tmp_path: Path) -> None:
    request = _materialization_request(
        tmp_path, inhess_source_path=str(tmp_path / "missing_hessian")
    )
    materialized = materialize_orca_stage_from_request(request)
    reaction_dir = Path(materialized.reaction_dir)
    inp_text = (reaction_dir / "ts_guess.inp").read_text(encoding="utf-8")
    assert "InHess" not in inp_text
    assert not (reaction_dir / "ts_guess.inhess.hess").exists()
    source_payload = (reaction_dir / "source_candidate.json").read_text(encoding="utf-8")
    assert "hessian_handoff" in source_payload
    assert "error" in source_payload


def test_materialize_orca_stage_without_inhess_source(tmp_path: Path) -> None:
    request = _materialization_request(tmp_path, inhess_source_path="")
    materialized = materialize_orca_stage_from_request(request)
    reaction_dir = Path(materialized.reaction_dir)
    inp_text = (reaction_dir / "ts_guess.inp").read_text(encoding="utf-8")
    assert "InHess" not in inp_text
    source_payload = (reaction_dir / "source_candidate.json").read_text(encoding="utf-8")
    assert "hessian_handoff" not in source_payload
