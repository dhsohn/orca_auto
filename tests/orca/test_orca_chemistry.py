from __future__ import annotations

import pytest

from orca_auto.orca.orca_chemistry import build_formula


@pytest.mark.parametrize(
    ("atoms", "expected"),
    [
        pytest.param(["O", "H", "H", "C"], "CH2O", id="carbon_first"),
        pytest.param(["Na", "Cl"], "ClNa", id="no_carbon_alphabetical"),
        pytest.param(["O", "H", "H"], "H2O", id="no_carbon_hydrogen_alphabetical"),
        pytest.param(["Fe", "O", "O", "O", "O"], "FeO4", id="no_carbon_metal_first"),
        pytest.param(["C", "Cl", "H", "N", "Br", "O"], "CHBrClNO", id="carbon_then_alphabetical"),
        pytest.param(["C", "O", "O"], "CO2", id="carbon_without_hydrogen"),
        pytest.param([], "", id="empty"),
        pytest.param(["C"], "C", id="single_element_no_count"),
        pytest.param(["C", "C", "C"], "C3", id="multiple_same"),
        pytest.param(["C"] * 8 + ["H"] * 10 + ["O"] * 2, "C8H10O2", id="complex_molecule"),
    ],
)
def test_build_formula_is_hill_order(atoms: list[str], expected: str) -> None:
    assert build_formula(atoms) == expected
