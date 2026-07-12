"""Admission-time validation of the interaction_energy / rmsd_dedup blocks."""

from __future__ import annotations

import pytest

from orca_auto.flow.manifest import (
    INTERACTION_ENERGY_MAX_FRAGMENTS_CAP,
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
)


def _valid_interaction() -> dict[str, object]:
    return {
        "enabled": True,
        "fragments": [
            {"atom_indices": [0, 1], "charge": 0, "multiplicity": 1, "label": "host"},
            {"atom_indices": [2], "charge": -1, "multiplicity": 2},
        ],
    }


def test_absent_or_disabled_interaction_energy_is_none() -> None:
    assert normalize_interaction_energy_block(None) is None
    assert normalize_interaction_energy_block({"enabled": False, "fragments": [{}]}) is None


def test_valid_interaction_energy_is_normalized_with_defaults() -> None:
    normalized = normalize_interaction_energy_block(_valid_interaction())
    assert normalized is not None
    assert normalized["enabled"] is True
    assert normalized["sp_route_line"] == "! r2scan-3c TightSCF"
    assert normalized["fragments"][1]["label"] == "fragment_2"  # default label
    assert normalized["fragments"][0]["atom_indices"] == [0, 1]


def test_enabled_interaction_energy_requires_fragments() -> None:
    with pytest.raises(ValueError, match="fragments"):
        normalize_interaction_energy_block({"enabled": True})


def test_interaction_energy_rejects_over_cap_max_fragments() -> None:
    block = _valid_interaction()
    block["max_fragments"] = INTERACTION_ENERGY_MAX_FRAGMENTS_CAP + 1
    with pytest.raises(ValueError, match="ceiling"):
        normalize_interaction_energy_block(block)


def test_interaction_energy_rejects_too_many_fragments() -> None:
    block = _valid_interaction()
    block["max_fragments"] = 1
    with pytest.raises(ValueError, match="max_fragments"):
        normalize_interaction_energy_block(block)


def test_interaction_energy_rejects_negative_atom_index() -> None:
    block = _valid_interaction()
    block["fragments"] = [{"atom_indices": [-1]}]
    with pytest.raises(ValueError, match="atom_indices"):
        normalize_interaction_energy_block(block)


def test_interaction_energy_rejects_bad_multiplicity() -> None:
    block = _valid_interaction()
    block["fragments"] = [{"atom_indices": [0], "multiplicity": 0}]
    with pytest.raises(ValueError, match="multiplicity"):
        normalize_interaction_energy_block(block)


def test_rmsd_dedup_defaults_are_finite_when_enabled() -> None:
    normalized = normalize_rmsd_dedup_block({"enabled": True})
    assert normalized is not None
    assert normalized["rmsd_threshold_angstrom"] == 0.25
    assert normalized["energy_window_kcal"] == 0.1
    assert normalized["heavy_atoms_only"] is True


def test_rmsd_dedup_absent_or_disabled_is_none() -> None:
    assert normalize_rmsd_dedup_block(None) is None
    assert normalize_rmsd_dedup_block({"enabled": False}) is None


def test_rmsd_dedup_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError, match="rmsd_threshold_angstrom"):
        normalize_rmsd_dedup_block({"enabled": True, "rmsd_threshold_angstrom": 0})


def test_run_dir_resolver_validates_for_every_template() -> None:
    from orca_auto.flow.run_dir.options import _resolve_run_dir_interaction_options

    # A malformed block fails closed at run-dir admission (any template), not
    # silently dropped.
    with pytest.raises(ValueError, match="atom_indices"):
        _resolve_run_dir_interaction_options(
            {"interaction_energy": {"enabled": True, "fragments": [{"atom_indices": [-1]}]}}
        )
    # A valid block is normalized; an absent one resolves to None.
    resolved = _resolve_run_dir_interaction_options({"interaction_energy": _valid_interaction()})
    assert resolved["interaction_energy"]["enabled"] is True
    assert resolved["rmsd_dedup"] is None
