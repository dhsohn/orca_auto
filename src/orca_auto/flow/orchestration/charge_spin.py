"""Charge/spin injection for CREST and xTB job manifests.

The workflow request carries ``charge``/``multiplicity`` for the ORCA stages;
CREST and xTB read ``charge``/``uhf`` from their job manifests instead. Every
place that builds (or rebuilds) such a manifest — template creation, reaction
xTB materialization, and flow.yaml restarts — must inject the same values, or
a charged/open-shell workflow silently screens conformers and paths on the
neutral-singlet surface. One shared helper keeps those siblings identical.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

_INTEGER_TEXT_RE = re.compile(r"[-+]?\d+")


def strict_int(value: Any, *, field: str, minimum: int | None = None) -> int:
    """Parse an exact integer without truncating booleans or fractions."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer, not a boolean")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            constraint = f" >= {minimum}" if minimum is not None else ""
            raise ValueError(f"{field} must be an integer{constraint}. got={value!r}")
        parsed = int(value)
    elif isinstance(value, str) and _INTEGER_TEXT_RE.fullmatch(value.strip()):
        parsed = int(value.strip())
    else:
        constraint = f" >= {minimum}" if minimum is not None else ""
        raise ValueError(f"{field} must be an integer{constraint}. got={value!r}")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field} must be >= {minimum}. got={parsed}")
    return parsed


def uhf_from_multiplicity(value: Any) -> int:
    """Number of unpaired electrons for a spin multiplicity (2S+1)."""
    multiplicity = strict_int(value, field="multiplicity", minimum=1)
    return multiplicity - 1


def manifest_with_charge_spin(
    *,
    charge: Any,
    multiplicity: Any,
    manifest_overrides: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Manifest overrides with authoritative workflow ``charge``/``uhf``.

    Zero values are omitted. Explicit engine values may repeat the canonical
    workflow state, but a conflicting value is rejected before CREST/xTB can
    screen on a different potential-energy surface than downstream ORCA.
    Returns ``None`` when nothing remains, matching the builders' "no
    overrides" convention.
    """
    charge_value = 0 if charge in (None, "") else strict_int(charge, field="charge")
    uhf_value = uhf_from_multiplicity(1 if multiplicity in (None, "") else multiplicity)
    resolved = dict(manifest_overrides or {})
    for key, expected in (("charge", charge_value), ("uhf", uhf_value)):
        if key not in resolved:
            continue
        raw = resolved.pop(key)
        try:
            observed = (
                0
                if raw is None or (isinstance(raw, str) and not raw.strip())
                else strict_int(raw, field=f"engine manifest {key}")
            )
        except ValueError as exc:
            raise ValueError(
                f"engine manifest {key} must match workflow {key}={expected}. got={raw!r}"
            ) from exc
        if observed != expected:
            raise ValueError(
                f"engine manifest {key}={observed} conflicts with workflow {key}={expected}"
            )
    if charge_value != 0:
        resolved["charge"] = charge_value
    if uhf_value != 0:
        resolved["uhf"] = uhf_value
    return resolved or None


__all__ = ["manifest_with_charge_spin", "strict_int", "uhf_from_multiplicity"]
