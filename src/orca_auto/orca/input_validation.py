"""Fail-closed admission checks on a whole ORCA input.

Top of the input-handling stack: these validators combine the tokenizer
(:mod:`.input_syntax`), block walking (:mod:`.input_blocks`) and ``MOInp``
scanning (:mod:`.input_references`) to reject inputs whose geometry form or
resource/checkpoint directives the runtime cannot bind unambiguously. They
only raise ``ValueError``; rewriting stays with the modules below.
"""

from __future__ import annotations

import re

from .input_blocks import GEOM_HEADER_RE, iter_blocks
from .input_references import orca_moinp_references
from .input_syntax import active_orca_directive_text, orca_line_tokens, orca_route_tokens

COORDS_BLOCK_RE = re.compile(r"^\s*%\s*coords\b", re.IGNORECASE)
MAXCORE_DIRECTIVE_RE = re.compile(r"^\s*%maxcore\b", re.IGNORECASE)
NPROCS_DIRECTIVE_RE = re.compile(r"\bnprocs\s+\d+\b", re.IGNORECASE)
PAL_ROUTE_TOKEN_RE = re.compile(r"\APAL\d+\Z", re.IGNORECASE)


def validate_supported_xyz_geometry_syntax(
    lines: list[str],
    *,
    label: str,
) -> None:
    """Fail closed for geometry forms whose atom count and dependencies are not bound."""

    validate_unambiguous_orca_directives(lines, label=label)
    geometry_block_count = 0
    inline_geometry_open = False
    for line in lines:
        stripped = line.strip()
        tokens = orca_line_tokens(line)
        if not tokens:
            continue
        first_lower = tokens[0].value.lower()
        if any(
            not token.quoted and token.value.lower() == "scants"
            for token in orca_route_tokens(line)
        ):
            raise ValueError(f"{label} uses unsupported ORCA route keyword ScanTS")
        if any(
            token.value.lower() in {"compound", "compound_file"}
            or token.value.lower().startswith("%compound")
            for token in orca_route_tokens(line)
            if not token.quoted
        ):
            raise ValueError(f"{label} uses an unsupported multiple-job geometry construct")
        spaced_percent_keyword = (
            f"%{tokens[1].value.lower()}" if len(tokens) >= 2 and tokens[0].value == "%" else ""
        )
        if (
            COORDS_BLOCK_RE.match(stripped)
            or first_lower == "%coords"
            or spaced_percent_keyword == "%coords"
        ):
            raise ValueError(f"{label} uses an unsupported %coords geometry block")
        if (
            first_lower in {"$new_job", "$newjob", "compound", "compound_file"}
            or first_lower.startswith("%compound")
            or spaced_percent_keyword.startswith("%compound")
        ):
            raise ValueError(f"{label} uses an unsupported multiple-job geometry construct")
        first = tokens[0].value
        if stripped == "*" or (first == "*" and len(tokens) == 1):
            if not inline_geometry_open:
                raise ValueError(f"{label} has an unexpected ORCA geometry terminator")
            inline_geometry_open = False
            continue
        if first == "*" or first.startswith("*"):
            if inline_geometry_open:
                raise ValueError(f"{label} has an unterminated inline ORCA geometry block")
            match = GEOM_HEADER_RE.match(stripped)
            if match is None or match.group(1).lower() not in {"xyz", "xyzfile"}:
                raise ValueError(f"{label} uses an unsupported ORCA geometry format")
            geometry_type = match.group(1).lower()
            expected_token_count = 5 if geometry_type == "xyzfile" else 4
            if len(tokens) != expected_token_count:
                raise ValueError(f"{label} has an invalid {geometry_type} geometry header")
            geometry_block_count += 1
            if geometry_block_count > 1:
                raise ValueError(f"{label} uses unsupported multiple ORCA geometry blocks")
            inline_geometry_open = geometry_type == "xyz"
    if geometry_block_count != 1:
        raise ValueError(f"{label} must define exactly one supported ORCA geometry block")
    if inline_geometry_open:
        raise ValueError(f"{label} has an unterminated inline ORCA geometry block")


def validate_unambiguous_orca_directives(lines: list[str], *, label: str) -> None:
    """Reject duplicate resource/checkpoint directives with unclear ORCA precedence."""

    maxcore_count = 0
    moinp_count = len(orca_moinp_references(lines))
    # ``iter_blocks`` owns the block-termination rule, so the ``nprocs`` count
    # covers exactly the body rows that ``read_nprocs`` reads: tokens after a
    # closing ``end`` (which ORCA does not parse as %pal content) are ignored,
    # while duplicate blocks and duplicate ``nprocs`` rows stay rejected.
    pal_blocks = list(iter_blocks(lines, "pal"))
    pal_block_count = len(pal_blocks)
    pal_nprocs_count = sum(
        len(NPROCS_DIRECTIVE_RE.findall(row.text)) for block in pal_blocks for row in block.rows
    )
    pal_route_count = 0
    for line in lines:
        if MAXCORE_DIRECTIVE_RE.match(active_orca_directive_text(line)):
            maxcore_count += 1
        pal_route_count += sum(
            1
            for token in orca_route_tokens(line)
            if not token.quoted and PAL_ROUTE_TOKEN_RE.fullmatch(token.value)
        )

    duplicate_labels = [
        name
        for name, count in (
            ("%maxcore", maxcore_count),
            ("%moinp", moinp_count),
            ("%pal blocks", pal_block_count),
            ("%pal nprocs", pal_nprocs_count),
            ("PAL route shorthands", pal_route_count),
        )
        if count > 1
    ]
    if pal_block_count and pal_route_count:
        duplicate_labels.append("mixed %pal and PAL route shorthands")
    if duplicate_labels:
        raise ValueError(
            f"{label} has ambiguous duplicate ORCA directives: {', '.join(duplicate_labels)}"
        )
