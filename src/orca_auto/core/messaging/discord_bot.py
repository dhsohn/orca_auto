"""Discord Bot REST notification channel.

This adapter sends semantic notifications with a bot token to the configured
default channel.  A separate gateway adapter can later receive commands and
component interactions while scheduled/worker processes keep using this
short-lived REST sender.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orca_auto.core.config import DiscordConfig

from .channel import SendResult
from .render_discord import render_discord_embed
from .richtext import Message

_LOGGER = logging.getLogger(__name__)
_DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord's rate-limit contract (429 + ``Retry-After``) and snowflake response
# shape; stateless helpers for the send loop below.
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
    except (AttributeError, TypeError, ValueError):
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
    except (HTTPException, OSError):
        pass


def _retry_after_from_error(exc: HTTPError) -> tuple[float | None, str]:
    """Return a bounded Discord retry delay without surfacing response secrets."""
    candidates: list[float] = []
    headers = getattr(exc, "headers", None)
    header_value = headers.get("Retry-After") if headers is not None else None
    header_delay = _numeric_retry_after(header_value)
    if header_delay is not None:
        # Discord documents the response header as authoritative. Avoid reading
        # an error stream we do not need: a truncated 429 body must not turn a
        # perfectly usable header into an exception at the notification boundary.
        _close_http_error(exc)
        if header_delay > _MAX_RETRY_DELAY_SECONDS:
            return None, "discord_retry_after_exceeds_limit"
        return header_delay, ""

    try:
        try:
            body = exc.read(_MAX_ERROR_BODY_BYTES)
            decoded = json.loads(body.decode("utf-8")) if body else None
        except (AttributeError, HTTPException, OSError, TypeError, ValueError):
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
    # The JSON body is a fallback for responses/proxies which omit the header.
    delay = candidates[0]
    if delay > _MAX_RETRY_DELAY_SECONDS:
        return None, "discord_retry_after_exceeds_limit"
    return delay, ""


@dataclass(frozen=True)
class DiscordBotChannel:
    """Deliver notifications through Discord's authenticated message API."""

    config: DiscordConfig
    logger: logging.Logger | None = None
    sleeper: Callable[[float], None] | None = None

    @property
    def enabled(self) -> bool:
        return self.config.bot_notification_enabled

    def _payload(self, message: Message) -> dict[str, object]:
        payload: dict[str, object] = {
            "embeds": [render_discord_embed(message)],
            "allowed_mentions": {"parse": []},
            # Discord de-duplicates retries by bot author + nonce for a few
            # minutes when enforce_nonce is set. Reuse this serialized payload
            # across every attempt so an ambiguous network failure cannot
            # create duplicate notifications.
            "nonce": secrets.token_hex(12),
            "enforce_nonce": True,
        }
        return payload

    def _post_once(self, data: bytes) -> SendResult:
        url = f"{_DISCORD_API_BASE}/channels/{self.config.default_channel_id}/messages"
        request = Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bot {self.config.bot_token}",
                "Content-Type": "application/json",
                "User-Agent": "orca_auto-discord-bot/1",
            },
            method="POST",
        )
        with urlopen(request, timeout=_bounded_timeout(self.config.timeout_seconds)) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                return SendResult(sent=False, error="discord_unconfirmed_delivery")
            # The id is not retained, but a response without one is not a
            # confirmed delivery.
            if not _response_message_id(response):
                return SendResult(sent=False, error="discord_invalid_response")
            return SendResult(sent=True)

    def _post_attempt(self, data: bytes) -> tuple[SendResult, bool, float | None]:
        try:
            return self._post_once(data), False, None
        except HTTPError as exc:
            status = getattr(exc, "code", None)
            retry_after: float | None = None
            if status == 429:
                retry_after, retry_error = _retry_after_from_error(exc)
                if retry_error:
                    return SendResult(sent=False, error=retry_error), False, None
            else:
                _close_http_error(exc)
            return (
                SendResult(sent=False, error=f"discord_http_{status or 'unknown'}"),
                _is_retryable_status(status),
                retry_after,
            )
        except (HTTPException, URLError, OSError):
            return SendResult(sent=False, error="discord_network_error"), True, None
        except ValueError:
            # urllib includes malformed header bytes in these exceptions.  The
            # Authorization header contains the bot token, so expose only a
            # stable redacted transport code and do not retry the bad request.
            return SendResult(sent=False, error="discord_request_error"), False, None

    def send(self, message: Message) -> SendResult:
        if not self.enabled:
            return SendResult(sent=False, skipped=True, error="discord_bot_disabled")

        try:
            data = json.dumps(
                self._payload(message),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            # Request construction is advisory like transport: a payload that
            # cannot be serialized (a lone surrogate from a non-UTF-8 path,
            # for instance) is a failed send, not an exception for the caller.
            # The message may carry path text, so log only the stable code.
            result = SendResult(sent=False, error="discord_request_error")
            (self.logger or _LOGGER).warning("discord_bot_send_failed: %s", result.error)
            return result
        attempts = _bounded_attempts(self.config.max_attempts)
        total_delay = 0.0
        result = SendResult(sent=False, error="discord_not_attempted")
        for attempt_index in range(1, attempts + 1):
            result, retryable, retry_after = self._post_attempt(data)
            if result.sent or attempt_index >= attempts or not retryable:
                break
            configured_backoff = _numeric_retry_after(self.config.retry_backoff_seconds)
            delay = retry_after if retry_after is not None else configured_backoff
            if delay is None:
                delay = _DEFAULT_RETRY_BACKOFF_SECONDS
            if not math.isfinite(delay) or delay > _MAX_TOTAL_RETRY_DELAY_SECONDS - total_delay:
                result = SendResult(sent=False, error="discord_retry_budget_exceeded")
                break
            total_delay += delay
            if delay > 0:
                (self.sleeper or time.sleep)(delay)
        if not result.sent:
            (self.logger or _LOGGER).warning("discord_bot_send_failed: %s", result.error)
        return result


__all__ = ["DiscordBotChannel"]
