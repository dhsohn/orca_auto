"""Dependency-free ANSI styling helpers for terminal output.

The project intentionally keeps a single runtime dependency (PyYAML), so this
module hand-rolls the small amount of ANSI handling the CLI needs: TTY
detection, ``NO_COLOR``/``FORCE_COLOR``/``--no-color`` support, and a couple of
``paint`` helpers. Messenger output never routes through here.
"""

from __future__ import annotations

import os
import sys
from typing import IO

from orca_auto.core import statuses as _s

_RESET = "\033[0m"

# Foreground SGR codes.
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"
DIM = "2"
BOLD = "1"

# Process-wide override set by the CLI when ``--no-color`` is passed. ``None``
# means "decide from the environment / TTY".
_color_override: bool | None = None


def set_color_override(enabled: bool | None) -> None:
    """Force color on/off for the rest of the process (``None`` resets)."""

    global _color_override
    _color_override = enabled


def color_enabled(stream: IO[str] | None = None) -> bool:
    """Return whether ANSI color should be emitted for ``stream``.

    Precedence: explicit ``--no-color`` override, then ``NO_COLOR`` (disable)
    and ``FORCE_COLOR`` (enable) environment variables, then TTY detection.
    """

    if _color_override is not None:
        return _color_override
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


_ACTIVITY_STATUS_COLORS = {
    _s.STATUS_CREATED: DIM,
    _s.STATUS_PLANNED: DIM,
    _s.STATUS_PENDING: DIM,
    _s.STATUS_QUEUED: DIM,
    _s.STATUS_SUBMITTED: CYAN,
    _s.STATUS_RUNNING: BLUE,
    _s.STATUS_RETRYING: YELLOW,
    _s.STATUS_CANCEL_REQUESTED: YELLOW,
    _s.STATUS_COMPLETED: GREEN,
    _s.STATUS_FAILED: RED,
    _s.STATUS_CANCEL_FAILED: RED,
    _s.STATUS_SUBMISSION_FAILED: RED,
    _s.STATUS_REPAIR_BLOCKED: RED,
    _s.STATUS_CANCELLED: MAGENTA,
    # ORCA run results can report this bare failure variant.
    "error": RED,
}


def status_color(status: object) -> str | None:
    """Return the ANSI SGR code for an activity status, or ``None``."""

    normalized = str(status).strip().lower() if status is not None else ""
    return _ACTIVITY_STATUS_COLORS.get(normalized)


def paint(text: str, *codes: str, stream: IO[str] | None = None) -> str:
    """Wrap ``text`` in the given SGR ``codes`` when color is enabled.

    When color is disabled the text is returned unchanged, so callers can wrap
    output unconditionally and rely on this for piping/`NO_COLOR` correctness.
    """

    if not codes or not text or not color_enabled(stream):
        return text
    return f"{sgr(*codes)}{text}{_RESET}"


def sgr(*codes: str) -> str:
    """Return the opening SGR escape sequence for ``codes`` (no reset)."""

    return "\033[" + ";".join(codes) + "m"


# Public alias so callers can append a reset after a raw :func:`sgr` opener.
RESET = _RESET


def label(text: str, *, stream: IO[str] | None = None) -> str:
    """Dim a field label (e.g. ``workflow_id:``) for key/value output."""

    return paint(text, DIM, stream=stream)


def status_text(status: object, *, stream: IO[str] | None = None) -> str:
    """Return the status string tinted by its activity color (no-op if none)."""

    text = "" if status is None else str(status)
    color = status_color(status)
    return paint(text, color, stream=stream) if color else text


def clear_screen(*, force: bool = False) -> None:
    """Clear and home a terminal independently of ANSI color painting.

    ``force`` is for callers that have already verified a real terminal. Cursor
    control is not color, so a no-color live view can still refresh in place.
    """

    if force or color_enabled():
        sys.stdout.write("\033[2J\033[3J\033[H")
        sys.stdout.flush()


__all__ = [
    "BLUE",
    "BOLD",
    "CYAN",
    "DIM",
    "GREEN",
    "MAGENTA",
    "RED",
    "RESET",
    "YELLOW",
    "clear_screen",
    "color_enabled",
    "label",
    "paint",
    "set_color_override",
    "sgr",
    "status_color",
    "status_text",
]
