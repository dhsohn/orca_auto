"""Discord implementation of the :class:`MessageChannel` port (webhook transport).

A webhook needs no bot, no gateway and no public endpoint — it is a single URL
that accepts ``POST`` requests with embeds, which is all Phase 1 (outbound
notifications) requires. Interactive control (buttons / slash commands) is a
Phase 2 concern and will need a separate gateway adapter.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from orca_auto.core.config import DiscordConfig
from orca_auto.core.config.schema import discord_webhook_url_is_valid

from .channel import SendResult
from .render_discord import render_discord_embed
from .richtext import Message

_LOGGER = logging.getLogger(__name__)

# Discord message flag: SUPPRESS_NOTIFICATIONS (deliver silently).
_SUPPRESS_NOTIFICATIONS = 1 << 12
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


def _webhook_url_with_wait(raw_url: str) -> str | None:
    """Validate a Discord webhook URL and force its synchronous response mode."""
    if not discord_webhook_url_is_valid(raw_url):
        return None
    parsed = urlsplit(raw_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "wait"
    ]
    query.append(("wait", "true"))
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(query), ""))


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
    # Discord's Retry-After response header is authoritative. The JSON value is
    # a fallback for responses/proxies which omit it, matching the REST adapter.
    delay = candidates[0]
    if delay > _MAX_RETRY_DELAY_SECONDS:
        return None, "discord_retry_after_exceeds_limit"
    return delay, ""


@dataclass(frozen=True)
class DiscordWebhookChannel:
    config: DiscordConfig
    logger: logging.Logger | None = None
    sleeper: Callable[[float], None] | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _payload(self, message: Message, *, silent: bool) -> dict[str, object]:
        payload: dict[str, object] = {
            "embeds": [render_discord_embed(message)],
            # Domain values can contain ``@everyone``, user IDs, or role IDs.
            # Notifications must never turn those values into Discord pings.
            "allowed_mentions": {"parse": []},
        }
        if silent:
            payload["flags"] = _SUPPRESS_NOTIFICATIONS
        return payload

    def _post_once(self, url: str, data: bytes) -> SendResult:
        request = Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "orca_auto-discord-webhook/1",
            },
            method="POST",
        )
        with urlopen(request, timeout=_bounded_timeout(self.config.timeout_seconds)) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                return SendResult(
                    sent=False, error="discord_unconfirmed_delivery", provider="discord"
                )
            message_id = _response_message_id(response)
            if not message_id:
                return SendResult(sent=False, error="discord_invalid_response", provider="discord")
            return SendResult(
                sent=True,
                provider="discord",
                message_id=message_id,
                message_ids=(message_id,),
                sent_count=1,
                total_count=1,
            )

    def _post_attempt(
        self,
        url: str,
        data: bytes,
    ) -> tuple[SendResult, bool, float | None]:
        try:
            return self._post_once(url, data), False, None
        except HTTPError as exc:
            status = getattr(exc, "code", None)
            retry_after: float | None = None
            if status == 429:
                retry_after, retry_error = _retry_after_from_error(exc)
                if retry_error:
                    return (
                        SendResult(sent=False, error=retry_error, provider="discord"),
                        False,
                        None,
                    )
            else:
                _close_http_error(exc)
            return (
                SendResult(
                    sent=False, error=f"discord_http_{status or 'unknown'}", provider="discord"
                ),
                _is_retryable_status(status),
                retry_after,
            )
        except (URLError, OSError):
            return (
                SendResult(sent=False, error="discord_network_error", provider="discord"),
                True,
                None,
            )

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        if not self.config.enabled:
            return SendResult(
                sent=False,
                skipped=True,
                error="discord_disabled",
                provider="discord",
            )

        url = _webhook_url_with_wait(self.config.webhook_url)
        if url is None:
            result = SendResult(
                sent=False,
                error="discord_invalid_webhook_url",
                provider="discord",
            )
            (self.logger or _LOGGER).warning("discord_send_failed: %s", result.error)
            return result

        data = json.dumps(self._payload(message, silent=silent)).encode("utf-8")
        attempts = _bounded_attempts(self.config.max_attempts)
        total_delay = 0.0
        result = SendResult(sent=False, error="discord_not_attempted", provider="discord")
        for attempt_index in range(1, attempts + 1):
            result, retryable, retry_after = self._post_attempt(url, data)
            if result.sent or attempt_index >= attempts or not retryable:
                break
            configured_backoff = _numeric_retry_after(self.config.retry_backoff_seconds)
            delay = retry_after if retry_after is not None else configured_backoff
            if delay is None:
                delay = _DEFAULT_RETRY_BACKOFF_SECONDS
            if not math.isfinite(delay) or delay > _MAX_TOTAL_RETRY_DELAY_SECONDS - total_delay:
                result = SendResult(
                    sent=False,
                    error="discord_retry_budget_exceeded",
                    provider="discord",
                )
                break
            total_delay += delay
            if delay > 0:
                (self.sleeper or time.sleep)(delay)
        if not result.sent:
            (self.logger or _LOGGER).warning("discord_send_failed: %s", result.error)
        return result


__all__ = ["DiscordWebhookChannel"]
