from __future__ import annotations

import re
from pathlib import Path

from .completion_rules import OPT_ROUTE_RE, TS_ROUTE_RE
from .input_blocks import file_route_lines

SP_RE = re.compile(r"\b(SP|Energy)\b", re.IGNORECASE)
FREQ_RE = re.compile(r"\b(Freq|NumFreq|AnFreq)\b", re.IGNORECASE)


def detect_job_type(inp_path: Path) -> str:
    # Scan every route line through the shared keyword regexes so this label
    # can never disagree with completion/report classification (which also
    # means TightOpt/COpt spellings count as "opt" here too).
    route_line = " ".join(file_route_lines(inp_path))
    if TS_ROUTE_RE.search(route_line):
        return "ts"
    if OPT_ROUTE_RE.search(route_line):
        return "opt"
    if SP_RE.search(route_line):
        return "sp"
    if FREQ_RE.search(route_line):
        return "freq"
    return "other"


__all__ = ["detect_job_type"]
