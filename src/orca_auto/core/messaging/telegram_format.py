from __future__ import annotations

from typing import Any

from orca_auto.core.utils.coercion import normalize_text as _normalize_text

MAX_TELEGRAM_MESSAGE_LENGTH = 4096


def escape_html_text(text: str) -> str:
    """Escape the Telegram-HTML metacharacters without normalizing whitespace."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_html(value: Any) -> str:
    return escape_html_text(_normalize_text(value))


def html_code(value: Any) -> str:
    return f"<code>{escape_html(value)}</code>"


def _split_long_segment(text: str, *, limit: int) -> list[str]:
    pieces: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            pieces.append(remaining)
            break
        split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        else:
            split_at += 1
        pieces.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return pieces


def _append_telegram_line(chunks: list[str], current: str, line: str, *, limit: int) -> str:
    if current and len(current) + len(line) > limit:
        chunk = current.strip()
        if chunk:
            chunks.append(chunk)
        return line
    return current + line


def _flush_telegram_chunk(chunks: list[str], current: str) -> str:
    chunk = current.strip()
    if chunk:
        chunks.append(chunk)
    return ""


def split_telegram_message(
    text: str,
    *,
    limit: int = MAX_TELEGRAM_MESSAGE_LENGTH,
) -> list[str]:
    """Split a Telegram message without cutting across normal line boundaries."""
    if limit <= 0:
        raise ValueError("limit must be positive")

    message = str(text).strip()
    if not message:
        return []
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current = ""

    for line in message.splitlines(keepends=True):
        if len(line) > limit:
            current = _flush_telegram_chunk(chunks, current)
            for piece in _split_long_segment(line, limit=limit):
                _flush_telegram_chunk(chunks, piece)
            continue
        current = _append_telegram_line(chunks, current, line, limit=limit)

    _flush_telegram_chunk(chunks, current)
    return chunks


__all__ = [
    "MAX_TELEGRAM_MESSAGE_LENGTH",
    "_append_telegram_line",
    "_flush_telegram_chunk",
    "_split_long_segment",
    "escape_html",
    "escape_html_text",
    "html_code",
    "split_telegram_message",
]
