"""Presentation-only queue ``detail_kind`` from the admitted input lines.

:func:`.job_type.job_type_from_routes` keeps its coarse scientific meaning.
:func:`queue_detail_kind` records what the input requests for the queue
``Detail`` column from the same single read at admission.
"""

from __future__ import annotations

import re
from pathlib import Path

from .completion_rules import RouteFacts, route_facts
from .input_blocks import iter_blocks, percent_directive_header, scan_coordinate_rows
from .input_syntax import orca_line_tokens, orca_route_lines, orca_route_tokens, value_token_index
from .job_type import FREQ_RE, SP_RE, job_type_from_routes

QUEUE_DETAIL_KIND_KEY = "detail_kind"

# Single source of truth: normalized kind token -> fixed Detail label.
QUEUE_DETAIL_KIND_LABELS: dict[str, str] = {
    "sp": "SP",
    "irc": "IRC",
    "neb": "NEB",
    "opt": "Opt",
    "ts": "TS",
    "freq": "Freq",
    "neb-ts": "NEB-TS",
    "opt+freq": "Opt+Freq",
    "ts+freq": "TS+Freq",
    "ts+irc": "TS+IRC",
    "ts+freq+irc": "TS+Freq+IRC",
    "neb-ts+freq": "NEB-TS+Freq",
    "neb-ts+irc": "NEB-TS+IRC",
    "neb-ts+freq+irc": "NEB-TS+Freq+IRC",
    "unknown": "Unknown",
    "other": "Unknown",
}

QUEUE_DETAIL_KINDS = frozenset(QUEUE_DETAIL_KIND_LABELS)

_COMPOUND_EXTRA_ORDER = ("freq", "irc", "neb")

_NEB_TOKEN_RE = re.compile(r"(?:ZOOM-)?NEB(?:-CI)?", re.IGNORECASE)
_NON_SINGLE_POINT_TOKEN_RE = re.compile(
    r"EN(?:ERGY)?GRAD|NUMGRAD|SCANTS|EXTOPT|GOAT(?:-\w+)?|DOCKER|SOLVATOR"
    r"|COMPOUND(?:_FILE)?"
    r"|CIM|PRINTTHERMOCHEM|PROPERTIESONLY|EDA|NMSCAN|NORMALMODESCAN|NMGRAD(?:IENT)?"
    r"|MTR|MT|MODETRAJECTORY"
    r"|ESD(?:\(.*)?",
    re.IGNORECASE,
)
_NON_SINGLE_POINT_BLOCKS = frozenset({"md", "goat", "docker", "solvator", "esd"})
_OPERATION_KEYS = frozenset({"runtyp", "ts_search"})
_FREQUENCY_SWITCHES = frozenset({"anfreq", "numfreq"})


def queue_detail_kind_label(detail_kind: str) -> str | None:
    """Fixed Detail label for a normalized ``detail_kind``, or ``None`` when unrecognized."""
    normalized = detail_kind.strip()
    if not normalized or normalized != normalized.lower():
        return None
    return QUEUE_DETAIL_KIND_LABELS.get(normalized)


def _disabled_frequency_switches(lines: list[str]) -> set[tuple[int, int]]:
    """``(line, column)`` of each switch a closed ``%freq`` body sets to a bare ``false``."""
    disabled: set[tuple[int, int]] = set()
    for block in iter_blocks(lines, "freq"):
        if not block.closed:
            continue
        located = [(row.line_index, token) for row in block.rows for token in row.tokens]
        tokens = [token for _line_index, token in located]
        for index, (line_index, token) in enumerate(located):
            if token.quoted or token.value.lower() not in _FREQUENCY_SWITCHES:
                continue
            value_index = value_token_index(tokens, index)
            if (
                value_index < len(tokens)
                and not tokens[value_index].quoted
                and tokens[value_index].value.lower() == "false"
            ):
                disabled.add((line_index, token.start))
    return disabled


def _route_frequency_keyword_positions(lines: list[str]) -> set[tuple[int, int]]:
    """``AnFreq``/``NumFreq`` on a route line are FREQ keywords, not ``%freq`` block switches."""
    positions: set[tuple[int, int]] = set()
    for line_index, line in enumerate(lines):
        for token in orca_route_tokens(line):
            if not token.quoted and token.value.lower() in _FREQUENCY_SWITCHES:
                positions.add((line_index, token.start))
    return positions


def _is_not_one_plain_job(lines: list[str]) -> bool:
    """Whether the input runs more than one plain single point outside its routes."""
    route_freq = _route_frequency_keyword_positions(lines)
    switches: set[tuple[int, int]] = set()
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        if not tokens:
            continue
        if tokens[0].value.lower() in {"$new_job", "$newjob"}:
            return True
        header = percent_directive_header(tokens)
        if header is not None and (
            header[0] in _NON_SINGLE_POINT_BLOCKS or header[0].startswith("compound")
        ):
            return True
        for token in tokens:
            word = token.value.lower()
            if word in _OPERATION_KEYS:
                return True
            if word in _FREQUENCY_SWITCHES:
                position = (line_index, token.start)
                if position not in route_freq:
                    switches.add(position)
    return bool(switches - _disabled_frequency_switches(lines))


def _route_tokens(lines: list[str]) -> list[str]:
    return [token.value for line in lines for token in orca_route_tokens(line) if not token.quoted]


def _detail_is_unsupported(lines: list[str], facts: RouteFacts, tokens: list[str]) -> bool:
    is_neb = any(_NEB_TOKEN_RE.fullmatch(token) for token in tokens)
    return (
        (facts.is_irc and is_neb)
        or (facts.is_non_stationary and not is_neb)
        or any(token.upper() == "MD" for token in tokens)
        or any(_NON_SINGLE_POINT_TOKEN_RE.fullmatch(token) for token in tokens)
        or scan_coordinate_rows(lines) is not None
        or _is_not_one_plain_job(lines)
    )


def _primary_route_kind(facts: RouteFacts, routes: str) -> str | None:
    if facts.is_neb_ts:
        return "neb-ts"
    if facts.is_ts:
        return "ts"
    if facts.is_opt:
        return "opt"
    if SP_RE.search(routes):
        return "sp"
    return None


def _route_extras(facts: RouteFacts, routes: str, tokens: list[str]) -> set[str]:
    extras: set[str] = set()
    if facts.is_irc:
        extras.add("irc")
    is_plain_neb = (
        not facts.is_ts
        and not facts.is_irc
        and any(_NEB_TOKEN_RE.fullmatch(token) for token in tokens)
    )
    if is_plain_neb:
        extras.add("neb")
    if FREQ_RE.search(routes):
        extras.add("freq")
    return extras


def _join_detail_kind(primary: str | None, extras: set[str]) -> str:
    if primary:
        parts = [primary] + [part for part in _COMPOUND_EXTRA_ORDER if part in extras]
        return parts[0] if len(parts) == 1 else "+".join(parts)
    if len(extras) == 1:
        return next(iter(extras))
    if not extras:
        return "sp"
    return "unknown"


def _assemble_detail_kind(facts: RouteFacts, routes: str, tokens: list[str], coarse: str) -> str:
    primary = _primary_route_kind(facts, routes)
    extras = _route_extras(facts, routes, tokens)
    if primary:
        kind = _join_detail_kind(primary, extras)
    elif coarse not in {"other", "freq"}:
        return "unknown"
    else:
        kind = _join_detail_kind(None, extras)

    return kind if kind in QUEUE_DETAIL_KIND_LABELS else "unknown"


def queue_detail_kind(inp_path: Path, lines: list[str]) -> str:
    """Normalized detail kind for queue metadata, or ``""`` when there is no route line."""
    route_lines = orca_route_lines(lines)
    if not route_lines:
        return ""
    coarse = job_type_from_routes(route_lines)
    facts = route_facts(inp_path, lines=lines)
    routes = " ".join(route_lines)
    tokens = _route_tokens(lines)
    if _detail_is_unsupported(lines, facts, tokens):
        return "unknown"
    return _assemble_detail_kind(facts, routes, tokens, coarse)


__all__ = [
    "QUEUE_DETAIL_KINDS",
    "QUEUE_DETAIL_KIND_KEY",
    "QUEUE_DETAIL_KIND_LABELS",
    "queue_detail_kind",
    "queue_detail_kind_label",
]
