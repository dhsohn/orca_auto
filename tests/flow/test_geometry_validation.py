from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.flow.contracts.xtb import (
    XtbArtifactContract,
    XtbCandidateArtifact,
    geometry_validation_passed,
)
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


def _shift_endpoints(tmp_path: Path) -> tuple[Path, Path]:
    """Intramolecular 1,2-H shift: Hm migrates from C0 to O2. Endpoints are one fragment."""
    reactant = _write(
        tmp_path / "shift_r.xyz",
        ["C 0.0 0.0 0.0", "H -1.05 0.0 0.0", "O 1.43 0.0 0.0", "H 2.38 0.0 0.0"],
    )
    product = _write(
        tmp_path / "shift_p.xyz",
        ["C 0.0 0.0 0.0", "H 1.43 0.95 0.0", "O 1.43 0.0 0.0", "H 2.38 0.0 0.0"],
    )
    return reactant, product


def test_validate_accepts_mid_transfer_atom_bonded_to_neither(tmp_path: Path) -> None:
    # The migrating H sits beyond the plain bond cutoff of both partners but is
    # still within reach of the carbon, so its reacting bond keeps it attached.
    reactant, product = _shift_endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 0.4 1.35 0.0", "O 1.43 0.0 0.0", "H 2.38 0.0 0.0"],
    )
    verdict = validate_ts_guess_geometry(
        ts_guess_xyz=ts, reactant_xyz=reactant, product_xyz=product
    )
    assert verdict.valid
    assert verdict.metrics["ts_fragments"] == 1
    assert verdict.metrics["intact_reacting_bond_count"] == 1


def test_validate_rejects_dissociation_along_a_reacting_bond(tmp_path: Path) -> None:
    """A guess whose only missing bond is a reacting one must still read as fragmented.

    Every bond change here lies inside the reaction's bond set, so the spurious
    rearrangement rule cannot see it. Counting a severed reacting bond as a
    connection would let this dissociated guess through to ORCA.
    """
    reactant, product = _shift_endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 0.0 8.0 0.0", "O 1.43 0.0 0.0", "H 2.38 0.0 0.0"],
    )
    verdict = validate_ts_guess_geometry(
        ts_guess_xyz=ts, reactant_xyz=reactant, product_xyz=product
    )
    assert not verdict.valid
    assert any("fragmented" in reason for reason in verdict.reasons)
    assert not any("rearranged" in reason for reason in verdict.reasons)
    assert verdict.metrics["spurious_bond_changes_vs_reactant"] == 0
    assert verdict.metrics["intact_reacting_bond_count"] == 0


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

    # stretch scale is manifest-tunable and reaches the validator
    tuned = _ts_guess_validation_fields(
        str(ts), input_summary, {"ts_guess_validation": {"reacting_bond_stretch_scale": 2.5}}
    )
    assert tuned is not None
    assert tuned["geometry_validation"]["reacting_bond_stretch_scale"] == 2.5
    # incompatible comparison -> fail-closed invalid verdict
    bad = _write(tmp_path / "bad.xyz", ["C 0.0 0.0 0.0"])
    fields = _ts_guess_validation_fields(str(bad), input_summary, {})
    assert fields is not None
    assert fields["geometry_valid"] is False
    assert "error" in fields["geometry_validation"]


def test_ts_guess_validation_rejects_multiframe_geometry(tmp_path: Path) -> None:
    reactant, product = _endpoints(tmp_path)
    frame = ["C 0.0 0.0 0.0", "H 1.25 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"]
    ts = tmp_path / "multi_ts.xyz"
    ts.write_text(
        "\n".join(["4", "first", *frame, "4", "second", *frame, ""]),
        encoding="utf-8",
    )

    fields = _ts_guess_validation_fields(
        str(ts), {"reactant_xyz": str(reactant), "product_xyz": str(product)}, {}
    )

    assert fields is not None
    assert fields["geometry_valid"] is False
    assert "exactly one" in fields["geometry_validation"]["error"]


@pytest.mark.parametrize(
    "options",
    [
        {"bond_scale": 0},
        {"bond_scale": float("nan")},
        {"bond_scale": float("inf")},
        {"bond_scale": True},
        {"reacting_bond_stretch_scale": 0},
        {"reacting_bond_stretch_scale": float("nan")},
        {"max_spurious_bond_changes": -1},
        {"max_spurious_bond_changes": 0.5},
        {"max_spurious_bond_changes": True},
    ],
)
def test_ts_guess_validation_rejects_lossy_or_nonphysical_options(
    tmp_path: Path,
    options: dict[str, object],
) -> None:
    reactant, product = _endpoints(tmp_path)
    ts = _write(
        tmp_path / "ts.xyz",
        ["C 0.0 0.0 0.0", "H 1.25 0.0 0.0", "O 4.0 0.0 0.0", "H 4.95 0.0 0.0"],
    )

    fields = _ts_guess_validation_fields(
        str(ts),
        {"reactant_xyz": str(reactant), "product_xyz": str(product)},
        {"ts_guess_validation": options},
    )

    assert fields is not None
    assert fields["geometry_valid"] is False
    assert fields["geometry_validation"]["valid"] is False
    assert "must be" in fields["geometry_validation"]["error"]


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


def test_xtb_handoff_status_rejects_candidate_without_geometry_verdict(tmp_path: Path) -> None:
    """A syntactically valid XYZ is not enough to authorize an ORCA handoff."""
    contract = _invalid_ts_contract(tmp_path)
    candidate = replace(contract.candidate_details[0], metadata={})
    contract = replace(contract, candidate_details=(candidate,))

    status = xtb_handoff_status_impl(contract)

    assert status["status"] == "failed"
    assert status["reason"] == "xtb_ts_guess_geometry_unvalidated"
    assert status["artifact_path"] == ""


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
        metadata={
            "geometry_valid": True,
            "geometry_validation": {"valid": True, "reasons": []},
        },
    )
    contract = replace(
        invalid,
        candidate_details=(*invalid.candidate_details, runner_up),
        selected_candidate_paths=(*invalid.selected_candidate_paths, str(runner_up_path)),
    )

    status = xtb_handoff_status_impl(contract)
    assert status["status"] == "ready"
    assert status["artifact_path"] == str(runner_up_path)


@pytest.mark.parametrize(
    ("metadata", "passed"),
    [
        ({"geometry_valid": True, "geometry_validation": {"valid": True, "reasons": []}}, True),
        ({}, False),
        ({"geometry_valid": True}, False),
        ({"geometry_valid": True, "geometry_validation": {"valid": True}}, False),
        ({"geometry_valid": False, "geometry_validation": {"valid": True, "reasons": []}}, False),
        ({"geometry_valid": True, "geometry_validation": {"valid": False, "reasons": []}}, False),
        (
            {"geometry_valid": True, "geometry_validation": {"valid": True, "reasons": ["rmsd"]}},
            False,
        ),
        (
            {
                "geometry_valid": True,
                "geometry_validation": {"valid": True, "reasons": [], "error": "probe failed"},
            },
            False,
        ),
        ({"geometry_valid": 1, "geometry_validation": {"valid": True, "reasons": []}}, False),
    ],
)
def test_geometry_validation_passed_requires_every_clause(
    metadata: dict[str, object],
    passed: bool,
) -> None:
    """The handoff gate accepts only an explicit, self-consistent verdict.

    One shared predicate backs both the accepting rule and the refusal-reason
    reporter; each clause here is load-bearing for the fail-closed direction.
    """
    assert geometry_validation_passed(metadata) is passed
