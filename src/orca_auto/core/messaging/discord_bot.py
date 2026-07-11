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
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orca_auto.core.config import DiscordConfig

from .channel import SendResult
from .discord_webhook import (
    _DEFAULT_RETRY_BACKOFF_SECONDS,
    _MAX_TOTAL_RETRY_DELAY_SECONDS,
    _SUPPRESS_NOTIFICATIONS,
    _bounded_attempts,
    _bounded_timeout,
    _close_http_error,
    _is_retryable_status,
    _numeric_retry_after,
    _response_message_id,
    _retry_after_from_error,
)
from .render_discord import render_discord_embed
from .richtext import Message

_LOGGER = logging.getLogger(__name__)
_DISCORD_API_BASE = "https://discord.com/api/v10"


@dataclass(frozen=True)
class DiscordBotChannel:
    """Deliver notifications through Discord's authenticated message API."""

    config: DiscordConfig
    logger: logging.Logger | None = None
    sleeper: Callable[[float], None] | None = None

    @property
    def enabled(self) -> bool:
        return self.config.bot_notification_enabled

    def _payload(self, message: Message, *, silent: bool) -> dict[str, object]:
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
        if silent:
            payload["flags"] = _SUPPRESS_NOTIFICATIONS
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
                return SendResult(
                    sent=False,
                    error="discord_unconfirmed_delivery",
                    provider="discord",
                )
            message_id = _response_message_id(response)
            if not message_id:
                return SendResult(
                    sent=False,
                    error="discord_invalid_response",
                    provider="discord",
                )
            return SendResult(
                sent=True,
                provider="discord",
                message_id=message_id,
                message_ids=(message_id,),
                sent_count=1,
                total_count=1,
            )

    def _post_attempt(self, data: bytes) -> tuple[SendResult, bool, float | None]:
        try:
            return self._post_once(data), False, None
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
                    sent=False,
                    error=f"discord_http_{status or 'unknown'}",
                    provider="discord",
                ),
                _is_retryable_status(status),
                retry_after,
            )
        except (URLError, OSError):
            return (
                SendResult(
                    sent=False,
                    error="discord_network_error",
                    provider="discord",
                ),
                True,
                None,
            )

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        if not self.enabled:
            return SendResult(
                sent=False,
                skipped=True,
                error="discord_bot_disabled",
                provider="discord",
            )

        data = json.dumps(
            self._payload(message, silent=silent),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        attempts = _bounded_attempts(self.config.max_attempts)
        total_delay = 0.0
        result = SendResult(sent=False, error="discord_not_attempted", provider="discord")
        for attempt_index in range(1, attempts + 1):
            result, retryable, retry_after = self._post_attempt(data)
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
            (self.logger or _LOGGER).warning("discord_bot_send_failed: %s", result.error)
        return result


__all__ = ["DiscordBotChannel"]
