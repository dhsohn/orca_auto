"""Heavy-atom RMSD and post-DFT re-deduplication grouping tests."""

from __future__ import annotations

import math

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
            RmsdCandidate("good", tuple(_CHIRAL), -100.0),
            RmsdCandidate("corrupt", tuple(corrupt), -100.0),
        ],
        rmsd_threshold_angstrom=0.25,
        energy_window_kcal=1.0,
    )
    # The corrupt candidate is not merged away on a bogus zero RMSD.
    assert len(grouping.groups) == 2


def test_group_by_rmsd_merges_duplicates_keeping_lowest_energy() -> None:
    duplicate = _translate(_rotate_z(_CHIRAL, 20.0), 1.0, 1.0, 1.0)
    candidates = [
        RmsdCandidate("high", tuple(_CHIRAL), -100.0),
        RmsdCandidate("low", tuple(duplicate), -100.0005),
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
        RmsdCandidate("a", tuple(_CHIRAL), -100.0),
        RmsdCandidate("b", tuple(duplicate), -100.0),  # 0 kcal apart
        RmsdCandidate("c", tuple(duplicate), -99.0),  # ~627 kcal apart
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
        RmsdCandidate("has_energy", tuple(_CHIRAL), -100.0),
        RmsdCandidate("no_energy", tuple(duplicate), None),
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
        RmsdCandidate("zeta", tuple(_CHIRAL), -100.0),
        RmsdCandidate("alpha", tuple(duplicate), -100.0),
    ]
    reversed_order = list(reversed(forward))
    grouping_a = group_by_rmsd(forward, rmsd_threshold_angstrom=0.25, energy_window_kcal=1.0)
    grouping_b = group_by_rmsd(reversed_order, rmsd_threshold_angstrom=0.25, energy_window_kcal=1.0)
    assert grouping_a.groups == grouping_b.groups
    # Equal energy: the alphabetically first stage_id is the stable representative.
    assert grouping_a.groups[0].representative_stage_id == "alpha"
