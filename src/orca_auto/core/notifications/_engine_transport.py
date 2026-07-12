from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orca_auto.core.messaging import (
    Message,
    Severity,
    build_channel,
    group,
    line,
    raw,
)

from ._engine_rendering import severity_for_event_line


def _lines_message(lines: list[str], severity: Severity = "info") -> Message:
    """Wrap pre-formatted plain-text lines as a Doc-model message.

    Engine job notifications are plain text (no markup), so each line becomes a
    ``raw`` span: the Telegram renderer reproduces the original text and the
    Discord renderer collects the lines into the embed description. ``severity``
    only reaches the Discord embed colour/glyph — the Telegram renderer ignores
    it — so a failed job no longer renders in the neutral ``info`` blue.
    """
    title = lines[0] if lines else ""
    return Message(
        title=title,
        severity=severity,
        groups=(
            group(
                *(line(raw(text)) for text in lines[1:]),
                heading=(raw(title),) if title else (),
            ),
        ),
        author="orca_auto",
    )


def send_lines(cfg: Any, lines: list[str]) -> bool:
    if not lines:
        return False
    # ``build_channel`` is referenced as a module global (not a default arg) so
    # tests can monkeypatch it.
    channel = build_channel(cfg.messenger)
    result = channel.send(_lines_message(lines, severity_for_event_line(lines[0])))
    return bool(result.sent or result.skipped)


def channel_line_sender() -> Callable[[Any, list[str]], bool]:
    def send(cfg: Any, lines: list[str]) -> bool:
        return send_lines(cfg, lines)

    return send
