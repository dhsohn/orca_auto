"""Atom identity, order and normal-mode association on AUTHENTIC ORCA 6.1.1 outputs.

Fixtures: ``tests/fixtures/orca_6_1_1`` (``provenance.json``: bounded
repository acceptance calculations, ORCA 6.1.1, HF/STO-3G, original bytes
retained). Each test first checks the recorded SHA-256 and size, so it only
ever pins these supported bytes.

Two readers parse one decoded output: the result parser (``result.coordinates``)
and the frequency scanner (``analysis.atoms`` and the normal-mode matrix). A
shared snapshot or an equal atom count does not prove they describe the same
geometry block, so every expectation below is an independent literal copied
from the fixture's final ``CARTESIAN COORDINATES (ANGSTROEM)`` and
``NORMAL MODES`` blocks. The row and column counts are observations of these
two outputs only; nothing here assumes that every Hessian prints 3N modes or
3N matrix rows, and Nimag is pinned from frequency values, not from atom
counts. No synthetic or malformed row is certified as reachable engine output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.completion_rules import route_facts
from orca_auto.orca.evidence import collect_structure_evidence, parsed_final_output
from orca_auto.orca.frequencies import mode_summaries
from orca_auto.orca.report.si import render_si_block_md

FIXTURES = Path(__file__).parents[1] / "fixtures" / "orca_6_1_1"

# Final geometry blocks, copied from the fixture bytes.
_WATER_ATOMS = (
    ("O", 0.066935, 0.000000, 0.066935),
    ("H", -0.019526, 0.000000, 1.052591),
    ("H", 1.052591, 0.000000, -0.019526),
)
_NH3_ATOMS = (
    ("N", 0.000000, 0.000000, 0.000000),
    ("H", 1.005477, -0.000000, -0.000000),
    ("H", -0.502738, 0.870768, -0.000000),
    ("H", -0.502738, -0.870768, -0.000000),
)
# Observed in these two outputs only (frequency lines and NORMAL MODES rows/columns).
_OBSERVED: dict[str, dict[str, Any]] = {
    "water_opt_freq": {"atoms": _WATER_ATOMS, "modes": 9, "matrix_rows": 9, "imaginary": 0},
    "nh3_ts_irc": {"atoms": _NH3_ATOMS, "modes": 12, "matrix_rows": 12, "imaginary": 1},
}


def _authentic_output(case: str) -> Path:
    manifest = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
    assert manifest["engine"] == "ORCA 6.1.1"
    for filename, recorded in manifest["cases"][case]["files"].items():
        data = (FIXTURES / filename).read_bytes()
        assert hashlib.sha256(data).hexdigest() == recorded["sha256"], filename
        assert len(data) == recorded["bytes"], filename
    return FIXTURES / f"{case}.out"


def _assert_atoms(actual: Any, expected: tuple[tuple[str, float, float, float], ...]) -> None:
    assert [atom[0] for atom in actual] == [atom[0] for atom in expected]
    for got, want in zip(actual, expected, strict=True):
        assert got[1:] == pytest.approx(want[1:], abs=1e-9)


@pytest.mark.parametrize("case", sorted(_OBSERVED))
def test_both_readers_take_one_final_geometry_in_one_atom_order(case: str) -> None:
    observed = _OBSERVED[case]
    result, analysis = parsed_final_output(_authentic_output(case))

    assert analysis is not None
    _assert_atoms(result.coordinates, observed["atoms"])
    _assert_atoms(analysis.atoms, observed["atoms"])
    assert len(analysis.frequencies) == observed["modes"]
    assert analysis.imaginary_count() == observed["imaginary"]
    # Matrix rows are Cartesian displacements in atom order (x, y, z per atom)
    # and columns are mode indices, as printed in these outputs.
    assert sorted(analysis.mode_matrix) == list(range(observed["matrix_rows"]))
    for row in analysis.mode_matrix.values():
        assert sorted(row) == list(range(observed["modes"]))


def test_authentic_ts_imaginary_mode_is_carried_by_the_listed_atoms() -> None:
    _result, analysis = parsed_final_output(_authentic_output("nh3_ts_irc"))
    assert analysis is not None

    # NORMAL MODES column 6, rows 0..11, copied from the fixture.
    assert analysis.mode_vector(6) == pytest.approx(
        [0.0, 0.0, -0.123688, 0.0, 0.0, 0.572917, 0.0, 0.0, 0.572917, 0.0, 0.0, 0.572917],
        abs=1e-9,
    )
    (summary,) = mode_summaries(analysis, None)
    assert summary.mode_index == 6
    assert summary.frequency_cm == pytest.approx(-1081.29)
    assert summary.imaginary is True
    hydrogens, nitrogen = summary.top_atoms[:3], summary.top_atoms[3]
    assert {entry.atom_index for entry in hydrogens} == {1, 2, 3}
    assert {entry.element for entry in hydrogens} == {"H"}
    assert all(entry.displacement == pytest.approx(0.572917) for entry in hydrogens)
    assert (nitrogen.atom_index, nitrogen.element) == (0, "N")
    assert nitrogen.displacement == pytest.approx(0.123688)


def test_authentic_minimum_lowest_mode_names_its_atoms() -> None:
    _result, analysis = parsed_final_output(_authentic_output("water_opt_freq"))
    assert analysis is not None

    (summary,) = mode_summaries(analysis, None)
    assert summary.mode_index == 6
    assert summary.frequency_cm == pytest.approx(2169.88)
    assert summary.imaginary is False
    hydrogens, oxygen = summary.top_atoms[:2], summary.top_atoms[2]
    assert {entry.atom_index for entry in hydrogens} == {1, 2}
    assert {entry.element for entry in hydrogens} == {"H"}
    # |(0.702104, 0, 0.068501)| and |(-0.048551, 0, -0.048551)|.
    assert all(entry.displacement == pytest.approx(0.705437, rel=1e-5) for entry in hydrogens)
    assert (oxygen.atom_index, oxygen.element) == (0, "O")
    assert oxygen.displacement == pytest.approx(0.068661, rel=1e-4)


def test_authentic_si_coordinates_follow_the_mode_atom_order(tmp_path: Path) -> None:
    out_path = _authentic_output("water_opt_freq")
    state = {
        "status": "completed",
        "selected_inp": str(FIXTURES / "water_opt_freq.inp"),
        "final_result": {"last_out_path": str(out_path)},
    }

    block = collect_structure_evidence(
        tmp_path / "water", state, route_facts(FIXTURES / "water_opt_freq.inp")
    )

    assert block is not None
    assert block.analysis is not None
    _assert_atoms(block.analysis.atoms, _WATER_ATOMS)
    rendered = render_si_block_md(block).splitlines()
    assert "Nimag = 0" in rendered
    # The block ends with the coordinate rows (splitlines drops the final newline).
    assert rendered[-3:] == [
        "O       0.066935     0.000000     0.066935",
        "H      -0.019526     0.000000     1.052591",
        "H       1.052591     0.000000    -0.019526",
    ]
