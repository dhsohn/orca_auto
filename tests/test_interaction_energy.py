"""Interaction energy ΔE_int computation and fragment-partition tests."""

from __future__ import annotations

import math

from orca_auto.orca.report.interaction_energy import (
    InteractionFragmentEnergy,
    compute_interaction_energy,
    validate_fragment_partition,
)

_KCAL_PER_HARTREE = 627.5094740631


def _fragment(
    label: str, stage_id: str, charge: int, multiplicity: int, energy: float | None
) -> InteractionFragmentEnergy:
    return InteractionFragmentEnergy(label, stage_id, charge, multiplicity, energy)


def test_valid_partition_accepts_disjoint_exhaustive_fragments() -> None:
    assert validate_fragment_partition([[0, 1, 2], [3, 4, 5]], 6) == ""


def test_partition_rejects_a_gap() -> None:
    reason = validate_fragment_partition([[0, 1], [3, 4, 5]], 6)
    assert "missing" in reason and "2" in reason


def test_partition_rejects_overlap() -> None:
    reason = validate_fragment_partition([[0, 1, 2], [2, 3, 4, 5]], 6)
    assert "more than one fragment" in reason


def test_partition_rejects_out_of_range_index() -> None:
    reason = validate_fragment_partition([[0, 1, 2], [3, 4, 6]], 6)
    assert "outside" in reason


def test_partition_rejects_empty_fragment() -> None:
    reason = validate_fragment_partition([[0, 1, 2, 3, 4, 5], []], 6)
    assert "no atoms" in reason


def test_interaction_energy_is_complex_minus_sum_of_fragments() -> None:
    result = compute_interaction_energy(
        complex_stage_id="cx",
        complex_label="complex",
        complex_charge=0,
        complex_multiplicity=1,
        complex_energy_hartree=-100.0,
        fragments=[
            _fragment("A", "f1", 0, 1, -60.0),
            _fragment("B", "f2", 0, 1, -39.99),
        ],
    )
    assert result.resolved
    assert result.de_int_hartree is not None
    assert math.isclose(result.de_int_hartree, -100.0 - (-60.0 - 39.99), rel_tol=1e-12)
    assert result.de_int_kcalmol is not None
    assert math.isclose(
        result.de_int_kcalmol, result.de_int_hartree * _KCAL_PER_HARTREE, rel_tol=1e-12
    )
    assert result.note == ""


def test_missing_fragment_energy_fails_closed() -> None:
    result = compute_interaction_energy(
        complex_stage_id="cx",
        complex_label="complex",
        complex_charge=0,
        complex_multiplicity=1,
        complex_energy_hartree=-100.0,
        fragments=[
            _fragment("A", "f1", 0, 1, -60.0),
            _fragment("B", "f2", 0, 1, None),
        ],
    )
    assert not result.resolved
    assert result.de_int_hartree is None
    assert result.de_int_kcalmol is None
    assert "missing fragment single-point energy" in result.note


def test_missing_complex_energy_fails_closed() -> None:
    result = compute_interaction_energy(
        complex_stage_id="cx",
        complex_label="complex",
        complex_charge=0,
        complex_multiplicity=1,
        complex_energy_hartree=None,
        fragments=[_fragment("A", "f1", 0, 1, -60.0)],
    )
    assert not result.resolved
    assert "complex single point" in result.note


def test_charge_and_spin_mismatch_is_noted_but_still_computed() -> None:
    result = compute_interaction_energy(
        complex_stage_id="cx",
        complex_label="complex",
        complex_charge=0,
        complex_multiplicity=1,
        complex_energy_hartree=-100.0,
        fragments=[
            _fragment("cation", "f1", 1, 1, -60.0),
            _fragment("neutral", "f2", 0, 1, -39.99),
        ],
    )
    assert result.resolved  # still computed
    assert "fragment charges sum to 1" in result.note
