"""Discord implementation of the :class:`MessageChannel` port (webhook transport).

A webhook needs no bot, no gateway and no public endpoint — it is a single URL
that accepts ``POST`` requests with embeds, which is all Phase 1 (outbound
notifications) requires. Interactive control (buttons / slash commands) is a
Phase 2 concern and will need a separate gateway adapter.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orca_auto.core.config import DiscordConfig

from .channel import SendResult
from .render_discord import render_discord_embed
from .richtext import Message

_LOGGER = logging.getLogger(__name__)

# Discord message flag: SUPPRESS_NOTIFICATIONS (deliver silently).
_SUPPRESS_NOTIFICATIONS = 1 << 12


def _is_retryable_status(status: int | None) -> bool:
    return status == 429 or (status is not None and 500 <= status < 600)


@dataclass(frozen=True)
class DiscordWebhookChannel:
    config: DiscordConfig
    logger: logging.Logger | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _payload(self, message: Message, *, silent: bool) -> dict[str, object]:
        payload: dict[str, object] = {"embeds": [render_discord_embed(message)]}
        if silent:
            payload["flags"] = _SUPPRESS_NOTIFICATIONS
        return payload

    def _post_once(self, data: bytes) -> SendResult:
        request = Request(
            self.config.webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=float(self.config.timeout_seconds)) as response:
            status = int(getattr(response, "status", response.getcode()))
            ok = 200 <= status < 300
            return SendResult(
                sent=ok,
                skipped=False,
                error="" if ok else f"discord_http_{status}",
            )

    def _post_attempt(self, data: bytes) -> tuple[SendResult, bool]:
        try:
            return self._post_once(data), False
        except HTTPError as exc:
            status = getattr(exc, "code", None)
            return (
                SendResult(sent=False, error=f"discord_http_error:{exc}"),
                _is_retryable_status(status),
            )
        except URLError as exc:
            return SendResult(sent=False, error=f"discord_url_error:{exc}"), True
        except OSError as exc:
            return SendResult(sent=False, error=f"discord_error:{exc}"), True

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        if not self.config.enabled:
            return SendResult(sent=False, skipped=True, error="discord_disabled")

        data = json.dumps(self._payload(message, silent=silent)).encode("utf-8")
        attempts = max(1, int(self.config.max_attempts))
        result = SendResult(sent=False, error="discord_not_attempted")
        for attempt_index in range(1, attempts + 1):
            result, retryable = self._post_attempt(data)
            if result.sent or attempt_index >= attempts or not retryable:
                break
            delay = max(0.0, float(self.config.retry_backoff_seconds))
            if delay > 0:
                time.sleep(delay)
        if not result.sent:
            (self.logger or _LOGGER).warning("discord_send_failed: %s", result.error)
        return result


__all__ = ["DiscordWebhookChannel"]
