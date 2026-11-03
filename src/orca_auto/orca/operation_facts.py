"""The operation an ORCA input requests, read once: :class:`OperationFacts`.

Facts, not policy. :func:`parse_operation_facts` reads every declaration that
says what the input runs: the route keywords (as :class:`RouteFacts`), the
``%method RunTyp`` values, a ``%geom TS_search`` key, and the route and
``%neb`` settings that decide which files ORCA writes next to the input.
Consumers turn these facts into their own decisions:

- submission (:mod:`.input_validation`) refuses declarations it cannot verify
  or that contradict each other;
- the execution binding (``execution_binding._inputs``) reserves the outputs
  the operation writes;
- completion (:func:`.completion_rules.completion_mode`,
  :func:`.completion_rules.geometry_scope`), the queue Detail and the coarse
  ``job_type`` keep their own policies over the same route facts.

Every fact reads one :class:`~.input_blocks.ParsedInput` of the input version
it is given, so the input is tokenized once per call.

A requested frequency (``route.requests_frequency``) and frequency evidence
that completion requires (a TS search always needs it) are deliberately
separate: the first is a fact here, the second is completion policy.
``tests/contracts/test_rule_pins_operation.py`` pins each consumer's answer for
the same inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .input_blocks import InputSource, ParsedInput, parse_input
from .input_syntax import value_token_index
from .route_rules import IRC_ROUTE_RE, OPT_ROUTE_RE, PARTIAL_OPT_ROUTE_RE, RouteFacts, route_facts

NEB_ROUTE_RE = re.compile(r"\b(?:ZOOM-)?NEB(?:-(?:TS|CI))?\b", re.IGNORECASE)
# GOAT global optimizations rewrite <basename>.xyz and <basename>.engrad.
GOAT_ROUTE_RE = re.compile(
    r"\bGOAT(?:-(?:ENTROPY|EXPLORE|REACT|DIVERSITY|TS|COARSE))?\b", re.IGNORECASE
)
# ORCA 6.1 %neb spellings of the end-point preoptimization switch (default off)
# and the boolean values it reads as off.
_NEB_PREOPT_KEYS = frozenset({"preopt", "preopt_ends", "preopt_endpoints", "preopt_minima"})
_ORCA_FALSE_VALUES = frozenset({"false", "no", "off", "0"})
# Whole route keywords that write <basename>.engrad, besides the optimization,
# IRC and GOAT families matched by pattern.
_ENGRAD_EXACT_ROUTE_KEYWORDS = frozenset({"engrad", "energygrad", "numgrad", "optts", "optts(gmf)"})


@dataclass(frozen=True)
class MethodRunType:
    """One unquoted ``RunTyp`` key of a ``%method`` body and its value token."""

    value: str | None  # None: the key has no value
    quoted: bool = False


@dataclass(frozen=True)
class OperationFacts:
    """What one input's declarations request, before any consumer's policy."""

    route: RouteFacts
    method_run_types: tuple[MethodRunType, ...]
    declares_ts_search: bool  # an unquoted TS_search key in a %geom body
    route_writes_engrad: bool  # a route keyword whose job writes <basename>.engrad
    neb_route: bool  # an NEB-family route keyword (NEB, NEB-CI, NEB-TS, ZOOM-)
    goat_route: bool  # a GOAT-family route keyword
    neb_preopt_ends: bool  # %neb preoptimizes the end points (unreadable counts as on)


def parse_operation_facts(lines: InputSource, *, inp_path: Path) -> OperationFacts:
    """Read the operation facts of the input ``lines`` read from ``inp_path``.

    ``lines`` may be the input's lines or its :class:`~.input_blocks.ParsedInput`;
    every fact here reads that one parse. ``inp_path`` only names the input in
    :class:`RouteFacts`; nothing is read from it.
    """

    parsed = parse_input(lines)
    route = route_facts(inp_path, lines=parsed)
    routes = " ".join(route.route_lines)
    declarations = [
        declaration for declaration in parsed.operation_declarations if not declaration.key_quoted
    ]
    return OperationFacts(
        route=route,
        method_run_types=tuple(
            MethodRunType(value=None)
            if declaration.value is None
            else MethodRunType(value=declaration.value.value, quoted=declaration.value.quoted)
            for declaration in declarations
            if declaration.block == "method"
        ),
        declares_ts_search=any(declaration.block == "geom" for declaration in declarations),
        route_writes_engrad=_route_writes_engrad(parsed),
        neb_route=NEB_ROUTE_RE.search(routes) is not None,
        goat_route=GOAT_ROUTE_RE.search(routes) is not None,
        neb_preopt_ends=_neb_preoptimizes_end_points(parsed),
    )


def _route_writes_engrad(parsed: ParsedInput) -> bool:
    for tokens in parsed.route_tokens:
        for token in tokens:
            if token.quoted:
                continue
            # Whole keywords only: InterpOpt, RigidBodyOpt and the MD-driven
            # MD-L-Opt/MD-L-OptH emit no engrad file.
            if token.value.casefold() in _ENGRAD_EXACT_ROUTE_KEYWORDS or any(
                pattern.fullmatch(token.value) is not None
                for pattern in (OPT_ROUTE_RE, PARTIAL_OPT_ROUTE_RE, IRC_ROUTE_RE, GOAT_ROUTE_RE)
            ):
                return True
    return False


def _neb_preoptimizes_end_points(parsed: ParsedInput) -> bool:
    """Whether ``%neb`` enables end-point preoptimization; an unreadable value counts as on."""

    for block in parsed.blocks_named("neb"):
        tokens = [token for row in block.rows for token in row.tokens]
        for index, token in enumerate(tokens):
            if token.quoted:
                continue
            word = token.value.lower()
            # iter_blocks closes %neb at the Monitor_Internals sub-block's end
            # (the brace may be attached to the keyword), so switches after it
            # are unreadable.
            if word.startswith("monitor_internals"):
                return True
            if word not in _NEB_PREOPT_KEYS:
                continue
            value_index = value_token_index(tokens, index)
            if (
                value_index >= len(tokens)
                or tokens[value_index].quoted
                or tokens[value_index].value.lower() not in _ORCA_FALSE_VALUES
            ):
                return True
    return False


__all__ = ["MethodRunType", "OperationFacts", "parse_operation_facts"]
