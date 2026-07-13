"""Shared charge/uhf manifest injection used by builders and restarts."""

from __future__ import annotations

import pytest

from orca_auto.flow.orchestration.charge_spin import (
    manifest_with_charge_spin,
    uhf_from_multiplicity,
)


def test_uhf_from_multiplicity_counts_unpaired_electrons() -> None:
    assert uhf_from_multiplicity(1) == 0
    assert uhf_from_multiplicity(2) == 1
    assert uhf_from_multiplicity(3) == 2
    for value in ("bad", None, 0, 1.5, True):
        with pytest.raises(ValueError, match="multiplicity must"):
            uhf_from_multiplicity(value)


def test_neutral_singlet_injects_nothing() -> None:
    assert manifest_with_charge_spin(charge=0, multiplicity=1, manifest_overrides=None) is None
    assert manifest_with_charge_spin(charge=0, multiplicity=1, manifest_overrides={"gfn": 2}) == {
        "gfn": 2
    }


def test_charged_doublet_injects_beneath_user_keys() -> None:
    assert manifest_with_charge_spin(charge=-1, multiplicity=2, manifest_overrides={"gfn": 1}) == {
        "charge": -1,
        "uhf": 1,
        "gfn": 1,
    }


@pytest.mark.parametrize(
    ("charge", "multiplicity"),
    [(-0.5, 2), (0, 2.5), (True, 1), (0, False)],
)
def test_canonical_electronic_state_rejects_fractional_and_boolean_values(
    charge: object,
    multiplicity: object,
) -> None:
    with pytest.raises(ValueError, match=r"(?:charge|multiplicity) must be an integer"):
        manifest_with_charge_spin(
            charge=charge,
            multiplicity=multiplicity,
            manifest_overrides=None,
        )


def test_matching_explicit_engine_state_is_accepted_and_normalized() -> None:
    resolved = manifest_with_charge_spin(
        charge=-1,
        multiplicity=2,
        manifest_overrides={"charge": "-1", "uhf": 1, "gfn": 2},
    )
    assert resolved == {"charge": -1, "uhf": 1, "gfn": 2}


@pytest.mark.parametrize(
    "overrides",
    [
        {"charge": 2, "uhf": 1},
        {"charge": -1, "uhf": 4},
        {"charge": "invalid", "uhf": 1},
        {"charge": -1, "uhf": 1.5},
        {"charge": -1, "uhf": True},
    ],
)
def test_conflicting_or_malformed_engine_state_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"engine manifest (?:charge|uhf).*workflow"):
        manifest_with_charge_spin(
            charge=-1,
            multiplicity=2,
            manifest_overrides=overrides,
        )
