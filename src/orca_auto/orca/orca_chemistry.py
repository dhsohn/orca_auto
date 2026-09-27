"""Chemical formula of an atom list, shared by the parser, reports and molecule keys."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


def build_formula(elements: Iterable[str]) -> str:
    """Hill-system formula of ``elements``; empty for no atoms.

    With carbon: C, then H, then every other symbol alphabetically. Without
    carbon: every symbol alphabetically, H included (``ClNa``, ``FeO4``, ``H2O``).
    """
    counts = Counter(elements)
    order = sorted(counts)
    if "C" in counts:
        first = ["C", "H"] if "H" in counts else ["C"]
        order = first + [symbol for symbol in order if symbol not in first]
    return "".join(
        symbol if counts[symbol] == 1 else f"{symbol}{counts[symbol]}" for symbol in order
    )
