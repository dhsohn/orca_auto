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
    "neb-idpp": "NEB-IDPP",
    "neb-mmfts": "NEB-MMFTS",
    "opt+freq": "Opt+Freq",
    "ts+freq": "TS+Freq",
    "ts+irc": "TS+IRC",
    "ts+freq+irc": "TS+Freq+IRC",
    "neb-ts+freq": "NEB-TS+Freq",
    "neb-ts+irc": "NEB-TS+IRC",
    "neb-ts+freq+irc": "NEB-TS+Freq+IRC",
    "unknown": "Unknown",
    "unsupported": "Other",
    # Legacy "other" never distinguished a definite operation from missing evidence.
    "other": "Unknown",
}

QUEUE_DETAIL_KINDS = frozenset(QUEUE_DETAIL_KIND_LABELS)

_COMPOUND_EXTRA_ORDER = ("freq", "irc", "neb")

# Exact presentation aliases only; scientific route rules stay in completion_rules.
_NEB_TOKEN_RE = re.compile(r"(?:ZOOM-)?NEB(?:-CI)?", re.IGNORECASE)
_NEB_NAMED_KINDS = frozenset({"neb-idpp", "neb-mmfts"})
_NEB_TS_TOKENS = frozenset(
    {"neb-ts", "zoom-neb-ts", "fast-neb-ts", "loose-neb-ts", "tight-neb-ts", "flat-neb-ts"}
)
_NEB_PATH_TOKENS = _NEB_NAMED_KINDS | {"neb", "neb-ci", "zoom-neb", "zoom-neb-ci"}
_NEB_TOKENS = _NEB_PATH_TOKENS | _NEB_TS_TOKENS
_NEB_FAMILY_RE = re.compile(r"(?:[A-Z0-9]+-)*NEB(?:[-(].*)?", re.IGNORECASE)
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
_DEFINITE_UNSUPPORTED_TOKEN_RE = re.compile(
    r"EN(?:ERGY)?GRAD|NUMGRAD|SCANTS|EXTOPT|GOAT(?:-(?:ENTROPY|EXPLORE|REACT|DIVERSITY|TS|COARSE))?"
    r"|DOCKER|SOLVATOR|COMPOUND(?:_FILE)?|MD"
    r"|CIM|PRINTTHERMOCHEM|PROPERTIESONLY|EDA|NMSCAN|NORMALMODESCAN|NMGRAD(?:IENT)?"
    r"|MTR|MT|MODETRAJECTORY",
    re.IGNORECASE,
)
_ESD_OPERATION_RE = re.compile(
    r"(?<!\S)ESD\s*\(\s*(?:ABS|FLUOR|PHOSP|ISC|IC|RR|RRAMAN)\s*\)(?!\S)", re.IGNORECASE
)
_BLOCK_OPERATION_VALUES = {
    "freq": {key: frozenset({"true", "false"}) for key in _FREQUENCY_SWITCHES},
    "geom": {"ts_search": frozenset({"ef"})},
    "method": {"runtyp": frozenset({"gradient", "engrad", "energygrad", "numgrad"})},
}


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
        if not tokens[0].quoted and tokens[0].value.lower() in {"$new_job", "$newjob"}:
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


def _presentation_input_lines(lines: list[str]) -> list[str]:
    """Exclude quoted route names only for Detail, leaving scientific readers unchanged."""
    result: list[str] = []
    for line in lines:
        tokens = orca_line_tokens(line)
        if tokens and tokens[0].value.startswith("!"):
            line = "" if tokens[0].quoted else "! " + " ".join(_route_tokens([line]))
        result.append(line)
    return result


def _operation_keyword_positions(lines: list[str]) -> set[tuple[int, int]]:
    """Markers the conservative guard sees, except route frequency aliases."""
    positions: set[tuple[int, int]] = set()
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        header = percent_directive_header(tokens)
        if header is not None and header[0].startswith("compound"):
            # Compound reference syntax is checked by its own evidence reader.
            continue
        positions.update(
            (line_index, token.start)
            for token in tokens
            if token.value.lower() in _OPERATION_KEYS | _FREQUENCY_SWITCHES
        )
    return positions - _route_frequency_keyword_positions(lines)


def _block_operation_kind(lines: list[str], *, named_neb: bool) -> str | None:
    """Positive operation evidence in closed blocks; uncertain values take precedence."""
    requested = False
    scan_proven = False
    seen: set[tuple[str, str]] = set()
    unexplained = _operation_keyword_positions(lines)
    for name in _NON_SINGLE_POINT_BLOCKS | _BLOCK_OPERATION_VALUES.keys():
        for block in iter_blocks(lines, name):
            located = [(row.line_index, token) for row in block.rows for token in row.tokens]
            tokens = [token for _line_index, token in located]
            if name in _NON_SINGLE_POINT_BLOCKS:
                if not block.closed:
                    return "unknown"
                unexplained.difference_update(
                    (line_index, token.start) for line_index, token in located if token.quoted
                )
                requested = True
                continue
            values = _BLOCK_OPERATION_VALUES[name]
            for index, (line_index, token) in enumerate(located):
                key = token.value.lower()
                if key not in values:
                    continue
                identity = (name, key)
                if token.quoted or not block.closed or identity in seen:
                    return "unknown"
                seen.add(identity)
                value_index = value_token_index(tokens, index)
                if (
                    value_index >= len(tokens)
                    or tokens[value_index].quoted
                    or tokens[value_index].value.lower() not in values[key]
                ):
                    return "unknown"
                unexplained.discard((line_index, token.start))
                if name == "freq" and tokens[value_index].value.lower() == "true" and named_neb:
                    return "unknown"
                if name != "freq" or tokens[value_index].value.lower() == "true":
                    requested = True
            if name == "geom" and any(
                not token.quoted and token.value.lower() == "scan" for token in tokens
            ):
                if not block.closed or not scan_coordinate_rows(lines[block.start : block.end + 1]):
                    return "unknown"
                scan_proven = True
                requested = True
    if unexplained or (scan_coordinate_rows(lines) is not None and not scan_proven):
        return "unknown"
    return "unsupported" if requested else None


def _quoted_text_is_closed(text: str) -> bool:
    """A tokenizer quote closes only after an even run of preceding backslashes."""
    if len(text) < 2 or text[0] not in {'"', "'"} or text[-1] != text[0]:
        return False
    prefix = text[:-1]
    return (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0


def _compound_operation_kind(lines: list[str]) -> str | None:
    """A complete compound file directive or route-bearing segments around $new_job."""
    requested = False
    new_jobs: list[int] = []
    for line_index, line in enumerate(lines):
        tokens = orca_line_tokens(line)
        if not tokens:
            continue
        if not tokens[0].quoted and tokens[0].value.lower() in {"$new_job", "$newjob"}:
            new_jobs.append(line_index)
        header = percent_directive_header(tokens)
        if header is None or not header[0].startswith("compound"):
            continue
        body = tokens[header[1] :]
        complete_file = (
            len(body) == 1
            and body[0].quoted
            and bool(body[0].value)
            and _quoted_text_is_closed(line[body[0].start : body[0].end])
        )
        if header[0] not in {"compound", "compound_file"} or not complete_file:
            return "unknown"
        requested = True
    if new_jobs:
        boundaries = [-1, *new_jobs, len(lines)]
        if any(
            not _route_tokens(lines[start + 1 : end])
            for start, end in zip(boundaries, boundaries[1:], strict=False)
        ):
            return "unknown"
        requested = True
    return "unsupported" if requested else None


def _unsupported_operation_kind(lines: list[str], tokens: list[str]) -> str:
    """Separate proven unsupported requests from the existing conservative guard."""
    block_kind = _block_operation_kind(
        lines, named_neb=any(token.lower() in _NEB_NAMED_KINDS for token in tokens)
    )
    compound_kind = _compound_operation_kind(lines)
    esd_markers = sum(
        token.upper() == "ESD" or token.upper().startswith("ESD(") for token in tokens
    )
    esd_requests = len(_ESD_OPERATION_RE.findall(" ".join(tokens)))
    uncertain_alias = any(
        _NON_SINGLE_POINT_TOKEN_RE.fullmatch(token)
        and not _DEFINITE_UNSUPPORTED_TOKEN_RE.fullmatch(token)
        and not (token.upper() == "ESD" or token.upper().startswith("ESD("))
        for token in tokens
    )
    if "unknown" in {block_kind, compound_kind} or esd_markers != esd_requests or uncertain_alias:
        return "unknown"
    if (
        "unsupported" in {block_kind, compound_kind}
        or esd_requests
        or any(_DEFINITE_UNSUPPORTED_TOKEN_RE.fullmatch(token) for token in tokens)
    ):
        return "unsupported"
    return "unknown"


def _detail_is_unsupported(lines: list[str], facts: RouteFacts, tokens: list[str]) -> bool:
    is_neb = any(token.lower() in _NEB_TOKENS for token in tokens)
    return (
        (facts.is_irc and any(token.lower() in _NEB_PATH_TOKENS for token in tokens))
        or (facts.is_non_stationary and not is_neb)
        or any(token.upper() == "MD" for token in tokens)
        or any(_NON_SINGLE_POINT_TOKEN_RE.fullmatch(token) for token in tokens)
        or scan_coordinate_rows(lines) is not None
        or _is_not_one_plain_job(lines)
    )


def _primary_route_kind(facts: RouteFacts, routes: str, tokens: list[str]) -> str | None:
    for token in tokens:
        if token.lower() in _NEB_NAMED_KINDS:
            return token.lower()
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
    primary = _primary_route_kind(facts, routes, tokens)
    if primary in _NEB_NAMED_KINDS and (facts.is_ts or facts.is_opt or SP_RE.search(routes)):
        return "unknown"
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
    lines = _presentation_input_lines(lines)
    route_lines = orca_route_lines(lines)
    tokens = _route_tokens(lines)
    if not tokens or any(
        _NEB_FAMILY_RE.fullmatch(token) and token.lower() not in _NEB_TOKENS for token in tokens
    ):
        return "unknown"
    neb_tokens = {token.lower() for token in tokens if token.lower() in _NEB_TOKENS}
    if neb_tokens & _NEB_NAMED_KINDS and len(neb_tokens) > 1:
        return "unknown"
    coarse = job_type_from_routes(route_lines)
    facts = route_facts(inp_path, lines=lines)
    routes = " ".join(route_lines)
    # A conservative non-stationary match needs an exact display operation;
    # check each token because a TS keyword suppresses the aggregate route fact.
    if any(
        token.lower() not in _NEB_TOKENS
        and token.upper() != "MD"
        and route_facts(inp_path, lines=["! " + token]).is_non_stationary
        for token in tokens
    ):
        return "unknown"
    kind = _assemble_detail_kind(facts, routes, tokens, coarse)
    if kind == "unknown":
        return "unknown"
    if _detail_is_unsupported(lines, facts, tokens):
        if facts.is_irc and any(token.lower() in _NEB_PATH_TOKENS for token in tokens):
            return "unknown"
        return _unsupported_operation_kind(lines, tokens)
    return kind


__all__ = [
    "QUEUE_DETAIL_KINDS",
    "QUEUE_DETAIL_KIND_KEY",
    "QUEUE_DETAIL_KIND_LABELS",
    "queue_detail_kind",
    "queue_detail_kind_label",
]
