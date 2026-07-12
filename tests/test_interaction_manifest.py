"""Admission-time validation of the interaction_energy / rmsd_dedup blocks."""

from __future__ import annotations

import pytest

from orca_auto.flow.manifest import (
    INTERACTION_ENERGY_MAX_FRAGMENTS_CAP,
    INTERACTION_ENERGY_MAX_MULTIPLICITY,
    normalize_interaction_energy_block,
    normalize_rmsd_dedup_block,
    validate_interaction_energy_state_balance,
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
    block["fragments"] = [{"atom_indices": [-1]}, {"atom_indices": [0]}]
    with pytest.raises(ValueError, match="atom_indices"):
        normalize_interaction_energy_block(block)


def test_interaction_energy_rejects_bad_multiplicity() -> None:
    block = _valid_interaction()
    block["fragments"] = [
        {"atom_indices": [0], "multiplicity": 0},
        {"atom_indices": [1]},
    ]
    with pytest.raises(ValueError, match="multiplicity"):
        normalize_interaction_energy_block(block)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("max_fragments",), 2.9),
        (("fragments", 0, "atom_indices", 0), 0.9),
        (("fragments", 0, "charge"), -0.9),
        (("fragments", 0, "multiplicity"), 1.9),
    ],
)
def test_interaction_energy_rejects_non_integral_numbers(
    path: tuple[object, ...], value: object
) -> None:
    block = _valid_interaction()
    target: object = block
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(ValueError, match="integer"):
        normalize_interaction_energy_block(block)


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": "tru", "fragments": [{"atom_indices": [0]}]},
        {"enabled": True, "fragments": [{"atom_indices": [0], "multiplicty": 2}]},
        {"enabled": True, "fragments": [{"atom_indices": [0]}], "max_fragment": 2},
        {"enabled": True, "fragments": [{"atom_indices": [0]}], "sp_route_line": ["! HF"]},
    ],
)
def test_interaction_energy_rejects_malformed_or_unknown_fields(
    block: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        normalize_interaction_energy_block(block)


@pytest.mark.parametrize(
    "label",
    [
        "host\nH 99 98 97",
        "host\rguest",
        "host\x00guest",
        "host\u0085guest",
        "host\u2028guest",
        "host\u2029guest",
        "x" * 81,
    ],
)
def test_interaction_energy_rejects_unsafe_fragment_labels(label: str) -> None:
    block = _valid_interaction()
    block["fragments"] = [
        {"atom_indices": [0], "label": label},
        {"atom_indices": [1]},
    ]
    with pytest.raises(ValueError, match="label"):
        normalize_interaction_energy_block(block)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_interaction_energy_rejects_unicode_line_separators_in_route(separator: str) -> None:
    block = _valid_interaction()
    block["sp_route_line"] = f"! r2scan-3c{separator}! HF STO-3G"
    with pytest.raises(ValueError, match="sp_route_line"):
        normalize_interaction_energy_block(block)


def test_rmsd_dedup_defaults_are_finite_when_enabled() -> None:
    normalized = normalize_rmsd_dedup_block({"enabled": True})
    assert normalized is not None
    assert normalized["rmsd_threshold_angstrom"] == 0.25
    assert normalized["energy_window_kcal"] == 0.1
    assert normalized["heavy_atoms_only"] is False


def test_rmsd_dedup_absent_or_disabled_is_none() -> None:
    assert normalize_rmsd_dedup_block(None) is None
    assert normalize_rmsd_dedup_block({"enabled": False}) is None


def test_rmsd_dedup_rejects_non_positive_threshold() -> None:
    with pytest.raises(ValueError, match="rmsd_threshold_angstrom"):
        normalize_rmsd_dedup_block({"enabled": True, "rmsd_threshold_angstrom": 0})


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": "tru"},
        {"enabled": True, "heavy_atoms_only": "flase"},
        {"enabled": True, "energy_window_kcalmol": 1.0},
    ],
)
def test_rmsd_dedup_rejects_malformed_or_unknown_fields(block: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        normalize_rmsd_dedup_block(block)


def test_run_dir_resolver_validates_for_every_template() -> None:
    from orca_auto.flow.run_dir.options import _resolve_run_dir_interaction_options

    # A malformed block fails closed at run-dir admission (any template), not
    # silently dropped.
    with pytest.raises(ValueError, match="atom_indices"):
        _resolve_run_dir_interaction_options(
            {
                "interaction_energy": {
                    "enabled": True,
                    "fragments": [{"atom_indices": [-1]}, {"atom_indices": [0]}],
                }
            }
        )
    # A valid block is normalized; an absent one resolves to None.
    resolved = _resolve_run_dir_interaction_options({"interaction_energy": _valid_interaction()})
    assert resolved["interaction_energy"]["enabled"] is True
    assert resolved["rmsd_dedup"] is None


def test_interaction_energy_requires_two_disjoint_contiguous_fragments() -> None:
    with pytest.raises(ValueError, match="at least two"):
        normalize_interaction_energy_block({"enabled": True, "fragments": [{"atom_indices": [0]}]})
    for fragments in (
        [{"atom_indices": [0, 1]}, {"atom_indices": [1, 2]}],
        [{"atom_indices": [0]}, {"atom_indices": [2]}],
    ):
        with pytest.raises(ValueError, match="disjoint|contiguous"):
            normalize_interaction_energy_block({"enabled": True, "fragments": fragments})
    with pytest.raises(ValueError, match="contiguous"):
        normalize_interaction_energy_block(
            {
                "enabled": True,
                "fragments": [
                    {"atom_indices": [0]},
                    {"atom_indices": [10**12]},
                ],
            }
        )


def test_interaction_energy_caps_fragment_multiplicity() -> None:
    block = _valid_interaction()
    block["fragments"][1]["multiplicity"] = INTERACTION_ENERGY_MAX_MULTIPLICITY + 1  # type: ignore[index]
    with pytest.raises(ValueError, match="multiplicity.*ceiling"):
        normalize_interaction_energy_block(block)


@pytest.mark.parametrize(
    "route",
    [
        "! HF Opt",
        "! HF TightOpt",
        "! HF OptTS Freq",
        "! HF NumFreq",
        "! HF EnGrad",
        "! HF IRC",
        "! HF NEB-CI",
        "! HF MD",
        "! HF GOAT",
    ],
)
def test_interaction_energy_rejects_non_single_point_routes(route: str) -> None:
    block = _valid_interaction()
    block["sp_route_line"] = route
    with pytest.raises(ValueError, match="single-point"):
        normalize_interaction_energy_block(block)


def test_scan_density_functional_is_allowed_in_single_point_route() -> None:
    block = _valid_interaction()
    block["sp_route_line"] = "! SCAN def2-TZVP TightSCF"
    normalized = normalize_interaction_energy_block(block)
    assert normalized is not None
    assert normalized["sp_route_line"] == "! SCAN def2-TZVP TightSCF"


def test_interaction_state_balance_rejects_charge_and_impossible_spin() -> None:
    normalized = normalize_interaction_energy_block(_valid_interaction())
    assert normalized is not None
    with pytest.raises(ValueError, match="charges sum"):
        validate_interaction_energy_state_balance(
            normalized, complex_charge=0, complex_multiplicity=2
        )

    spin_block = normalize_interaction_energy_block(
        {
            "enabled": True,
            "fragments": [
                {"atom_indices": [0], "multiplicity": 2},
                {"atom_indices": [1], "multiplicity": 2},
            ],
        }
    )
    assert spin_block is not None
    with pytest.raises(ValueError, match="cannot be formed"):
        validate_interaction_energy_state_balance(
            spin_block, complex_charge=0, complex_multiplicity=5
        )
