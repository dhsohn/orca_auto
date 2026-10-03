"""Shared low-level extractors for ORCA output parser modules."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from .patterns import (
    _BASIS_KEYWORDS,
    _COORD_XYZ_LINE_RE,
    _CPCM_TOKEN_RE,
    _INPUT_LINE_RE,
    _METHOD_KEYWORDS,
    _OPT_CYCLE_RE,
    _PROGRAM_VERSION_RE,
    _RUNTIME_RE,
    _SMD_SOLVENT_RE,
    _SMD_TRUE_RE,
    FINAL_SINGLE_POINT_ENERGY_RE,
    final_single_point_energy_value,
)

AtomRow = tuple[str, float, float, float]


def parse_optimization_cycles(text: str) -> Iterator[tuple[int, float | None, str]]:
    """Yield each cycle's number, last finite energy, and convergence text.

    Keep headers without valid energies so callers can distinguish an unfinished
    cycle from output with no optimization cycles.
    """
    cycle_positions = [
        (match.start(), int(match.group(1))) for match in _OPT_CYCLE_RE.finditer(text)
    ]
    if not cycle_positions:
        return

    energy_positions = []
    for energy_match in FINAL_SINGLE_POINT_ENERGY_RE.finditer(text):
        try:
            energy_positions.append(
                (energy_match.start(), final_single_point_energy_value(energy_match.group(1)))
            )
        except ValueError:
            continue

    for position, (cycle_start, cycle_num) in enumerate(cycle_positions):
        cycle_end = (
            cycle_positions[position + 1][0] if position + 1 < len(cycle_positions) else len(text)
        )
        energy = None
        for energy_position, energy_value in energy_positions:
            if cycle_start <= energy_position < cycle_end:
                energy = energy_value
        yield cycle_num, energy, text[cycle_start:cycle_end]


def parse_input_line(text: str) -> tuple[str, str, list[str]]:
    """Extract method, basis_set, and tokens from the input line.

    Returns:
        (method, basis_set, all_input_tokens)
    """
    matches = _INPUT_LINE_RE.findall(text)
    if not matches:
        return ("", "", [])

    # There may be multiple input lines; merge them.
    all_tokens: list[str] = []
    for line in matches:
        all_tokens.extend(line.strip().split())

    method = first_known_token(all_tokens, _METHOD_KEYWORDS)
    basis_set = first_known_token(all_tokens, _BASIS_KEYWORDS)

    return (method, basis_set, all_tokens)


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
    header = "CARTESIAN COORDINATES (ANGSTROEM)"
    section_starts: list[int] = []
    section_start = text.find(header)
    while section_start >= 0:
        section_starts.append(section_start)
        section_start = text.find(header, section_start + len(header))
    if not section_starts:
        return []

    def _parse_section(start: int, end: int) -> list[AtomRow] | None:
        section_lines = text[start + len(header) : end].splitlines()
        line_index = 0
        while line_index < len(section_lines) and not section_lines[line_index].strip():
            line_index += 1
        if line_index >= len(section_lines):
            return []

        separator = section_lines[line_index].strip()
        if not separator or set(separator) != {"-"}:
            return None
        line_index += 1

        atoms: list[AtomRow] = []
        while line_index < len(section_lines):
            raw_line = section_lines[line_index]
            if not raw_line.strip():
                break
            match = _COORD_XYZ_LINE_RE.match(raw_line)
            if match is None or raw_line[match.end() :].strip():
                first_token = raw_line.strip().split()[0] if raw_line.strip() else ""
                if first_token.isalpha() and len(first_token) <= 2:
                    return None
                break
            atoms.append(
                (match.group(1), float(match.group(2)), float(match.group(3)), float(match.group(4)))
            )
            line_index += 1
        return atoms

    last_index = len(section_starts) - 1
    last_start = section_starts[last_index]
    last_end = section_starts[last_index + 1] if last_index + 1 < len(section_starts) else len(text)
    atoms = _parse_section(last_start, last_end)
    if atoms is None or not atoms:
        return []

    expected_n_atoms: int | None = None
    for index in range(last_index - 1, -1, -1):
        previous_start = section_starts[index]
        previous_end = section_starts[index + 1]
        previous_atoms = _parse_section(previous_start, previous_end)
        if previous_atoms:
            expected_n_atoms = len(previous_atoms)
            break

    if expected_n_atoms is not None and len(atoms) != expected_n_atoms:
        return []
    return atoms


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
