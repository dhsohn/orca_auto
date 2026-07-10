from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.flow.contracts.xtb import XtbArtifactContract, XtbCandidateArtifact
from orca_auto.flow.engines.xtb.runner_artifacts import _ts_guess_validation_fields
from orca_auto.flow.geometry_validation import (
    GeometryValidationError,
    validate_ts_guess_geometry,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_handoff import xtb_handoff_status_impl
from orca_auto.flow.orchestration.support import reaction_ts_guess_error_impl

# Four-atom H-transfer model: C-Hm + O-Hs -> C + Hm-O-Hs (atoms C, Hm, O, Hs).
# Bond cutoffs at scale 1.25: C-H 1.34 A, O-H 1.19 A.


def _write(path: Path, atom_rows: list[str]) -> Path:
    path.write_text(f"{len(atom_rows)}\ncomment\n" + "\n".join(atom_rows) + "\n", encoding="utf-8")
    return path


def _endpoints(tmp_path: Path) -> tuple[Path, Path]:
    reactant = _write(
        tmp_path / "r.xyz",
        ["C 0.0 0.0 0.0", "H 1.05 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    product = _write(
        tmp_path / "p.xyz",
        ["C 0.0 0.0 0.0", "H 3.05 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    return reactant, product


def test_validate_accepts_reactant_like_ts(tmp_path: Path) -> None:
    reactant, product = _endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 1.25 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    verdict = validate_ts_guess_geometry(
        ts_guess_xyz=ts, reactant_xyz=reactant, product_xyz=product
    )
    assert verdict.valid
    assert verdict.reasons == ()
    assert verdict.metrics["reaction_bond_count"] == 2


def test_validate_accepts_mid_transfer_atom_bonded_to_neither(tmp_path: Path) -> None:
    # The migrating H sits beyond the bond cutoff of both partners; the
    # reaction-bond union must keep it from counting as an extra fragment.
    reactant, product = _endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 2.0 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    verdict = validate_ts_guess_geometry(
        ts_guess_xyz=ts, reactant_xyz=reactant, product_xyz=product
    )
    assert verdict.valid


def test_validate_rejects_scrambled_spectator(tmp_path: Path) -> None:
    # Spectator H swapped onto the carbon while the migrating H sits on O:
    # two bond changes outside the reacting set.
    reactant, product = _endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 4.95 0.0 0.0", "O 4.0 0.0 0.0", "H 1.05 0.0 0.0"],
    )
    verdict = validate_ts_guess_geometry(
        ts_guess_xyz=ts, reactant_xyz=reactant, product_xyz=product
    )
    assert not verdict.valid
    assert any("rearranged" in reason for reason in verdict.reasons)


def test_validate_rejects_fragmented_beyond_endpoints(tmp_path: Path) -> None:
    # Conformational "reaction" (no bond change); the TS breaks the chain, so
    # both the fragmentation and rearrangement rules fire.
    reactant = _write(tmp_path / "r.xyz", ["C 0.0 0.0 0.0", "C 1.4 0.0 0.0", "C 2.8 0.0 0.0"])
    product = _write(tmp_path / "p.xyz", ["C 0.0 0.0 0.0", "C 1.4 0.0 0.0", "C 2.8 0.0 0.0"])
    ts = _write(tmp_path / "ts.xyz", ["C 0.0 0.0 0.0", "C 1.4 0.0 0.0", "C 6.0 0.0 0.0"])
    verdict = validate_ts_guess_geometry(
        ts_guess_xyz=ts, reactant_xyz=reactant, product_xyz=product
    )
    assert not verdict.valid
    assert any("fragmented" in reason for reason in verdict.reasons)
    assert any("rearranged" in reason for reason in verdict.reasons)


def test_validate_raises_on_mismatched_geometries(tmp_path: Path) -> None:
    reactant, product = _endpoints(tmp_path)
    short = _write(tmp_path / "short.xyz", ["C 0.0 0.0 0.0", "H 1.05 0.0 0.0"])
    with pytest.raises(GeometryValidationError):
        validate_ts_guess_geometry(ts_guess_xyz=short, reactant_xyz=reactant, product_xyz=product)
    swapped = _write(
        tmp_path / "swapped.xyz",
        ["H 0.0 0.0 0.0", "C 1.05 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    with pytest.raises(GeometryValidationError):
        validate_ts_guess_geometry(ts_guess_xyz=swapped, reactant_xyz=reactant, product_xyz=product)


def test_ts_guess_validation_fields_annotations(tmp_path: Path) -> None:
    reactant, product = _endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 1.25 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    input_summary = {"reactant_xyz": str(reactant), "product_xyz": str(product)}

    fields = _ts_guess_validation_fields(str(ts), input_summary, {})
    assert fields is not None
    assert fields["geometry_valid"] is True
    assert fields["geometry_validation"]["valid"] is True

    # disabled via manifest
    assert (
        _ts_guess_validation_fields(
            str(ts), input_summary, {"ts_guess_validation": {"enabled": False}}
        )
        is None
    )
    # endpoints unknown
    assert _ts_guess_validation_fields(str(ts), {}, {}) is None
    # unreadable comparison -> error recorded, no verdict
    bad = _write(tmp_path / "bad.xyz", ["C 0.0 0.0 0.0"])
    fields = _ts_guess_validation_fields(str(bad), input_summary, {})
    assert fields is not None
    assert "geometry_valid" not in fields
    assert "error" in fields["geometry_validation"]


def _invalid_ts_contract(tmp_path: Path) -> XtbArtifactContract:
    ts = _write(
        tmp_path / "xtbpath_ts.xyz",
        ["C 0.0 0.0 0.0", "H 1.25 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    detail = XtbCandidateArtifact(
        rank=1,
        kind="ts_guess",
        path=str(ts),
        selected=True,
        metadata={
            "geometry_valid": False,
            "geometry_validation": {"valid": False, "reasons": ["rearranged: 2 bond changes"]},
        },
    )
    return XtbArtifactContract(
        job_id="xtb-1",
        job_type="path_search",
        status="completed",
        reason="completed",
        job_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        candidate_details=(detail,),
        selected_candidate_paths=(str(ts),),
    )


def test_reaction_ts_guess_error_reports_geometry_invalid(tmp_path: Path) -> None:
    error = reaction_ts_guess_error_impl(_invalid_ts_contract(tmp_path))
    assert error["reason"] == "xtb_ts_guess_geometry_invalid"
    assert "rearranged: 2 bond changes" in error["message"]


def test_xtb_handoff_status_rejects_geometry_invalid_candidate(tmp_path: Path) -> None:
    status = xtb_handoff_status_impl(_invalid_ts_contract(tmp_path))
    assert status["status"] == "failed"
    assert status["reason"] == "xtb_ts_guess_geometry_invalid"


def test_xtb_handoff_status_keeps_valid_lower_ranked_guess(tmp_path: Path) -> None:
    """A geometry-invalid top-ranked guess must not hide a valid runner-up."""
    invalid = _invalid_ts_contract(tmp_path)
    runner_up_path = _write(
        tmp_path / "xtbpath_ts_2.xyz",
        ["C 0.0 0.0 0.0", "H 1.25 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )
    runner_up = XtbCandidateArtifact(
        rank=2,
        kind="ts_guess",
        path=str(runner_up_path),
        selected=True,
        metadata={"geometry_valid": True},
    )
    contract = replace(
        invalid,
        candidate_details=(*invalid.candidate_details, runner_up),
        selected_candidate_paths=(*invalid.selected_candidate_paths, str(runner_up_path)),
    )

    status = xtb_handoff_status_impl(contract)
    assert status["status"] == "ready"
    assert status["artifact_path"] == str(runner_up_path)
