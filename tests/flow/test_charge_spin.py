"""Shared charge/uhf manifest injection used by builders and restarts."""

from __future__ import annotations

from orca_auto.flow.orchestration.charge_spin import (
    manifest_with_charge_spin,
    uhf_from_multiplicity,
)


def test_uhf_from_multiplicity_counts_unpaired_electrons() -> None:
    assert uhf_from_multiplicity(1) == 0
    assert uhf_from_multiplicity(2) == 1
    assert uhf_from_multiplicity(3) == 2
    assert uhf_from_multiplicity("bad") == 0
    assert uhf_from_multiplicity(None) == 0
    assert uhf_from_multiplicity(0) == 0


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


def test_explicit_user_overrides_always_win() -> None:
    resolved = manifest_with_charge_spin(
        charge=-1,
        multiplicity=2,
        manifest_overrides={"charge": 2, "uhf": 4},
    )
    assert resolved == {"charge": 2, "uhf": 4}
