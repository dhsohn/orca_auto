"""Conservative permission and output evidence for optional recovery orbitals.

This is an eligibility gate, not a GBW decoder or a scientific completion test.
Unknown SCF directives and output dialects deliberately reduce automatic reuse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .input_blocks import InputSource, parse_input
from .input_references import orca_input_requests_moread

_FRESH_GUESSES = frozenset({"pmodel", "patom", "hueckel", "huckel", "hcore"})
_STATE_PREFIXES = (
    "guess",
    "rotate",
    "mix",
    "occ",
    "alpha",
    "beta",
    "broken",
    "flipspin",
    "finalms",
    "deltascf",
    "mom",
    "pmom",
    "imom",
    "smear",
    "fermi",
    "rohf_case",
)
# Only understood numerical SCF scalars are transparent to the policy. Other
# keys may select an electronic state or an unfamiliar guess: preserve them.
_NUMERICAL_SCF_KEYS = frozenset(
    {
        "maxiter",
        "tolenergy",
        "toldens",
        "tolrms",
        "tolmaxp",
        "tolerrore",
        "tolerrord",
        "diismaxeq",
        "diisstart",
        "soscfstart",
        "dampfac",
        "dampmax",
        "dampmin",
        "levelshift",
        "shift",
        "sthresh",
    }
)
_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][-+]?\d+)?\Z")


def recovery_guess_policy(lines: InputSource) -> str:
    """Return a skip reason, or ``automatic`` when replacement is permitted.

    Explicit reuse wins, including MORead with AutoStart false. The opt-out
    for AutoStart false is our recovery policy, not ORCA's AutoStart semantics.
    """
    parsed = parse_input(lines)
    if orca_input_requests_moread(parsed):
        return "explicit_reuse"
    for route in parsed.route_tokens:
        for token in route:
            word = token.value.lower()
            if word in _FRESH_GUESSES:
                return "explicit_fresh_guess"
            if word == "noautostart":
                return "autostart_disabled"
            if word == "autostart":
                return "ambiguous_scf_policy"
            if word.startswith(_STATE_PREFIXES) or word == "noiter":
                return "explicit_state_control"
    scf_blocks = list(parsed.blocks_named("scf"))
    if len(scf_blocks) > 1:
        return "ambiguous_scf_policy"
    for block in scf_blocks:
        if not block.closed:
            return "ambiguous_scf_policy"
        tokens = [token for row in block.rows for token in row.tokens]
        index = 0
        while index < len(tokens):
            key = tokens[index]
            word = key.value.lower()
            if word == "guess":
                return "explicit_fresh_guess"
            if word.startswith(_STATE_PREFIXES):
                return "explicit_state_control"
            index += 1
            if index < len(tokens) and tokens[index].value == "=":
                index += 1
            if key.quoted or index >= len(tokens) or tokens[index].quoted:
                return "ambiguous_scf_policy"
            value = tokens[index].value.lower()
            index += 1
            if word == "autostart":
                if value in {"false", "0"}:
                    return "autostart_disabled"
                if value not in {"true", "1"}:
                    return "ambiguous_scf_policy"
            elif word not in _NUMERICAL_SCF_KEYS or not _NUMBER.fullmatch(value):
                return "unclassified_scf_policy"
    # These blocks can specify initial occupations, roots or state averaging.
    for name in ("casscf", "mrci", "rocis", "tddft", "cis", "mdci", "deltascf"):
        if list(parsed.blocks_named(name)):
            return "explicit_state_control"
    for block in parsed.blocks:
        if block.name != "scf" and any(
            not token.quoted
            and (
                token.value.lower().startswith(_STATE_PREFIXES)
                or token.value.lower() in {"iroot", "nroots", "root", "mult", "nel", "norb"}
            )
            for row in block.rows
            for token in row.tokens
        ):
            return "explicit_state_control"
    return "automatic"


_BANNER = re.compile(r"\*\s+O\s+R\s+C\s+A\s+\*\Z")
# Authentic 6.1.1 later phases can omit startup/GUESS headings and redirect SCF
# output to .lastscf. Each observed phase start needs its own completed GUESS.
_BOUNDARY = re.compile(
    r"(?:ORCA (?:STARTUP(?: CALCULATIONS)?|GUESS)|INITIAL GUESS:.*|"
    r"\*\s*GEOMETRY OPTIMIZATION CYCLE\s+\d+\s*\*|"
    r"\*{3}\s*FINAL ENERGY EVALUATION AT THE STATIONARY POINT\s*\*{3}|"
    r"Intrinsic Reaction Coordinate Calculation|ORCA NUMERICAL FREQUENCIES|"
    r"The output will be reduced\. Please look at the following files:|"
    r"<< Calculating gradient on displaced geometry\s+\d+\s+\(of\s+\d+\)\s*>>|"
    r"\*\s*(?:FORWARD|BACKWARD) IRC\s*\*|"
    r"\*\s*Starting time:.*)\Z"
)
_FINISHED = re.compile(r"Finished Guess after\s+\d+(?:\.\d+)?\s+sec\Z")
_ECHO_END = re.compile(r"\|\s*\d+>\s*\*{4}END OF INPUT\*{4}\Z")


@dataclass(frozen=True)
class OrbitalReadiness:
    eligible: bool
    reason: str
    boundary_line: int = 0
    evidence_line: int = 0


def checkpoint_output_readiness(payload: bytes) -> OrbitalReadiness:
    """Recognize ORCA 6.1 GUESS completion after the latest phase boundary.

    Full bounded output is required: a tail alone cannot exclude later phases
    or an input echo. Reduced-output phases need fresh GUESS evidence after the
    latest boundary; redirected SCF details are not read or inferred. Line
    numbers are one-based and refer to the exact captured output recorded by
    the caller. No SCF-header-only fallback is inferred.
    """
    if not payload or not payload.endswith(b"\n") or b"\0" in payload:
        return OrbitalReadiness(False, "incomplete_output")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError:
        return OrbitalReadiness(False, "unrecognized_output")
    banner = False
    echo = False
    boundary = 0
    evidence = 0
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if line == "INPUT FILE":
            echo = True
        if _ECHO_END.fullmatch(line):
            echo = False
            continue
        if echo or line.startswith(("#", "|")):
            continue
        if _BANNER.fullmatch(line):
            banner = True
            boundary, evidence = number, 0
        elif _BOUNDARY.fullmatch(line):
            boundary, evidence = number, 0
        elif banner and boundary and _FINISHED.fullmatch(line):
            evidence = number
    if echo:
        return OrbitalReadiness(False, "incomplete_input_echo", boundary)
    if not banner:
        return OrbitalReadiness(False, "unrecognized_output", boundary)
    if not evidence:
        return OrbitalReadiness(False, "no_completed_guess_after_boundary", boundary)
    return OrbitalReadiness(True, "completed_guess", boundary, evidence)
