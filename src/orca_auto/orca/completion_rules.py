"""What an ORCA input asks for, read once: :class:`RouteFacts` and the completion mode.

Every route question (the completion analyzer's mode, which report sections
and which SI block a job gets, whether its geometry is a stationary point) is
answered from one :func:`route_facts` reading, so no two consumers can
classify the same input differently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_syntax import input_file_lines, orca_route_lines
from .relaxed_scan import scan_coordinate_rows

# Only real ORCA TS keywords. No bare `TS` token: ORCA has no `! TS`, so it
# can only ever match stray text (the SCAN-functional collision class), never
# a job ORCA would actually run as a TS search.
TS_ROUTE_RE = re.compile(r"\b(OPTTS|NEB-TS)\b", re.IGNORECASE)
IRC_ROUTE_RE = re.compile(r"\bIRC\b", re.IGNORECASE)
_NEB_TS_ROUTE_RE = re.compile(r"\b(?:ZOOM-)?NEB-TS\b", re.IGNORECASE)
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
    return bool(OPT_ROUTE_RE.search(routes)) and not PARTIAL_OPT_ROUTE_RE.search(routes)


@dataclass(frozen=True)
class RouteFacts:
    """The route keywords and ``%geom Scan`` block of the input at ``inp_path``.

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
    is_non_stationary: bool  # a plain NEB / NEB-CI path or MD, no TS search


def route_facts(inp_path: Path) -> RouteFacts:
    """Read ``inp_path`` once and classify its route lines and scan block."""
    lines = input_file_lines(inp_path)
    route_lines = tuple(orca_route_lines(lines))
    routes = " ".join(route_lines)
    is_ts = bool(TS_ROUTE_RE.search(routes))
    is_opt = is_optimization_route(routes)
    return RouteFacts(
        inp_path=inp_path,
        route_lines=route_lines,
        is_ts=is_ts,
        is_irc=bool(IRC_ROUTE_RE.search(routes)),
        is_neb_ts=bool(_NEB_TS_ROUTE_RE.search(routes)),
        is_opt=is_opt,
        is_full_opt=is_full_optimization_route(routes),
        is_relaxed_scan=is_opt and scan_coordinate_rows(lines) is not None,
        # The NEB alternative also matches the NEB of NEB-TS, a TS route.
        is_non_stationary=not is_ts and bool(_NON_STATIONARY_ROUTE_RE.search(routes)),
    )


@dataclass
class CompletionMode:
    """What the completion analyzer verifies beyond a normal termination.

    ``kind`` is ``"ts"`` for a TS route (OptTS, NEB-TS), whose completion also
    needs exactly one imaginary mode, and ``"opt"`` for every other input,
    single points and IRC included. ``require_irc`` records an IRC keyword;
    only a ``"ts"`` verdict consults it.
    """

    kind: str
    require_irc: bool


def detect_completion_mode(inp_path: Path) -> CompletionMode:
    route = route_facts(inp_path)
    return CompletionMode(kind="ts" if route.is_ts else "opt", require_irc=route.is_irc)
