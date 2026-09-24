from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

NORMAL_TERMINATION_NEEDLES: tuple[str, ...] = ("ORCA TERMINATED NORMALLY",)

ERROR_TERMINATION_NEEDLES: tuple[str, ...] = (
    "ORCA FINISHED BY ERROR TERMINATION",
    "ABORTING THE RUN",
    "ENDED PREMATURELY AND MAY HAVE CRASHED",
    "FATAL ERROR",
    # An input-block syntax error (a malformed ``%geom`` entry, for example)
    # makes ORCA 6 print ``... check syntax!`` and ``LEAVING ORCA`` and exit
    # within a second, without any of the termination banners above.
    "CHECK SYNTAX",
    "LEAVING ORCA",
)

_INPUT_ECHO_RE = re.compile(r"\|\s*\d+>")

_OPT_CONVERGED_NEEDLES = ("THE OPTIMIZATION HAS CONVERGED", "OPTIMIZATION RUN DONE")
_OPT_NOT_CONVERGED_RE = re.compile(
    r"THE OPTIMIZATION DID NOT CONVERGE|OPTIMIZATION HAS NOT YET CONVERGED|"
    r"ORCA GEOMETRY OPTIMIZATION.*(?:DID NOT CONVERGE|NOT CONVERGED)"
)


def is_execution_output_line(line: str) -> bool:
    """Exclude input echoes and comments from execution diagnostics."""
    stripped = line.lstrip()
    return not (stripped.startswith("#") or _INPUT_ECHO_RE.match(stripped))


def optimization_convergence_line(line: str) -> bool | None:
    """Verdict on one output line; a negative marker takes precedence."""
    if not is_execution_output_line(line):
        return None
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


def termination_line(line: str) -> tuple[bool, bool]:
    """Normal/error termination evidence, excluding input echoes and comments."""
    if not is_execution_output_line(line):
        return False, False
    upper = line.upper()
    return (
        any(needle in upper for needle in NORMAL_TERMINATION_NEEDLES),
        any(needle in upper for needle in ERROR_TERMINATION_NEEDLES),
    )


_OUTPUT_NEWLINE_RE = re.compile(r"\r\n|[\r\n]")


def iter_output_lines(text: str) -> Iterator[str]:
    """StringIO(newline=None) semantics without cloning the entire buffer.

    Only CR, LF and CRLF are newlines; unicode separators and control
    characters inside a line must not expose commented/echoed diagnostics.
    """
    start = 0
    for match in _OUTPUT_NEWLINE_RE.finditer(text):
        yield text[start : match.start()] + "\n"
        start = match.end()
    if start < len(text):
        yield text[start:]


def has_normal_termination(text: str) -> bool:
    return any(termination_line(line)[0] for line in iter_output_lines(text))


def has_error_termination(text: str) -> bool:
    return any(termination_line(line)[1] for line in iter_output_lines(text))
