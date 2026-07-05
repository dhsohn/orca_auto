"""Shared low-level extractors for ORCA output parser modules."""

from __future__ import annotations

from .patterns import (
    _BASIS_KEYWORDS,
    _CALC_TYPE_KEYWORDS,
    _COORD_LINE_RE,
    _COORD_SECTION_RE,
    _FREQ_SECTION_RE,
    _FREQ_VALUE_RE,
    _INPUT_LINE_RE,
    _METHOD_KEYWORDS,
    _RUNTIME_RE,
)


def parse_input_line(text: str) -> tuple[str, str, str, list[str]]:
    """Extract calc_type, method, and basis_set from the input line.

    Returns:
        (calc_type, method, basis_set, all_input_tokens)
    """
    matches = _INPUT_LINE_RE.findall(text)
    if not matches:
        return ("sp", "", "", [])

    # There may be multiple input lines; merge them.
    all_tokens: list[str] = []
    for line in matches:
        all_tokens.extend(line.strip().split())

    calc_type = calc_type_from_tokens(all_tokens)
    method = first_known_token(all_tokens, _METHOD_KEYWORDS)
    basis_set = first_known_token(all_tokens, _BASIS_KEYWORDS)

    return (calc_type, method, basis_set, all_tokens)


def calc_type_from_tokens(tokens: list[str]) -> str:
    calc_types = [
        calc_type
        for token in tokens
        for keyword, calc_type in _CALC_TYPE_KEYWORDS.items()
        if token.upper() == keyword
    ]
    if not calc_types:
        return "sp"
    if "opt" in calc_types and "freq" in calc_types:
        return "opt+freq"
    if "ts" in calc_types and "freq" in calc_types:
        return "ts+freq"
    return calc_types[0]


def first_known_token(tokens: list[str], known_tokens: list[str]) -> str:
    token_set = {token.upper() for token in tokens}
    for known in known_tokens:
        if known.upper() in token_set:
            return known
    return ""


def parse_coordinates(text: str) -> tuple[list[str], int]:
    """Extract element symbols from the coordinate section.

    Returns:
        (elements, n_atoms)
    """
    # Use the last coordinate section (final coordinates after optimization).
    sections = list(_COORD_SECTION_RE.finditer(text))
    if not sections:
        return ([], 0)

    last_section = sections[-1].group(1)
    elements = _COORD_LINE_RE.findall(last_section)
    return (elements, len(elements))


def parse_frequencies(text: str) -> tuple[bool | None, float | None]:
    """Extract imaginary frequency status and lowest frequency.

    Returns:
        (has_imaginary_freq, lowest_freq_cm1)
    """
    section_matches = list(_FREQ_SECTION_RE.finditer(text))
    if not section_matches:
        return (None, None)

    section = section_matches[-1].group(1)
    freq_values = [float(v) for v in _FREQ_VALUE_RE.findall(section)]

    if not freq_values:
        return (None, None)

    # Exclude translational/rotational modes near 0.0 cm^-1 (absolute value < 10 cm^-1)
    real_freqs = [f for f in freq_values if abs(f) > 10.0]
    if not real_freqs:
        return (False, None)

    lowest = min(real_freqs)
    has_imaginary = lowest < 0.0
    return (has_imaginary, lowest)


def parse_wall_time(text: str) -> int | None:
    """Convert runtime to seconds."""
    m = _RUNTIME_RE.search(text)
    if m is None:
        return None
    days, hours, minutes, seconds = (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)),
        int(m.group(4)),
    )
    return days * 86400 + hours * 3600 + minutes * 60 + seconds
