"""Presentation-only queue ``detail_kind`` from the admitted input lines.

:func:`.job_type.job_type_from_routes` keeps its coarse scientific meaning.
:func:`queue_detail_kind` records what the input requests for the queue
``Detail`` column from the same single read at admission.
"""

from __future__ import annotations

import re
from pathlib import Path

from .input_blocks import (
    GRADIENT_RUN_TYPES,
    OPERATION_BLOCK_KEYS,
    InputSource,
    ParsedInput,
    parse_input,
    percent_directive_header,
)
from .job_type import FREQ_RE, SP_RE, job_type_from_routes
from .route_rules import RouteFacts, is_non_stationary_route, route_facts

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

# Exact presentation aliases only; scientific route rules stay in route_rules.
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
_FREQUENCY_SWITCHES = OPERATION_BLOCK_KEYS["freq"]
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
    "method": {"runtyp": GRADIENT_RUN_TYPES},
}


def queue_detail_kind_label(detail_kind: str) -> str | None:
    """Fixed Detail label for a normalized ``detail_kind``, or ``None`` when unrecognized."""
    normalized = detail_kind.strip()
    if not normalized or normalized != normalized.lower():
        return None
    return QUEUE_DETAIL_KIND_LABELS.get(normalized)


def _disabled_frequency_switches(parsed: ParsedInput) -> set[tuple[int, int]]:
    """``(line, column)`` of each switch a closed ``%freq`` body sets to a bare ``false``."""
    return {
        (declaration.line_index, declaration.column)
        for declaration in parsed.operation_declarations
        if declaration.block == "freq"
        and declaration.block_closed
        and not declaration.key_quoted
        and declaration.value is not None
        and not declaration.value.quoted
        and declaration.value.value.lower() == "false"
    }


def _route_frequency_keyword_positions(parsed: ParsedInput) -> set[tuple[int, int]]:
    """``AnFreq``/``NumFreq`` on a route line are FREQ keywords, not ``%freq`` block switches."""
    positions: set[tuple[int, int]] = set()
    for line_index, route_tokens in enumerate(parsed.route_tokens):
        for token in route_tokens:
            if not token.quoted and token.value.lower() in _FREQUENCY_SWITCHES:
                positions.add((line_index, token.start))
    return positions


def _is_not_one_plain_job(parsed: ParsedInput) -> bool:
    """Whether the input runs more than one plain single point outside its routes."""
    route_freq = _route_frequency_keyword_positions(parsed)
    switches: set[tuple[int, int]] = set()
    for line_index, tokens in enumerate(parsed.tokens):
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
    return bool(switches - _disabled_frequency_switches(parsed))


def _route_tokens(parsed: ParsedInput, start: int = 0, stop: int | None = None) -> list[str]:
    """The unquoted route token values of the lines ``start`` to ``stop``."""
    return [
        token.value
        for route_tokens in parsed.route_tokens[start:stop]
        for token in route_tokens
        if not token.quoted
    ]


def _presentation_input_lines(parsed: ParsedInput) -> list[str]:
    """Exclude quoted route names only for Detail, leaving scientific readers unchanged."""
    result: list[str] = []
    for line_index, (line, tokens) in enumerate(zip(parsed.lines, parsed.tokens, strict=True)):
        if tokens and tokens[0].value.startswith("!"):
            line = (
                ""
                if tokens[0].quoted
                else "! " + " ".join(_route_tokens(parsed, line_index, line_index + 1))
            )
        result.append(line)
    return result


def _operation_keyword_positions(parsed: ParsedInput) -> set[tuple[int, int]]:
    """Markers the conservative guard sees, except route frequency aliases."""
    positions: set[tuple[int, int]] = set()
    for line_index, tokens in enumerate(parsed.tokens):
        header = percent_directive_header(tokens)
        if header is not None and header[0].startswith("compound"):
            # Compound reference syntax is checked by its own evidence reader.
            continue
        positions.update(
            (line_index, token.start)
            for token in tokens
            if token.value.lower() in _OPERATION_KEYS | _FREQUENCY_SWITCHES
        )
    return positions - _route_frequency_keyword_positions(parsed)


def _block_operation_kind(parsed: ParsedInput, *, named_neb: bool) -> str | None:
    """Positive operation evidence in closed blocks; uncertain values take precedence."""
    requested = False
    scan_proven = False
    seen: set[tuple[str, str]] = set()
    unexplained = _operation_keyword_positions(parsed)
    for name in _NON_SINGLE_POINT_BLOCKS:
        for block in parsed.blocks_named(name):
            if not block.closed:
                return "unknown"
            unexplained.difference_update(
                (row.line_index, token.start)
                for row in block.rows
                for token in row.tokens
                if token.quoted
            )
            requested = True
    # The operation keys are read by the shared block reader; this policy
    # decides what an uncertain declaration (quoted, unclosed, repeated or with
    # an unknown value) means for the Detail: an unknown operation.
    for declaration in parsed.operation_declarations:
        values = _BLOCK_OPERATION_VALUES[declaration.block]
        if declaration.key not in values:
            continue
        identity = (declaration.block, declaration.key)
        if declaration.key_quoted or not declaration.block_closed or identity in seen:
            return "unknown"
        seen.add(identity)
        value = declaration.value
        if value is None or value.quoted or value.value.lower() not in values[declaration.key]:
            return "unknown"
        unexplained.discard((declaration.line_index, declaration.column))
        if declaration.block == "freq" and value.value.lower() == "true" and named_neb:
            return "unknown"
        if declaration.block != "freq" or value.value.lower() == "true":
            requested = True
    for block in parsed.blocks_named("geom"):
        if any(
            not token.quoted and token.value.lower() == "scan"
            for row in block.rows
            for token in row.tokens
        ):
            if not block.closed or not parsed.scan_coordinate_rows_between(
                block.start, block.end + 1
            ):
                return "unknown"
            scan_proven = True
            requested = True
    if unexplained or (parsed.scan_coordinate_rows is not None and not scan_proven):
        return "unknown"
    return "unsupported" if requested else None


def _quoted_text_is_closed(text: str) -> bool:
    """A tokenizer quote closes only after an even run of preceding backslashes."""
    if len(text) < 2 or text[0] not in {'"', "'"} or text[-1] != text[0]:
        return False
    prefix = text[:-1]
    return (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0


def _compound_operation_kind(parsed: ParsedInput) -> str | None:
    """A complete compound file directive or route-bearing segments around $new_job."""
    requested = False
    new_jobs: list[int] = []
    for line_index, (line, tokens) in enumerate(zip(parsed.lines, parsed.tokens, strict=True)):
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
        boundaries = [-1, *new_jobs, len(parsed.lines)]
        if any(
            not _route_tokens(parsed, start + 1, end)
            for start, end in zip(boundaries, boundaries[1:], strict=False)
        ):
            return "unknown"
        requested = True
    return "unsupported" if requested else None


def _unsupported_operation_kind(parsed: ParsedInput, tokens: list[str]) -> str:
    """Separate proven unsupported requests from the existing conservative guard."""
    block_kind = _block_operation_kind(
        parsed, named_neb=any(token.lower() in _NEB_NAMED_KINDS for token in tokens)
    )
    compound_kind = _compound_operation_kind(parsed)
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


def _detail_is_unsupported(parsed: ParsedInput, facts: RouteFacts, tokens: list[str]) -> bool:
    is_neb = any(token.lower() in _NEB_TOKENS for token in tokens)
    return (
        (facts.is_irc and any(token.lower() in _NEB_PATH_TOKENS for token in tokens))
        or (facts.is_non_stationary and not is_neb)
        or any(token.upper() == "MD" for token in tokens)
        or any(_NON_SINGLE_POINT_TOKEN_RE.fullmatch(token) for token in tokens)
        or parsed.scan_coordinate_rows is not None
        or _is_not_one_plain_job(parsed)
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


def queue_detail_kind(inp_path: Path, lines: InputSource) -> str:
    """Normalized detail kind for queue metadata, or ``""`` when there is no route line.

    The Detail reads a presentation version of the input (quoted route names
    dropped), parsed once; every reader below shares that one parse.
    """
    source = parse_input(lines)
    if not source.route_lines:
        return ""
    parsed = parse_input(_presentation_input_lines(source))
    route_lines = list(parsed.route_lines)
    tokens = _route_tokens(parsed)
    if not tokens or any(
        _NEB_FAMILY_RE.fullmatch(token) and token.lower() not in _NEB_TOKENS for token in tokens
    ):
        return "unknown"
    neb_tokens = {token.lower() for token in tokens if token.lower() in _NEB_TOKENS}
    if neb_tokens & _NEB_NAMED_KINDS and len(neb_tokens) > 1:
        return "unknown"
    coarse = job_type_from_routes(route_lines)
    facts = route_facts(inp_path, lines=parsed)
    routes = " ".join(route_lines)
    # A conservative non-stationary match needs an exact display operation;
    # check each token because a TS keyword suppresses the aggregate route fact.
    if any(
        token.lower() not in _NEB_TOKENS
        and token.upper() != "MD"
        and is_non_stationary_route(token)
        for token in tokens
    ):
        return "unknown"
    kind = _assemble_detail_kind(facts, routes, tokens, coarse)
    if kind == "unknown":
        return "unknown"
    if _detail_is_unsupported(parsed, facts, tokens):
        if facts.is_irc and any(token.lower() in _NEB_PATH_TOKENS for token in tokens):
            return "unknown"
        return _unsupported_operation_kind(parsed, tokens)
    return kind


__all__ = [
    "QUEUE_DETAIL_KINDS",
    "QUEUE_DETAIL_KIND_KEY",
    "QUEUE_DETAIL_KIND_LABELS",
    "queue_detail_kind",
    "queue_detail_kind_label",
]
