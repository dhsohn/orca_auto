"""What an ORCA input requests, as route facts: :class:`RouteFacts`.

Facts, not completion policy. The route keyword rules (the TS, IRC,
optimization and frequency patterns), whether an input requests a frequency
calculation (:func:`requests_frequency`) and :func:`route_facts` read one
input version's :class:`~.input_blocks.ParsedInput`. Completion policy over
these facts (the completion mode, the geometry scope) is
:mod:`.completion_rules`; the operation facts of submission, the binding and
the queue Detail (:mod:`.operation_facts`) build on them too. Whether an input
requests a frequency calculation is the one shared fact, so completion and the
Hessian admission limit cannot read the same input differently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import InputSource, ParsedInput, parse_input
from .input_syntax import input_file_lines, value_token_index

# Only real ORCA TS keywords. No bare `TS` token: ORCA has no `! TS`, so it
# can only ever match stray text (the SCAN-functional collision class), never
# a job ORCA would actually run as a TS search. NEB-TS also matches the tail
# of ZOOM-NEB-TS.
TS_ROUTE_RE = re.compile(r"\b(OPTTS|NEB-TS)\b", re.IGNORECASE)
IRC_ROUTE_RE = re.compile(r"\bIRC\b", re.IGNORECASE)
FREQ_ROUTE_RE = re.compile(r"\b(Freq|NumFreq|AnFreq)\b", re.IGNORECASE)
# ORCA 6.1 simple-input keywords that run a non-TS geometry optimization:
# convergence-prefixed (TightOpt ... SloppyOpt), coordinate-system (COpt/ZOpt)
# and L-BFGS (L-Opt) spellings, so a TightOpt job cannot slip through as a
# single point. search() also hits the -Opt tail of MECP-Opt/CI-Opt; only
# is_full_optimization_route decides a minimum claim.
OPT_ROUTE_RE = re.compile(
    r"\b(?:L-|(?:VERYTIGHT|TIGHT|NORMAL|LOOSE|SLOPPY|CRUDE)?[CZ]?)OPT\b", re.IGNORECASE
)
# Optimizations whose result is no minimum of the full surface: hydrogen-only
# (OptH/L-OptH), QM/MM active region (QMMMOpt) and crossing-seam searches
# (SurfCrossOpt = MECP-Opt, CI-Opt = ConicalIntersect-Opt).
PARTIAL_OPT_ROUTE_RE = re.compile(
    r"\b(?:(?:L-)?OPTH|QMMMOPT(?:/PDYNAMO)?|SURFCROSSOPT|MECP-OPT|CI-OPT|CONICALINTERSECT-OPT)\b",
    re.IGNORECASE,
)
# Route families whose final geometry is not a stationary point: path methods
# (plain NEB / NEB-CI — NEB-TS is a TS route) and dynamics. No SCAN token
# here: `SCAN` in a route line is the density functional
# (`! SCAN def2-SVP Opt Freq`), not a scan job — relaxed scans are identified
# from the `%geom Scan` block.
_NON_STATIONARY_ROUTE_RE = re.compile(
    r"\b(?:ZOOM-)?NEB(?:-CI)?\b|\bMD\b",
    re.IGNORECASE,
)


def is_optimization_route(routes: str) -> bool:
    """Whether ``routes`` run a non-TS geometry optimization of any kind."""
    return bool(OPT_ROUTE_RE.search(routes) or PARTIAL_OPT_ROUTE_RE.search(routes))


def is_full_optimization_route(routes: str) -> bool:
    """Whether ``routes`` minimize on the full surface, so the result may be claimed a minimum."""
    return (
        bool(OPT_ROUTE_RE.search(routes))
        and not PARTIAL_OPT_ROUTE_RE.search(routes)
        and not re.search(r"\bRIGIDBODYOPT\b", routes, re.IGNORECASE)
    )


def _has_geometry_constraints(parsed: ParsedInput) -> bool:
    """Whether %geom restricts the optimized coordinates.

    Boolean settings use their last explicit value; comments, quoted values
    and empty Constraints blocks do not impose a restriction.
    """
    hydrogen_settings: dict[str, bool] = {}
    constrained = False
    for block in parsed.blocks_named("geom"):
        tokens = [token for row in block.rows for token in row.tokens]
        for index, token in enumerate(tokens):
            if token.quoted:
                continue
            keyword = token.value.lower()
            value_index = value_token_index(tokens, index)
            if value_index >= len(tokens):
                continue
            value = tokens[value_index]
            if (
                keyword
                in {"constraints", "constrainfragments", "fixfrags", "rigidfrags", "relaxhfrags"}
                and value.value.lower() != "end"
            ):
                constrained = True
            elif keyword in {"optimizehydrogens", "freezehydrogens"} and not value.quoted:
                hydrogen_settings[keyword] = value.value.lower() == "true"
    return constrained or any(hydrogen_settings.values())


def _has_frequency_block_request(parsed: ParsedInput) -> bool:
    """Read AnFreq/NumFreq switches from %freq bodies, using their last boolean value."""
    settings: dict[str, bool] = {}
    for declaration in parsed.operation_declarations:
        value = declaration.value
        if declaration.block != "freq" or declaration.key_quoted or value is None:
            continue
        if not value.quoted and value.value.lower() in {"true", "false"}:
            settings[declaration.key] = value.value.lower() == "true"
    return any(settings.values())


def requests_frequency(lines: InputSource) -> bool:
    """Whether the input explicitly asks for a frequency (Hessian) calculation.

    A route keyword (Freq, NumFreq, AnFreq) on any route line, or an active
    ``%freq`` AnFreq/NumFreq switch whose last boolean value is true. This is
    the request only: whether completion also needs frequency evidence for
    other reasons (a TS search) is :func:`completion_mode` policy.
    """
    parsed = parse_input(lines)
    routes = " ".join(parsed.route_lines)
    return bool(FREQ_ROUTE_RE.search(routes)) or _has_frequency_block_request(parsed)


@dataclass(frozen=True)
class RouteFacts:
    """The route keywords and geometry/frequency settings of the input at ``inp_path``.

    ORCA accepts several route (``!``) lines with ``%`` blocks between them,
    so every flag reads all of ``route_lines``, never just the first. An
    unreadable input has no route lines and no flag set.
    """

    inp_path: Path
    route_lines: tuple[str, ...]
    is_ts: bool  # OptTS or NEB-TS
    is_irc: bool
    is_neb_ts: bool  # NEB-TS, ZOOM-NEB-TS included
    is_opt: bool  # a non-TS optimization, partial ones (OptH, MECP-Opt, ...) included
    is_full_opt: bool  # an optimization of the full surface, which may claim a minimum
    is_relaxed_scan: bool  # an optimization with a %geom Scan block
    has_constraints: bool
    is_non_stationary: bool  # a plain NEB / NEB-CI path or MD, no TS search
    requests_frequency: bool
    is_engrad: bool = False  # explicit energy-and-gradient request


def is_non_stationary_route(routes: str) -> bool:
    """Whether ``routes`` run a plain NEB / NEB-CI path or MD, and no TS search."""
    return not TS_ROUTE_RE.search(routes) and bool(_NON_STATIONARY_ROUTE_RE.search(routes))


def route_facts(inp_path: Path, *, lines: InputSource | None = None) -> RouteFacts:
    """Classify input routes and geometry settings, reusing verified lines when supplied.

    ``lines`` may be the input's lines or its :class:`~.input_blocks.ParsedInput`;
    without them the file at ``inp_path`` is read.
    """
    parsed = parse_input(input_file_lines(inp_path) if lines is None else lines)
    route_lines = parsed.route_lines
    routes = " ".join(route_lines)
    ts_keywords = {match.group(1).upper() for match in TS_ROUTE_RE.finditer(routes)}
    is_ts = bool(ts_keywords)
    is_opt = is_optimization_route(routes)
    has_constraints = _has_geometry_constraints(parsed)
    return RouteFacts(
        inp_path=inp_path,
        requests_frequency=requests_frequency(parsed),
        is_engrad=any(
            not token.quoted and token.value.casefold() in {"engrad", "energygrad"}
            for tokens in parsed.route_tokens
            for token in tokens
        ),
        route_lines=route_lines,
        is_ts=is_ts,
        is_irc=bool(IRC_ROUTE_RE.search(routes)),
        is_neb_ts="NEB-TS" in ts_keywords,
        is_opt=is_opt,
        is_full_opt=is_full_optimization_route(routes) and not has_constraints,
        has_constraints=has_constraints
        or bool(re.search(r"\bRIGIDBODYOPT\b", routes, re.IGNORECASE)),
        is_relaxed_scan=is_opt and parsed.scan_coordinate_rows is not None,
        # The NEB alternative also matches the NEB of NEB-TS, a TS route.
        is_non_stationary=is_non_stationary_route(routes),
    )
