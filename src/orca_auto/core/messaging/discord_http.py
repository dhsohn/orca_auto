"""Shared HTTP retry/backoff helpers for Discord adapters.

Both the bot REST sender and any future Discord transport POST embeds to the
same API and share Discord's rate-limit contract (429 + ``Retry-After``) and
snowflake response shape. These stateless helpers live here so the transport
modules stay focused on payload construction and the send loop.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from urllib.error import HTTPError

_MAX_RETRY_DELAY_SECONDS = 120.0
_MAX_TOTAL_RETRY_DELAY_SECONDS = 120.0
_MAX_ERROR_BODY_BYTES = 16_384
_MAX_ATTEMPTS = 10
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.5


def _is_retryable_status(status: int | None) -> bool:
    return status == 429 or (status is not None and 500 <= status < 600)


def _bounded_timeout(value: object) -> float:
    parsed = _numeric_retry_after(value)
    if parsed is None:
        return 5.0
    return min(_MAX_RETRY_DELAY_SECONDS, max(0.1, parsed))


def _bounded_attempts(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, bytearray, int, float)):
        return 2
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 2
    return min(_MAX_ATTEMPTS, max(1, parsed))


def _response_message_id(response: object) -> str:
    try:
        body = response.read()  # type: ignore[attr-defined]
        decoded = json.loads(body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(decoded, Mapping):
        return ""
    message_id = decoded.get("id")
    if (
        not isinstance(message_id, str)
        or not 1 <= len(message_id) <= 20
        or not message_id.isascii()
        or not message_id.isdigit()
        or not message_id.strip("0")
    ):
        return ""
    return message_id


def _numeric_retry_after(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _close_http_error(exc: HTTPError) -> None:
    try:
        exc.close()
    except OSError:
        pass


def _retry_after_from_error(exc: HTTPError) -> tuple[float | None, str]:
    """Return a bounded Discord retry delay without surfacing response secrets."""
    candidates: list[float] = []
    headers = getattr(exc, "headers", None)
    header_value = headers.get("Retry-After") if headers is not None else None
    header_delay = _numeric_retry_after(header_value)
    if header_delay is not None:
        candidates.append(header_delay)

    try:
        try:
            body = exc.read(_MAX_ERROR_BODY_BYTES)
            decoded = json.loads(body.decode("utf-8")) if body else None
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            decoded = None
    finally:
        # HTTPError owns the response stream. Closing it here prevents a socket
        # leak on repeated rate-limit responses.
        _close_http_error(exc)
    if isinstance(decoded, Mapping):
        body_delay = _numeric_retry_after(decoded.get("retry_after"))
        if body_delay is not None:
            candidates.append(body_delay)

    if not candidates:
        return None, ""
    # Discord's Retry-After response header is authoritative. The JSON body value
    # is a fallback for responses/proxies which omit the header.
    delay = candidates[0]
    if delay > _MAX_RETRY_DELAY_SECONDS:
        return None, "discord_retry_after_exceeds_limit"
    return delay, ""
