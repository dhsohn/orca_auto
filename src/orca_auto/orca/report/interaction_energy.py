"""Interaction energy ΔE_int = E(complex) − Σ E(fragment_i).

The complex and every fragment are computed as fresh single points at the SAME
level of theory and the SAME (complex-optimized) geometry, so their energies
are directly subtractable. Fragments are defined generically by 0-based atom
indices plus a per-fragment charge and multiplicity; this module enforces only
that the fragments PARTITION the complex (disjoint and exhaustive over every
atom) — it attaches no paper-specific chemical meaning.

Everything is fail-closed: a missing or non-finite fragment energy yields a
``None`` interaction energy (never a partial sum), and a non-partitioning
fragment set is rejected before any energy is combined.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from orca_auto.orca.report.render import KCAL_PER_HARTREE


def validate_fragment_partition(fragment_atom_indices: Sequence[Sequence[int]], natoms: int) -> str:
    """Return ``""`` when the fragments partition ``range(natoms)``, else a reason.

    A partition requires every atom index to appear in exactly one fragment:
    disjoint fragments whose union is the whole molecule. A gap or an overlap
    would make ΔE_int a physically meaningless partial sum, so it is rejected.
    """
    if natoms <= 0:
        return "the complex has no atoms to partition"
    if not fragment_atom_indices:
        return "no fragments are defined"
    seen: set[int] = set()
    for position, indices in enumerate(fragment_atom_indices):
        if not indices:
            return f"fragment {position + 1} has no atoms"
        for index in indices:
            if not isinstance(index, int) or isinstance(index, bool):
                return f"fragment {position + 1} has a non-integer atom index {index!r}"
            if index < 0 or index >= natoms:
                return (
                    f"fragment {position + 1} atom index {index} is outside the "
                    f"0..{natoms - 1} range of the complex"
                )
            if index in seen:
                return f"atom index {index} appears in more than one fragment"
            seen.add(index)
    if len(seen) != natoms:
        missing = sorted(set(range(natoms)) - seen)
        return f"fragments do not cover every atom; missing indices {missing}"
    return ""


@dataclass(frozen=True)
class InteractionFragmentEnergy:
    """One fragment's contribution to an interaction energy."""

    label: str
    stage_id: str
    charge: int
    multiplicity: int
    energy_hartree: float | None


@dataclass(frozen=True)
class InteractionEnergyResult:
    """ΔE_int for one complex, or a fail-closed omission with a reason."""

    complex_stage_id: str
    complex_label: str
    complex_charge: int
    complex_multiplicity: int
    complex_energy_hartree: float | None
    fragments: tuple[InteractionFragmentEnergy, ...]
    de_int_hartree: float | None
    de_int_kcalmol: float | None
    note: str

    @property
    def resolved(self) -> bool:
        return self.de_int_hartree is not None


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _charge_spin_note(
    complex_charge: int,
    complex_multiplicity: int,
    fragments: Sequence[InteractionFragmentEnergy],
) -> str:
    """A soft ⚠ when fragment charge/spin do not add up to the complex.

    Fragment charge/multiplicity are user-supplied generic metadata; no
    chemistry-specific rule is enforced, but a mismatch is worth surfacing
    because it usually signals a mis-specified fragmentation.
    """
    notes: list[str] = []
    charge_sum = sum(fragment.charge for fragment in fragments)
    if charge_sum != complex_charge:
        notes.append(
            f"fragment charges sum to {charge_sum}, not the complex charge {complex_charge}"
        )
    unpaired_sum = sum(fragment.multiplicity - 1 for fragment in fragments)
    complex_unpaired = complex_multiplicity - 1
    if unpaired_sum != complex_unpaired:
        notes.append(
            f"fragment unpaired-electron counts sum to {unpaired_sum}, "
            f"not the complex's {complex_unpaired}"
        )
    return "; ".join(notes)


def compute_interaction_energy(
    *,
    complex_stage_id: str,
    complex_label: str,
    complex_charge: int,
    complex_multiplicity: int,
    complex_energy_hartree: float | None,
    fragments: Sequence[InteractionFragmentEnergy],
) -> InteractionEnergyResult:
    """Combine the complex and fragment single-point energies into ΔE_int.

    ``de_int_hartree`` is ``None`` (never a partial sum) unless the complex and
    every fragment carry a finite energy.
    """
    note = _charge_spin_note(complex_charge, complex_multiplicity, fragments)
    fragments = tuple(fragments)
    if not fragments:
        reason = "no fragments were computed"
        return InteractionEnergyResult(
            complex_stage_id,
            complex_label,
            complex_charge,
            complex_multiplicity,
            complex_energy_hartree,
            fragments,
            None,
            None,
            "; ".join(part for part in (note, reason) if part),
        )

    missing = [f.label or f.stage_id for f in fragments if not _finite(f.energy_hartree)]
    if not _finite(complex_energy_hartree) or missing:
        if not _finite(complex_energy_hartree):
            reason = "the complex single point has no finite energy"
        else:
            reason = "missing fragment single-point energy for " + ", ".join(missing)
        return InteractionEnergyResult(
            complex_stage_id,
            complex_label,
            complex_charge,
            complex_multiplicity,
            complex_energy_hartree,
            fragments,
            None,
            None,
            "; ".join(part for part in (note, reason) if part),
        )

    assert complex_energy_hartree is not None  # guarded by _finite above
    de_int = complex_energy_hartree - sum(
        f.energy_hartree for f in fragments if f.energy_hartree is not None
    )
    if not math.isfinite(de_int):
        return InteractionEnergyResult(
            complex_stage_id,
            complex_label,
            complex_charge,
            complex_multiplicity,
            complex_energy_hartree,
            fragments,
            None,
            None,
            "; ".join(part for part in (note, "interaction energy is not finite") if part),
        )
    return InteractionEnergyResult(
        complex_stage_id,
        complex_label,
        complex_charge,
        complex_multiplicity,
        complex_energy_hartree,
        fragments,
        de_int,
        de_int * KCAL_PER_HARTREE,
        note,
    )


__all__ = [
    "InteractionEnergyResult",
    "InteractionFragmentEnergy",
    "compute_interaction_energy",
    "validate_fragment_partition",
]
