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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orca_auto.orca.parser import KCAL_PER_HARTREE

_ELEMENT_SYMBOLS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe "
    "Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In "
    "Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf "
    "Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm "
    "Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split()
_ATOMIC_NUMBER = {symbol: index for index, symbol in enumerate(_ELEMENT_SYMBOLS, start=1)}
_ATOMIC_NUMBER.update({"D": 1, "T": 1})


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


def validate_fragment_electronic_states(
    atom_symbols: Sequence[str], fragments: Sequence[Mapping[str, Any]]
) -> str:
    """Return a blocker for fragment states impossible for their electron counts."""
    for position, fragment in enumerate(fragments, start=1):
        raw_indices = fragment.get("atom_indices")
        if not isinstance(raw_indices, (list, tuple)):
            return f"fragment {position} atom_indices are unavailable"
        raw_charge = fragment.get("charge", 0)
        raw_multiplicity = fragment.get("multiplicity", 1)
        if (
            any(not isinstance(index, int) or isinstance(index, bool) for index in raw_indices)
            or not isinstance(raw_charge, int)
            or isinstance(raw_charge, bool)
            or not isinstance(raw_multiplicity, int)
            or isinstance(raw_multiplicity, bool)
        ):
            return f"fragment {position} has a malformed electronic state"
        indices = list(raw_indices)
        charge = raw_charge
        multiplicity = raw_multiplicity
        nuclear_charge = 0
        for index in indices:
            if index < 0 or index >= len(atom_symbols):
                return f"fragment {position} atom index {index} is outside the complex"
            symbol = str(atom_symbols[index]).strip().capitalize()
            atomic_number = _ATOMIC_NUMBER.get(symbol)
            if atomic_number is None:
                return f"fragment {position} uses unsupported element symbol {symbol or '?'}"
            nuclear_charge += atomic_number
        electrons = nuclear_charge - charge
        if electrons < 0:
            return (
                f"fragment {position} charge {charge} leaves a negative electron count "
                f"({electrons})"
            )
        doubled_spin = multiplicity - 1
        if multiplicity < 1 or doubled_spin > electrons:
            return (
                f"fragment {position} multiplicity {multiplicity} is impossible for "
                f"{electrons} electron(s)"
            )
        if doubled_spin % 2 != electrons % 2:
            return (
                f"fragment {position} multiplicity {multiplicity} has the wrong parity for "
                f"{electrons} electron(s)"
            )
    return ""


@dataclass(frozen=True)
class InteractionFragmentEnergy:
    """One fragment's contribution to an interaction energy."""

    label: str
    stage_id: str
    charge: int
    multiplicity: int
    energy_hartree: float | None
    atom_indices: tuple[int, ...] = ()
    formula: str = ""


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
    complex_formula: str = ""
    method: str = ""
    basis_set: str = ""
    solvation: str = ""
    orca_version: str = ""
    input_line: str = ""
    parent_stage_id: str = ""

    @property
    def resolved(self) -> bool:
        return self.de_int_hartree is not None


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def interaction_electronic_state_mismatch_reason(
    complex_charge: int,
    complex_multiplicity: int,
    fragment_states: Sequence[tuple[int, int]],
) -> str:
    """Return a hard blocker when charge or spin coupling is inconsistent."""
    charge_sum = sum(charge for charge, _multiplicity in fragment_states)
    if charge_sum != complex_charge:
        return (
            f"fragment charges sum to {charge_sum}, not the complex charge {complex_charge}; "
            "electron count is not conserved"
        )
    multiplicities = [multiplicity for _charge, multiplicity in fragment_states]
    if complex_multiplicity < 1 or any(multiplicity < 1 for multiplicity in multiplicities):
        return "complex and fragment multiplicities must be positive integers"

    # Work in doubled-spin units, where 2S = multiplicity - 1. The generalized
    # triangle rule yields one parity-preserving interval; checking its bounds
    # is O(number of fragments) and never materializes a multiplicity-sized set.
    doubled_spins = [multiplicity - 1 for multiplicity in multiplicities]
    maximum_doubled_spin = sum(doubled_spins)
    largest_fragment_spin = max(doubled_spins, default=0)
    minimum_doubled_spin = max(
        2 * largest_fragment_spin - maximum_doubled_spin,
        maximum_doubled_spin % 2,
    )
    complex_doubled_spin = complex_multiplicity - 1
    if (
        complex_doubled_spin < minimum_doubled_spin
        or complex_doubled_spin > maximum_doubled_spin
        or (complex_doubled_spin - maximum_doubled_spin) % 2 != 0
    ):
        return (
            f"complex multiplicity {complex_multiplicity} cannot be formed by coupling "
            f"fragment multiplicities {multiplicities}"
        )
    return ""


def compute_interaction_energy(
    *,
    complex_stage_id: str,
    complex_label: str,
    complex_charge: int,
    complex_multiplicity: int,
    complex_energy_hartree: float | None,
    fragments: Sequence[InteractionFragmentEnergy],
    blocker: str = "",
    complex_formula: str = "",
    method: str = "",
    basis_set: str = "",
    solvation: str = "",
    orca_version: str = "",
    input_line: str = "",
    parent_stage_id: str = "",
) -> InteractionEnergyResult:
    """Combine the complex and fragment single-point energies into ΔE_int.

    ``de_int_hartree`` is ``None`` (never a partial sum) unless the complex and
    every fragment carry a finite energy.
    """
    fragments = tuple(fragments)
    state_blocker = interaction_electronic_state_mismatch_reason(
        complex_charge,
        complex_multiplicity,
        [(fragment.charge, fragment.multiplicity) for fragment in fragments],
    )
    hard_blocker = "; ".join(part for part in (blocker, state_blocker) if part)
    common_tail = (
        complex_formula,
        method,
        basis_set,
        solvation,
        orca_version,
        input_line,
        parent_stage_id,
    )
    if hard_blocker:
        return InteractionEnergyResult(
            complex_stage_id,
            complex_label,
            complex_charge,
            complex_multiplicity,
            complex_energy_hartree,
            fragments,
            None,
            None,
            hard_blocker,
            *common_tail,
        )
    if len(fragments) < 2:
        reason = "at least two fragments are required for an interaction energy"
        return InteractionEnergyResult(
            complex_stage_id,
            complex_label,
            complex_charge,
            complex_multiplicity,
            complex_energy_hartree,
            fragments,
            None,
            None,
            reason,
            *common_tail,
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
            reason,
            *common_tail,
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
            "interaction energy is not finite",
            *common_tail,
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
        "",
        *common_tail,
    )


__all__ = [
    "InteractionEnergyResult",
    "InteractionFragmentEnergy",
    "compute_interaction_energy",
    "interaction_electronic_state_mismatch_reason",
    "validate_fragment_electronic_states",
    "validate_fragment_partition",
]
