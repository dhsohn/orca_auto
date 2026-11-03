"""Read and inject ``%pal nprocs`` / ``%maxcore`` resource directives of an ORCA input.

Each directive has one pattern here; ``input_validation`` counts duplicates
with the same patterns where the rules agree.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import InputSource, ParsedInput, parse_input, set_block_key_value
from .input_syntax import (
    OrcaLineToken,
    active_directive_text_from_tokens,
    active_orca_directive_text,
    find_route_idx,
    render_orca_input,
)

# A %maxcore directive; group 1 is its value when a number follows.
MAXCORE_RE = re.compile(r"^\s*%maxcore\b(?:\s+(\d+))?", re.IGNORECASE)
# ORCA block syntax accepts an optional "=" between key and value ("nprocs = 16").
NPROCS_RE = re.compile(r"\bnprocs(?:\s*=\s*|\s+)(\d+)\b", re.IGNORECASE)
# ORCA route-line shorthand "! PALn" (PAL2..PAL8) requests n parallel processes.
PAL_ROUTE_RE = re.compile(r"\bPAL(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PreparedSubmissionResourceInput:
    resource_request: dict[str, int]
    actions: tuple[str, ...]
    normalized_payload: bytes


def _maxcore_value(line: str) -> int | None:
    return _maxcore_match_value(MAXCORE_RE.match(active_orca_directive_text(line)))


def _maxcore_match_value(match: re.Match[str] | None) -> int | None:
    return int(match.group(1)) if match is not None and match.group(1) is not None else None


def _maxcore_matches(parsed: ParsedInput) -> list[re.Match[str]]:
    return [
        match
        for line, tokens in zip(parsed.lines, parsed.tokens, strict=True)
        if (match := _maxcore_directive(line, tokens)) is not None
    ]


def _maxcore_directive(line: str, tokens: tuple[OrcaLineToken, ...]) -> re.Match[str] | None:
    return MAXCORE_RE.match(active_directive_text_from_tokens(line, tokens))


def read_maxcore(lines: InputSource) -> int | None:
    values = [
        value
        for match in _maxcore_matches(parse_input(lines))
        if (value := _maxcore_match_value(match)) is not None
    ]
    return max(values) if values else None


def read_nprocs(lines: InputSource) -> int | None:
    # An explicit %pal block wins over the route-line shorthand; fall back to
    # "! PALn" so a PAL-only input is not treated as having no nprocs (which would
    # inject a conflicting %pal block and compute the wrong resource_request).
    parsed = parse_input(lines)
    values = [
        value
        for value in (
            _read_nprocs_from_pal_block(parsed),
            _read_nprocs_from_pal_shorthand(parsed),
        )
        if value is not None
    ]
    return max(values) if values else None


def _read_nprocs_from_pal_shorthand(parsed: ParsedInput) -> int | None:
    values: list[int] = []
    for route in parsed.route_lines:
        for match in PAL_ROUTE_RE.finditer(route):
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if value > 0:
                values.append(value)
    return max(values) if values else None


def _read_nprocs_from_pal_block(parsed: ParsedInput) -> int | None:
    # The block scan owns the block-termination rule (inline ``end``, ``end``
    # lines, the next ``%`` directive or the geometry section cutting an
    # unterminated block) and already drops ``# ... #`` and trailing comments.
    values = [
        value
        for block in parsed.blocks_named("pal")
        for row in block.rows
        if (value := read_nprocs_from_text(row.text)) is not None
    ]
    return max(values) if values else None


def read_nprocs_from_text(text: str) -> int | None:
    values: list[int] = []
    for match in NPROCS_RE.finditer(text):
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return max(values) if values else None


def maxcore_mb_per_core(*, max_memory_gb: int, max_cores: int) -> int:
    total_mb = max(1, int(max_memory_gb)) * 1024
    return max(1, total_mb // max(1, int(max_cores)))


def resource_request_from_lines(lines: InputSource) -> dict[str, int]:
    parsed = parse_input(lines)
    max_cores = read_nprocs(parsed)
    maxcore_mb = read_maxcore(parsed)
    if max_cores is None or maxcore_mb is None or maxcore_mb <= 0:
        return {}
    total_memory_gb = max(1, math.ceil((max_cores * maxcore_mb) / 1024))
    return {
        "max_cores": max_cores,
        "max_memory_gb": total_memory_gb,
    }


def prepare_submission_resource_request(
    inp_path: Path,
    source_payload: bytes,
    *,
    default_max_cores: int,
    default_max_memory_gb: int,
    parsed: ParsedInput | None = None,
) -> PreparedSubmissionResourceInput:
    """Normalize the resources of ``source_payload``, the bytes of ``inp_path``, in memory.

    ``parsed`` is the caller's parse of the same input. It is reused only when
    its lines are exactly the lines of the strict UTF-8 decode here; any other
    version is parsed afresh.
    """

    try:
        source_text = source_payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ValueError("ORCA selected input must be UTF-8 text") from exc
    lines = source_text.splitlines()
    # Each version of the input (as submitted, after each injection) is parsed
    # once and every reader of that version shares the parse.
    if parsed is None or parsed.lines != tuple(lines):
        parsed = parse_input(lines)
    # A missing %maxcore gets the configured value; a present one without a
    # readable value is refused, since replacing it would guess its intent and
    # keeping it beside an injected one leaves ORCA two directives.
    if any(match.group(1) is None for match in _maxcore_matches(parsed)):
        raise ValueError(f"ORCA input has a %maxcore directive without a value in MB: {inp_path}")
    actions: list[str] = []

    max_cores = read_nprocs(parsed)
    if max_cores is None:
        configured_cores = max(1, int(default_max_cores))
        if set_block_key_value(lines, "pal", "nprocs", str(configured_cores)):
            actions.append("pal_nprocs_injected")
        # The editor may close an unterminated block without reporting a change.
        parsed = parse_input(lines)
        max_cores = read_nprocs(parsed) or configured_cores

    maxcore_mb = read_maxcore(parsed)
    if maxcore_mb is None or maxcore_mb <= 0:
        configured_maxcore = maxcore_mb_per_core(
            max_memory_gb=max(1, int(default_max_memory_gb)),
            max_cores=max_cores,
        )
        if set_maxcore(lines, configured_maxcore):
            actions.append("maxcore_injected")
        parsed = parse_input(lines)

    resource_request = resource_request_from_lines(parsed)
    if not resource_request:
        raise ValueError(f"Could not determine ORCA resource_request from input: {inp_path}")
    normalized_payload = render_orca_input(lines).encode("utf-8")
    return PreparedSubmissionResourceInput(
        resource_request=resource_request,
        actions=tuple(actions),
        normalized_payload=normalized_payload,
    )


def set_maxcore(lines: list[str], value_mb: int) -> bool:
    matches = [index for index, line in enumerate(lines) if _maxcore_value(line) is not None]
    if matches:
        new_line = f"%maxcore {value_mb}"
        first = matches[0]
        changed = lines[first] != new_line or len(matches) > 1
        lines[first] = new_line
        for index in reversed(matches[1:]):
            del lines[index]
        return changed
    insert_at = find_route_idx(lines)
    if insert_at is not None:
        insert_at += 1
    else:
        insert_at = 0
    lines.insert(insert_at, f"%maxcore {value_mb}")
    return True
