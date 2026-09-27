"""Route-line regexes and the completion mode (TS, IRC, Opt, SP) an input asks for."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_syntax import file_route_lines

# Only real ORCA TS keywords. No bare `TS` token: ORCA has no `! TS`, so it
# can only ever match stray text (the SCAN-functional collision class), never
# a job ORCA would actually run as a TS search.
TS_ROUTE_RE = re.compile(r"\b(OPTTS|NEB-TS)\b", re.IGNORECASE)
IRC_ROUTE_RE = re.compile(r"\bIRC\b", re.IGNORECASE)
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

# Negative modes at or below this magnitude are numerical noise, not a reaction
# coordinate. Shared by the completion analyzer and the SI/report renderers so
# a verified TS can never be re-counted differently in the published SI.
IMAGINARY_FREQ_THRESHOLD_CM1 = 10.0


def is_optimization_route(routes: str) -> bool:
    """Whether ``routes`` run a non-TS geometry optimization of any kind."""
    return bool(OPT_ROUTE_RE.search(routes) or PARTIAL_OPT_ROUTE_RE.search(routes))


def is_full_optimization_route(routes: str) -> bool:
    """Whether ``routes`` minimize on the full surface, so the result may be claimed a minimum."""
    return bool(OPT_ROUTE_RE.search(routes)) and not PARTIAL_OPT_ROUTE_RE.search(routes)


@dataclass
class CompletionMode:
    kind: str  # "ts" or "opt"
    require_irc: bool


def detect_completion_mode(inp_path: Path) -> CompletionMode:
    # ORCA accepts multiple route (`!`) lines and allows `%` blocks between them,
    # so a TS/IRC keyword may sit on any route line, not just the first. Scan all
    # of them; reading only
    # the first line would misclassify such a job as Opt mode and skip the
    # imaginary-frequency / IRC completion checks entirely.
    routes = "\n".join(file_route_lines(inp_path))
    kind = "ts" if TS_ROUTE_RE.search(routes) else "opt"
    require_irc = bool(IRC_ROUTE_RE.search(routes))
    return CompletionMode(kind=kind, require_irc=require_irc)
