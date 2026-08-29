"""Shared low-level extractors for ORCA output parser modules."""

from __future__ import annotations

from collections.abc import Sequence

from .patterns import (
    _BASIS_KEYWORDS,
    _CALC_TYPE_KEYWORDS,
    _COORD_SECTION_RE,
    _COORD_XYZ_LINE_RE,
    _CPCM_TOKEN_RE,
    _FREQ_SECTION_RE,
    _FREQ_VALUE_RE,
    _INPUT_LINE_RE,
    _METHOD_KEYWORDS,
    _PROGRAM_VERSION_RE,
    _RUNTIME_RE,
    _SMD_SOLVENT_RE,
    _SMD_TRUE_RE,
)

AtomRow = tuple[str, float, float, float]


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


def first_known_token(tokens: list[str], known_tokens: Sequence[str]) -> str:
    token_set = {token.upper() for token in tokens}
    for known in known_tokens:
        if known.upper() in token_set:
            return known
    return ""


def parse_coordinates(text: str) -> list[AtomRow]:
    """Atom rows (element, x, y, z in Å) from the LAST coordinate section.

    The last section holds the final geometry after an optimization; for a
    single point it is the input geometry echoed back.
    """
    sections = list(_COORD_SECTION_RE.finditer(text))
    if not sections:
        return []

    last_section = sections[-1].group(1)
    return [
        (match.group(1), float(match.group(2)), float(match.group(3)), float(match.group(4)))
        for match in _COORD_XYZ_LINE_RE.finditer(last_section)
    ]


def parse_program_version(text: str) -> str:
    """ORCA release from the program header, e.g. ``5.0.4``; empty when absent."""
    match = _PROGRAM_VERSION_RE.search(text)
    return match.group(1) if match else ""


def parse_solvation(text: str, tokens: list[str]) -> str:
    """Implicit solvation model, e.g. ``CPCM(toluene)`` or ``SMD(water)``.

    CPCM comes from the route line token; SMD from the echoed ``%cpcm`` block
    (``smd true`` + ``smdsolvent "..."``). Empty string means gas phase (or an
    unrecognized model).
    """
    cpcm_seen = False
    cpcm_solvent = ""
    for token in tokens:
        match = _CPCM_TOKEN_RE.match(token)
        if match:
            cpcm_seen = True
            cpcm_solvent = (match.group(1) or "").strip()
            break
    if _SMD_TRUE_RE.search(text):
        smd_match = _SMD_SOLVENT_RE.search(text)
        solvent = smd_match.group(1).strip() if smd_match else cpcm_solvent
        return f"SMD({solvent})" if solvent else "SMD"
    if cpcm_seen:
        return f"CPCM({cpcm_solvent})" if cpcm_solvent else "CPCM"
    return ""


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
