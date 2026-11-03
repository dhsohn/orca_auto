"""Completion policy over an ORCA input's :class:`~.route_rules.RouteFacts`.

The completion analyzer's mode, which report sections and which SI block a job
gets, and whether its geometry is a stationary point all come from one
:func:`~.route_rules.route_facts`, so those answers cannot disagree on one
input; the geometry scope that bounds a stationary-point claim is
:func:`geometry_scope` for machine.json, the HTML report and the SI block
alike. Each caller reads the input file itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .input_syntax import read_input_text
from .route_rules import RouteFacts, route_facts


def geometry_scope(route: RouteFacts) -> str:
    """What the final geometry of ``route`` can be, before any output evidence.

    ``"transition_state"`` (an unconstrained TS search), ``"full"`` (a full
    optimization), ``"partial"`` (a restricted optimization, a constrained TS
    search included), ``"path"`` (scan, IRC, NEB or MD) or ``"single_point"``.
    Only ``"transition_state"`` and ``"full"`` can earn a stationary-point
    label, and only with matching frequency evidence. The TS operation itself
    (``is_ts``, the ``ts`` completion mode, the ``TS`` report kind) is
    unchanged by constraints: a constrained OptTS is still a TS search, never
    a single point.
    """
    if route.is_ts:
        return "partial" if route.has_constraints else "transition_state"
    if route.is_relaxed_scan or route.is_irc or route.is_non_stationary:
        return "path"
    if route.is_full_opt:
        return "full"
    if route.is_opt:
        return "partial"
    return "single_point"


def is_constrained_ts_search(route: RouteFacts) -> bool:
    """A TS search whose coordinates are restricted: its saddle stays unverified."""
    return route.is_ts and geometry_scope(route) == "partial"


@dataclass
class CompletionMode:
    """Requested work whose positive evidence the analyzer must verify."""

    kind: str
    require_irc: bool
    require_frequency: bool = False


class CompletionInputUnreadableError(RuntimeError):
    """The input whose completion requirements are being decided cannot be read."""


def detect_completion_mode(inp_path: Path) -> CompletionMode:
    """The completion requirements of the input at ``inp_path``.

    An input that cannot be read raises :class:`CompletionInputUnreadableError`.
    Its requirements are unknown, and taking it for an empty input would
    verify an optimization or frequency job as a single point.
    """
    text = read_input_text(inp_path)
    if text is None:
        raise CompletionInputUnreadableError(
            f"ORCA input cannot be read to decide its completion requirements: {inp_path}"
        )
    return completion_mode(route_facts(inp_path, lines=text.splitlines()))


def completion_mode(route: RouteFacts) -> CompletionMode:
    """Completion requirements from already-read input facts."""
    # Only differences in completion requirements need a mode. IRC/Freq
    # requirements are flags; detailed job labels remain in RouteFacts.
    kind = "ts" if route.is_ts else "opt" if route.is_opt else "sp"
    return CompletionMode(
        kind=kind,
        require_irc=route.is_irc,
        require_frequency=route.is_ts or route.requests_frequency,
    )
