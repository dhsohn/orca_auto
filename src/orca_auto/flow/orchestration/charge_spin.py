"""Charge/spin injection for CREST and xTB job manifests.

The workflow request carries ``charge``/``multiplicity`` for the ORCA stages;
CREST and xTB read ``charge``/``uhf`` from their job manifests instead. Every
place that builds (or rebuilds) such a manifest — template creation, reaction
xTB materialization, and flow.yaml restarts — must inject the same values, or
a charged/open-shell workflow silently screens conformers and paths on the
neutral-singlet surface. One shared helper keeps those siblings identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def uhf_from_multiplicity(value: Any) -> int:
    """Number of unpaired electrons for a spin multiplicity (2S+1)."""
    try:
        multiplicity = int(value)
    except (TypeError, ValueError):
        multiplicity = 1
    return max(0, multiplicity - 1)


def manifest_with_charge_spin(
    *,
    charge: Any,
    multiplicity: Any,
    manifest_overrides: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Manifest overrides with ``charge``/``uhf`` injected beneath user keys.

    Zero values are omitted (the engines' ``zero_is_absent`` reading treats
    them as defaults anyway) and explicit user overrides always win. Returns
    ``None`` when nothing remains, matching the builders' "no overrides"
    convention.
    """
    try:
        charge_value = int(charge)
    except (TypeError, ValueError):
        charge_value = 0
    resolved: dict[str, Any] = {}
    if charge_value != 0:
        resolved["charge"] = charge_value
    uhf_value = uhf_from_multiplicity(multiplicity)
    if uhf_value != 0:
        resolved["uhf"] = uhf_value
    resolved.update(dict(manifest_overrides or {}))
    return resolved or None


__all__ = ["manifest_with_charge_spin", "uhf_from_multiplicity"]
