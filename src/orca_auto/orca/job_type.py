"""Coarse ORCA job-type detection (``ts`` / ``opt`` / ``sp`` / ``freq``) from route lines."""

from __future__ import annotations

import re
from pathlib import Path

from .completion_rules import TS_ROUTE_RE, is_optimization_route
from .input_syntax import file_route_lines

SP_RE = re.compile(r"\b(SP|Energy)\b", re.IGNORECASE)
FREQ_RE = re.compile(r"\b(Freq|NumFreq|AnFreq)\b", re.IGNORECASE)


def detect_job_type(inp_path: Path) -> str:
    return job_type_from_routes(file_route_lines(inp_path))


def job_type_from_routes(route_lines: list[str]) -> str:
    # Scan every route line through the shared keyword regexes so this label
    # can never disagree with completion/report classification (which also
    # means TightOpt/COpt spellings count as "opt" here too). The coarse label
    # includes partial optimizations (OptH, MECP, ...); minimum claims use
    # structure_kind.
    route_line = " ".join(route_lines)
    if TS_ROUTE_RE.search(route_line):
        return "ts"
    if is_optimization_route(route_line):
        return "opt"
    if SP_RE.search(route_line):
        return "sp"
    if FREQ_RE.search(route_line):
        return "freq"
    return "other"


__all__ = ["detect_job_type", "job_type_from_routes"]
