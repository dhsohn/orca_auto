"""Heavy-atom RMSD and post-DFT re-deduplication grouping tests."""

from __future__ import annotations

import math

import pytest

from orca_auto.orca.report.rmsd import (
    RmsdCandidate,
    group_by_rmsd,
    heavy_atom_rmsd,
)

AtomRow = tuple[str, float, float, float]


def _rotate_z(atoms: list[AtomRow], degrees: float) -> list[AtomRow]:
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    return [(el, x * cos - y * sin, x * sin + y * cos, z) for el, x, y, z in atoms]


def _translate(atoms: list[AtomRow], dx: float, dy: float, dz: float) -> list[AtomRow]:
    return [(el, x + dx, y + dy, z + dz) for el, x, y, z in atoms]


_CHIRAL: list[AtomRow] = [
    ("C", 0.0, 0.0, 0.0),
    ("F", 1.1, 0.0, 0.0),
    ("Cl", 0.0, 1.2, 0.0),
    ("Br", 0.0, 0.0, 1.3),
]
_COMPARISON_KEY = ("same-state-and-level",)


def test_identical_geometry_has_zero_rmsd() -> None:
    assert heavy_atom_rmsd(_CHIRAL, _CHIRAL) == 0.0


def test_rigid_rotation_and_translation_is_zero_rmsd() -> None:
    moved = _translate(_rotate_z(_CHIRAL, 73.0), 5.0, -3.0, 2.0)
    rmsd = heavy_atom_rmsd(_CHIRAL, moved)
    assert rmsd is not None
    assert rmsd < 1e-6


def test_mirror_image_is_not_merged_by_proper_rotation() -> None:
    # Reflection through the yz-plane produces the enantiomer; a proper-rotation
    # alignment cannot superpose it, so the RMSD stays clearly non-zero.
    mirror = [(el, -x, y, z) for el, x, y, z in _CHIRAL]
    rmsd = heavy_atom_rmsd(_CHIRAL, mirror)
    assert rmsd is not None
    assert rmsd > 0.1


def test_hydrogen_positions_are_ignored_when_heavy_only() -> None:
    base: list[AtomRow] = [("C", 0.0, 0.0, 0.0), ("O", 1.4, 0.0, 0.0), ("H", 2.0, 0.6, 0.0)]
    moved_h: list[AtomRow] = [("C", 0.0, 0.0, 0.0), ("O", 1.4, 0.0, 0.0), ("H", 2.0, -0.6, 0.3)]
    assert heavy_atom_rmsd(base, moved_h, heavy_atoms_only=True) == 0.0
    with_h = heavy_atom_rmsd(base, moved_h, heavy_atoms_only=False)
    assert with_h is not None and with_h > 0.0


def test_different_elements_do_not_compare() -> None:
    other: list[AtomRow] = [
        ("C", 0.0, 0.0, 0.0),
        ("F", 1.1, 0.0, 0.0),
        ("Cl", 0.0, 1.2, 0.0),
        ("N", 0.0, 0.0, 1.3),
    ]
    assert heavy_atom_rmsd(_CHIRAL, other) is None


def test_different_atom_counts_do_not_compare() -> None:
    shorter: list[AtomRow] = [("C", 0.0, 0.0, 0.0), ("F", 1.1, 0.0, 0.0)]
    assert heavy_atom_rmsd(_CHIRAL, shorter) is None


def test_non_finite_coordinates_fail_closed_and_never_merge() -> None:
    corrupt: list[AtomRow] = [
        ("C", math.nan, 0.0, 0.0),
        ("F", 1.1, 0.0, 0.0),
        ("Cl", 0.0, 1.2, 0.0),
        ("Br", 0.0, 0.0, 1.3),
    ]
    # A non-finite geometry cannot be compared: return None (never a spurious 0.0).
    assert heavy_atom_rmsd(_CHIRAL, corrupt) is None
    grouping = group_by_rmsd(
        [
            RmsdCandidate("good", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
            RmsdCandidate("corrupt", tuple(corrupt), -100.0, _COMPARISON_KEY),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    # The corrupt candidate is not merged away on a bogus zero RMSD.
    assert len(grouping.groups) == 2


def test_non_finite_energy_fails_closed_and_never_merges() -> None:
    grouping = group_by_rmsd(
        [
            RmsdCandidate("good", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
            RmsdCandidate("corrupt", tuple(_CHIRAL), math.nan, _COMPARISON_KEY),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 2


def test_all_atom_default_keeps_hydrogen_defined_mirror_minima_distinct() -> None:
    left: list[AtomRow] = [
        ("N", 0.0, 0.0, 0.0),
        ("C", 1.0, 0.0, 0.0),
        ("O", 0.0, 1.0, 0.0),
        ("H", 0.0, 0.0, 1.0),
    ]
    right: list[AtomRow] = [
        ("N", 0.0, 0.0, 0.0),
        ("C", 1.0, 0.0, 0.0),
        ("O", 0.0, 1.0, 0.0),
        ("H", 0.0, 0.0, -1.0),
    ]
    assert heavy_atom_rmsd(left, right, heavy_atoms_only=True) == 0.0
    all_atom = heavy_atom_rmsd(left, right)
    assert all_atom is not None and all_atom > 0.25
    grouping = group_by_rmsd(
        [
            RmsdCandidate("left", tuple(left), -10.0, _COMPARISON_KEY),
            RmsdCandidate("right", tuple(right), -10.0, _COMPARISON_KEY),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 2


def test_group_by_rmsd_merges_duplicates_keeping_lowest_energy() -> None:
    duplicate = _translate(_rotate_z(_CHIRAL, 20.0), 1.0, 1.0, 1.0)
    candidates = [
        RmsdCandidate("high", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
        RmsdCandidate("low", tuple(duplicate), -100.0005, _COMPARISON_KEY),
    ]
    grouping = group_by_rmsd(
        candidates,
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 1
    group = grouping.groups[0]
    assert group.representative_stage_id == "low"  # lower energy wins
    assert group.degeneracy == 2
    assert group.merged_stage_ids == ("high",)


def test_energy_window_keeps_distant_in_energy_structures_distinct() -> None:
    duplicate = _rotate_z(_CHIRAL, 15.0)
    candidates = [
        RmsdCandidate("a", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
        RmsdCandidate("b", tuple(duplicate), -100.0, _COMPARISON_KEY),  # 0 kcal apart
        RmsdCandidate("c", tuple(duplicate), -99.0, _COMPARISON_KEY),  # ~627 kcal apart
    ]
    grouping = group_by_rmsd(
        candidates,
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=0.5,
    )
    # a and b merge (same energy, ~0 RMSD); c stays distinct on the energy window.
    reps = grouping.representative_ids
    assert "c" in reps
    group_ab = grouping.group_for("a")
    assert group_ab is not None and group_ab.degeneracy == 2 and "b" in group_ab.member_stage_ids


def test_candidate_without_energy_is_never_merged() -> None:
    duplicate = _rotate_z(_CHIRAL, 10.0)
    candidates = [
        RmsdCandidate("has_energy", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
        RmsdCandidate("no_energy", tuple(duplicate), None, _COMPARISON_KEY),
    ]
    grouping = group_by_rmsd(
        candidates,
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 2
    assert all(group.degeneracy == 1 for group in grouping.groups)


def test_grouping_is_deterministic_for_equal_energies() -> None:
    duplicate = _rotate_z(_CHIRAL, 5.0)
    forward = [
        RmsdCandidate("zeta", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
        RmsdCandidate("alpha", tuple(duplicate), -100.0, _COMPARISON_KEY),
    ]
    reversed_order = list(reversed(forward))
    grouping_a = group_by_rmsd(forward, rmsd_threshold_angstrom=0.25, energy_window_kcal=1.0)
    grouping_b = group_by_rmsd(reversed_order, rmsd_threshold_angstrom=0.25, energy_window_kcal=1.0)
    assert grouping_a.groups == grouping_b.groups
    # Equal energy: the alphabetically first stage_id is the stable representative.
    assert grouping_a.groups[0].representative_stage_id == "alpha"


def test_comparison_key_mismatch_keeps_identical_geometries_distinct() -> None:
    grouping = group_by_rmsd(
        [
            RmsdCandidate("neutral", tuple(_CHIRAL), -100.0, ("q0-m1",)),
            RmsdCandidate("radical", tuple(_CHIRAL), -100.0, ("q1-m2",)),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 2


def test_local_motion_is_not_hidden_by_large_molecule_rmsd_dilution() -> None:
    scaffold: list[AtomRow] = [
        ("C", float(index % 11), float((index // 11) % 9), float(index // 99))
        for index in range(99)
    ]
    left = [*scaffold, ("H", 0.25, 0.25, 0.0)]
    right = [*scaffold, ("H", 0.25, 0.25, 2.0)]
    rmsd = heavy_atom_rmsd(left, right)
    assert rmsd is not None and rmsd < 0.25
    grouping = group_by_rmsd(
        [
            RmsdCandidate("left", tuple(left), -100.0, _COMPARISON_KEY),
            RmsdCandidate("right", tuple(right), -100.0, _COMPARISON_KEY),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 2


@pytest.mark.parametrize("height", [0.05, 0.000001])
def test_reflection_preferred_near_planar_pair_is_not_merged(height: float) -> None:
    left: list[AtomRow] = [
        ("N", 0.0, 0.0, 0.0),
        ("C", 1.0, 0.0, 0.0),
        ("O", 0.0, 1.0, 0.0),
        ("H", 0.25, 0.25, height),
    ]
    right = [*left[:-1], ("H", 0.25, 0.25, -height)]
    grouping = group_by_rmsd(
        [
            RmsdCandidate("left", tuple(left), -10.0, _COMPARISON_KEY),
            RmsdCandidate("right", tuple(right), -10.0, _COMPARISON_KEY),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    assert len(grouping.groups) == 2


def test_non_finite_grouping_thresholds_fail_closed_to_singletons() -> None:
    candidates = [
        RmsdCandidate("a", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
        RmsdCandidate("b", tuple(_CHIRAL), -100.0, _COMPARISON_KEY),
    ]
    for threshold, window in ((math.nan, 1.0), (0.25, math.nan)):
        grouping = group_by_rmsd(
            candidates,
            rmsd_threshold_angstrom=threshold,
            energy_window_kcal=window,
        )
        assert len(grouping.groups) == 2


def test_non_finite_candidate_cannot_disrupt_lowest_finite_representative() -> None:
    grouping = group_by_rmsd(
        [
            RmsdCandidate("finite_high", tuple(_CHIRAL), 0.0, _COMPARISON_KEY),
            RmsdCandidate("nonfinite", tuple(_CHIRAL), math.nan, _COMPARISON_KEY),
            RmsdCandidate("finite_low", tuple(_CHIRAL), -1.0, _COMPARISON_KEY),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1000.0,
    )
    finite_group = grouping.group_for("finite_low")
    nonfinite_group = grouping.group_for("nonfinite")
    assert finite_group is not None
    assert finite_group.representative_stage_id == "finite_low"
    assert finite_group.member_stage_ids == ("finite_high", "finite_low")
    assert nonfinite_group is not None and nonfinite_group.degeneracy == 1
