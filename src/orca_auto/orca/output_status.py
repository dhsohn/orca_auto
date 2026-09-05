from __future__ import annotations

import re
from collections.abc import Iterable

NORMAL_TERMINATION_NEEDLES: tuple[str, ...] = ("ORCA TERMINATED NORMALLY",)

ERROR_TERMINATION_NEEDLES: tuple[str, ...] = (
    "ORCA FINISHED BY ERROR TERMINATION",
    "ABORTING THE RUN",
    "ENDED PREMATURELY AND MAY HAVE CRASHED",
    "FATAL ERROR",
)

_OPT_CONVERGED_NEEDLES = ("THE OPTIMIZATION HAS CONVERGED", "OPTIMIZATION RUN DONE")
_OPT_NOT_CONVERGED_RE = re.compile(
    r"THE OPTIMIZATION DID NOT CONVERGE|OPTIMIZATION HAS NOT YET CONVERGED|"
    r"ORCA GEOMETRY OPTIMIZATION.*(?:DID NOT CONVERGE|NOT CONVERGED)"
)


def optimization_convergence_line(line: str) -> bool | None:
    """Verdict on one output line; a negative marker takes precedence."""
    upper = line.upper()
    if _OPT_NOT_CONVERGED_RE.search(upper):
        return False
    if any(needle in upper for needle in _OPT_CONVERGED_NEEDLES):
        return True
    return None


def last_optimization_convergence(lines: Iterable[str]) -> bool | None:
    """Last explicit optimization verdict, shared by streaming and text readers."""
    verdict: bool | None = None
    for line in lines:
        current = optimization_convergence_line(line)
        if current is not None:
            verdict = current
    return verdict


def has_normal_termination(text: str) -> bool:
    upper = text.upper()
    return any(needle in upper for needle in NORMAL_TERMINATION_NEEDLES)


def has_error_termination(text: str) -> bool:
    upper = text.upper()
    return any(needle in upper for needle in ERROR_TERMINATION_NEEDLES)


def coarse_orca_status(
    text: str,
    *,
    opt_converged: bool | None = None,
    wall_time_seconds: int | None = None,
) -> str:
    if has_normal_termination(text):
        return "failed" if opt_converged is False else "completed"
    if has_error_termination(text):
        return "failed"
    if wall_time_seconds is not None:
        return "failed"
    return "running"
