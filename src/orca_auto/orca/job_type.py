from __future__ import annotations

import re
from pathlib import Path

from .completion_rules import TS_ROUTE_RE

OPT_RE = re.compile(r"\bOpt\b", re.IGNORECASE)
SP_RE = re.compile(r"\b(SP|Energy)\b", re.IGNORECASE)
FREQ_RE = re.compile(r"\b(Freq|NumFreq|AnFreq)\b", re.IGNORECASE)


def detect_job_type(inp_path: Path) -> str:
    route_line = _read_route_line(inp_path)
    if TS_ROUTE_RE.search(route_line):
        return "ts"
    if OPT_RE.search(route_line):
        return "opt"
    if SP_RE.search(route_line):
        return "sp"
    if FREQ_RE.search(route_line):
        return "freq"
    return "other"


def _read_route_line(inp_path: Path) -> str:
    try:
        with inp_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("!"):
                    return stripped
    except OSError:
        pass
    return ""


__all__ = ["detect_job_type"]
