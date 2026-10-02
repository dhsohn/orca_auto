"""Slack Web API notification channel (``chat.postMessage`` with a bot token).

The request goes to one fixed endpoint and the configured channel; nothing in a
message chooses the destination. A send counts as delivered only when the JSON
response has ``"ok": true``, ``channel`` equal to the configured channel ID and
a string ``ts`` of the form ``<digits>.<six digits>``. That is ORCA's own
fail-closed check against Slack's documented response shape, not a promise
about every future Slack timestamp.

Only HTTP 429 is retried, and only after waiting the number of seconds in a
well-formed ``Retry-After`` header. Network errors, timeouts, unreadable
responses, other HTTP errors and ``"ok": false`` are never retried, because a
resend could post the message twice. Attempts are capped at
``MAX_MESSENGER_ATTEMPTS`` and the waits of one send at
``MAX_MESSENGER_RETRY_BACKOFF_SECONDS`` in total; these are ORCA's local
bounds. Failures are reported and logged as stable codes only, never the
token, the payload, the response body or an exception text.

Redirects are never followed, to any host or scheme: a redirect is reported
as ``slack_http_<status>`` and nothing is sent to its ``Location``, so the
Bearer token cannot reach another target. This is ORCA's policy, not a Slack
guarantee. Each request uses its own opener; the process-wide urllib opener is
not changed.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from orca_auto.core.config import SlackConfig
from orca_auto.core.config.schema import (
    MAX_MESSENGER_ATTEMPTS,
    MAX_MESSENGER_RETRY_BACKOFF_SECONDS,
)

from .channel import SendResult
from .render_slack import render_slack_text
from .richtext import Message

_LOGGER = logging.getLogger(__name__)
_SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_MAX_RESPONSE_BYTES = 65_536
_SLACK_TS_PATTERN = re.compile(r"^[0-9]+\.[0-9]{6}$")
_RETRY_AFTER_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?$")


class _RefuseRedirects(HTTPRedirectHandler):
    """Never build a follow-up request; urllib then raises the redirect as HTTPError."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl


def _close_http_error(exc: HTTPError) -> None:
    try:
        exc.close()
    except (HTTPException, OSError):
        pass


def _attempt_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return min(MAX_MESSENGER_ATTEMPTS, max(1, value))


def _retry_after_seconds(exc: HTTPError) -> float | None:
    """The wait a 429 asks for, or ``None`` when it is missing or unusable."""

    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _RETRY_AFTER_PATTERN.fullmatch(text):
        return None
    seconds = float(text)
    if not math.isfinite(seconds) or seconds > MAX_MESSENGER_RETRY_BACKOFF_SECONDS:
        return None
    return seconds


@dataclass(frozen=True)
class SlackBotChannel:
    """Deliver notifications through Slack's ``chat.postMessage`` method."""

    config: SlackConfig
    logger: logging.Logger | None = None

    @property
    def enabled(self) -> bool:
        return self.config.slack_notification_enabled

    def _payload(self, message: Message) -> dict[str, object]:
        return {
            "channel": self.config.default_channel_id,
            "text": render_slack_text(message),
            # Plain text: no Markdown, no name/channel linking, no previews.
            "mrkdwn": False,
            "parse": "none",
            "unfurl_links": False,
            "unfurl_media": False,
        }

    def _post_once(self, data: bytes) -> SendResult:
        request = Request(
            _SLACK_POST_MESSAGE_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {self.config.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "orca_auto-slack-bot/1",
            },
            method="POST",
        )
        # A request-local opener: the standard handlers (proxies, HTTPS with its
        # default TLS verification, error processing) with redirects refused.
        opener = build_opener(_RefuseRedirects())
        with opener.open(request, timeout=self.config.timeout_seconds) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                return SendResult(sent=False, error="slack_unconfirmed_delivery")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
            return SendResult(sent=False, error="slack_invalid_response")
        try:
            parsed = json.loads(body)
        except ValueError:
            return SendResult(sent=False, error="slack_invalid_response")
        if not isinstance(parsed, dict) or parsed.get("ok") is not True:
            return SendResult(sent=False, error="slack_api_error")
        timestamp = parsed.get("ts")
        if (
            parsed.get("channel") != self.config.default_channel_id
            or not isinstance(timestamp, str)
            or not _SLACK_TS_PATTERN.fullmatch(timestamp)
        ):
            return SendResult(sent=False, error="slack_invalid_response")
        return SendResult(sent=True)

    def _post(self, data: bytes) -> tuple[SendResult, float | None]:
        """One attempt, and the wait before a retry (only for a usable 429)."""

        try:
            return self._post_once(data), None
        except HTTPError as exc:
            status = getattr(exc, "code", None)
            retry_after = _retry_after_seconds(exc) if status == 429 else None
            _close_http_error(exc)
            return SendResult(sent=False, error=f"slack_http_{status or 'unknown'}"), retry_after
        except (HTTPException, URLError, OSError):
            return SendResult(sent=False, error="slack_network_error"), None
        except ValueError:
            # urllib can put malformed header bytes, including the token, in
            # these exceptions; keep only a stable code.
            return SendResult(sent=False, error="slack_request_error"), None

    def _deliver(self, data: bytes) -> SendResult:
        attempts = _attempt_limit(self.config.max_attempts)
        waited = 0.0
        result = SendResult(sent=False, error="slack_not_attempted")
        for attempt in range(1, attempts + 1):
            result, retry_after = self._post(data)
            if result.sent or retry_after is None or attempt >= attempts:
                break
            if retry_after > MAX_MESSENGER_RETRY_BACKOFF_SECONDS - waited:
                break
            waited += retry_after
            if retry_after > 0:
                time.sleep(retry_after)
        return result

    def send(self, message: Message) -> SendResult:
        if not self.enabled:
            return SendResult(sent=False, skipped=True, error="slack_bot_disabled")
        try:
            data = json.dumps(
                self._payload(message),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            result = SendResult(sent=False, error="slack_request_error")
        else:
            result = self._deliver(data)
        if not result.sent:
            (self.logger or _LOGGER).warning("slack_bot_send_failed: %s", result.error)
        return result


__all__ = ["SlackBotChannel"]
