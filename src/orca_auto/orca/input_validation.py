"""Fail-closed admission checks on a whole ORCA input.

Top of the input-handling stack: these validators combine the tokenizer
(:mod:`.input_syntax`), block walking (:mod:`.input_blocks`) and ``MOInp``
scanning (:mod:`.input_references`) to reject inputs whose geometry form or
resource/checkpoint directives the runtime cannot bind unambiguously. They
only raise ``ValueError``; rewriting stays with the modules below.
"""

from __future__ import annotations

import re

from .input_blocks import (
    GRADIENT_RUN_TYPES,
    SINGLE_POINT_RUN_TYPES,
    geometry_header_match,
    iter_blocks,
)
from .input_references import orca_moinp_references
from .input_syntax import active_orca_directive_text, orca_line_tokens, orca_route_tokens
from .operation_facts import OperationFacts
from .resource_directives import MAXCORE_RE, NPROCS_RE

COORDS_BLOCK_RE = re.compile(r"^\s*%\s*coords\b", re.IGNORECASE)
# Counts whole unquoted route tokens, the PAL keywords ORCA parses. The reader
# (resource_directives.PAL_ROUTE_RE) scans the route text for a core count, so
# it also sees quoted or compound tokens; the two cannot share one pattern.
PAL_ROUTE_TOKEN_RE = re.compile(r"\APAL\d+\Z", re.IGNORECASE)
# ``%method RunTyp`` values whose job the completion analyzer and the execution
# binding classify correctly: a plain single point, or a single point with a
# gradient (completion needs the same evidence as ``sp``; the binding reserves
# the ``.engrad`` it writes). Every other value -- an optimization spelled
# ``Opt``/``Geom``/``Geometry``/``GeometryOpt``, dynamics, or anything unknown --
# would be admitted but verified as a single point, so it is refused.
SUPPORTED_METHOD_RUN_TYPES = SINGLE_POINT_RUN_TYPES | GRADIENT_RUN_TYPES


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
            # The shared reader decides what the header says, comments
            # included (``# note # * xyz 0 1``, ``* xyz 0 # c # 1``), exactly
            # as the binding that reads the geometry does. Its active text
            # drops quotes, so a quoted keyword, charge or multiplicity stays
            # refused here; a quoted xyzfile path is the fifth token.
            match = geometry_header_match(line)
            if (
                match is None
                or match.group(1).lower() not in {"xyz", "xyzfile"}
                or any(token.quoted for token in tokens[:4])
            ):
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


def validate_supported_method_run_type(facts: OperationFacts, *, label: str) -> None:
    """Reject a ``%method RunTyp`` whose job orca_auto would verify as the wrong kind.

    Request an optimization with a route keyword (``! Opt``) instead; that is
    the form completion, reports and the execution binding classify.
    """

    for run_type in facts.method_run_types:
        if run_type.value is None or run_type.quoted:
            raise ValueError(f"{label} has an unreadable %method RunTyp value")
        if run_type.value.lower() not in SUPPORTED_METHOD_RUN_TYPES:
            raise ValueError(
                f"{label} uses unsupported %method RunTyp {run_type.value}; request an "
                "optimization or other job type with a route keyword such as ! Opt"
            )


def validate_consistent_operation(facts: OperationFacts, *, label: str) -> None:
    """Reject a block setting that changes the job the route keywords request.

    Completion, the geometry scope and the execution binding classify a job
    from its route keywords (``facts.route``), but ORCA
    reads ``%`` blocks after the route, so a block can change the job:

    - ``%geom TS_search`` turns a route optimization (``Opt``, ``TightOpt``, ...)
      into a transition-state search, which would then be verified as a minimum;
    - a ``%method RunTyp`` (only ``Energy``/``SP`` or a gradient value reach
      here) replaces a route optimization, TS search, IRC, NEB or MD, which
      would then be failed for missing optimization evidence.

    Either is refused so the route alone states the job. Where the setting
    agrees with the route or changes nothing completion checks (``TS_search``
    with ``! OptTS`` or on a route that optimizes nothing, ``RunTyp`` on a
    single-point route, a separate frequency request) it is accepted.
    """

    route = facts.route
    if facts.declares_ts_search and route.is_opt and not route.is_ts:
        raise ValueError(
            f"{label} uses %geom TS_search on a non-TS optimization route; request "
            "the TS search with a route keyword such as ! OptTS"
        )
    run_types = [run_type.value for run_type in facts.method_run_types if run_type.value]
    route_operation = next(
        (
            name
            for name, requested in (
                ("TS search", route.is_ts),
                ("IRC", route.is_irc),
                ("optimization", route.is_opt),
                ("NEB or MD", route.is_non_stationary),
            )
            if requested
        ),
        None,
    )
    if run_types and route_operation is not None:
        raise ValueError(
            f"{label} sets %method RunTyp {run_types[-1]}, which overrides the route's "
            f"{route_operation}; keep only one of the two declarations"
        )


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
        len(NPROCS_RE.findall(row.text)) for block in pal_blocks for row in block.rows
    )
    pal_route_count = 0
    for line in lines:
        if MAXCORE_RE.match(active_orca_directive_text(line)):
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
