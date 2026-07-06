from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import file_route_lines

# Only real ORCA TS keywords. No bare `TS` token: ORCA has no `! TS`, so it
# can only ever match stray text (the SCAN-functional collision class), never
# a job ORCA would actually run as a TS search.
TS_ROUTE_RE = re.compile(r"\b(OPTTS|SCANTS|NEB-TS)\b", re.IGNORECASE)
IRC_ROUTE_RE = re.compile(r"\bIRC\b", re.IGNORECASE)
# Every simple-input spelling of a geometry optimization: convergence-prefixed
# (TightOpt, ...) and coordinate-system (COpt/ZOpt) variants included, so a
# TightOpt job cannot slip through as a single point.
OPT_ROUTE_RE = re.compile(r"\b(?:VERYTIGHT|TIGHT|NORMAL|LOOSE)?[CZ]?OPT\b", re.IGNORECASE)

# Negative modes at or below this magnitude are numerical noise, not a reaction
# coordinate. Shared by the completion analyzer and the SI/report renderers so
# a verified TS can never be re-counted differently in the published SI.
IMAGINARY_FREQ_THRESHOLD_CM1 = 10.0


@dataclass
class CompletionMode:
    kind: str  # "ts" or "opt"
    require_irc: bool
    route_line: str


def detect_completion_mode(inp_path: Path) -> CompletionMode:
    # ORCA accepts multiple route (`!`) lines and allows `%` blocks between them,
    # so a TS/IRC keyword may sit on any route line, not just the first. Scan all
    # of them, mirroring retry_policy_for_input / input_uses_scants; reading only
    # the first line would misclassify such a job as Opt mode and skip the
    # imaginary-frequency / IRC completion checks entirely.
    route_line = "\n".join(file_route_lines(inp_path))
    kind = "ts" if TS_ROUTE_RE.search(route_line) else "opt"
    require_irc = bool(IRC_ROUTE_RE.search(route_line))
    return CompletionMode(kind=kind, require_irc=require_irc, route_line=route_line)
