from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TS_ROUTE_RE = re.compile(r"\b(OPTTS|SCANTS|NEB-TS)\b", re.IGNORECASE)
IRC_ROUTE_RE = re.compile(r"\bIRC\b", re.IGNORECASE)


@dataclass
class CompletionMode:
    kind: str  # "ts" or "opt"
    require_irc: bool
    route_line: str


def detect_completion_mode(inp_path: Path) -> CompletionMode:
    route_line = ""
    with inp_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if stripped.startswith("!"):
                route_line = stripped
                break

    kind = "ts" if TS_ROUTE_RE.search(route_line) else "opt"
    require_irc = bool(IRC_ROUTE_RE.search(route_line))
    return CompletionMode(kind=kind, require_irc=require_irc, route_line=route_line)
