from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .telegram_config import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_BACKOFF_SECONDS,
    DEFAULT_TELEGRAM_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    TelegramConfigLike,
)
from .telegram_format import MAX_TELEGRAM_MESSAGE_LENGTH, escape_html, split_telegram_message
from .telegram_logging import safe_log_body
from .telegram_network import (
    _is_retryable_http_status,
    _is_timeout_error,
    _read_http_error_body,
    _should_retry_url_error,
)
from .telegram_network import (
    urlopen_with_ipv4_fallback as _network_urlopen_with_ipv4_fallback,
)

_MAX_RETRY_DELAY_SECONDS = 120.0
_MAX_TOTAL_RETRY_DELAY_SECONDS = 120.0
_MAX_ATTEMPTS = 10


def _bounded_timeout(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(parsed):
        return DEFAULT_TIMEOUT_SECONDS
    return min(_MAX_RETRY_DELAY_SECONDS, max(0.1, parsed))


def _bounded_attempts(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, bytearray, int, float)):
        return DEFAULT_MAX_ATTEMPTS
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_MAX_ATTEMPTS
    return min(_MAX_ATTEMPTS, max(1, parsed))


def _bounded_backoff(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_RETRY_BACKOFF_SECONDS
    if not math.isfinite(parsed):
        return DEFAULT_RETRY_BACKOFF_SECONDS
    return min(_MAX_RETRY_DELAY_SECONDS, max(0.0, parsed))


def _open_telegram_request(request: Request, *, timeout: float):
    return _network_urlopen_with_ipv4_fallback(
        request,
        timeout=timeout,
        urlopen_fn=urlopen,
    )


@dataclass(frozen=True)
class TelegramSendResult:
    sent: bool
    skipped: bool = False
    status_code: int | None = None
    response_text: str = ""
    error: str = ""
    message_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TelegramDeliveryResult:
    """Aggregate result for a possibly chunked Telegram delivery."""

    sent_count: int
    total_count: int
    skipped: bool = False
    error: str = ""
    message_ids: tuple[str, ...] = ()

    @property
    def sent(self) -> bool:
        return self.total_count > 0 and self.sent_count == self.total_count

    @property
    def partial(self) -> bool:
        return 0 < self.sent_count < self.total_count


def telegram_send_result_ok(result: Any, *, skipped_ok: bool = False) -> bool:
    return bool(
        getattr(result, "sent", False) or (skipped_ok and getattr(result, "skipped", False))
    )


def log_telegram_send_failure(logger: logging.Logger, result: Any) -> None:
    status_code = getattr(result, "status_code", None)
    error = getattr(result, "error", "")
    response_text = getattr(result, "response_text", "")
    if status_code is not None:
        logger.warning(
            "telegram_send_failed: status=%s error=%s body=%s",
            status_code,
            error,
            safe_log_body(response_text),
        )
    elif error:
        logger.warning("telegram_send_failed: %s", error)
    else:
        logger.warning("telegram_send_failed: unknown_error")


@dataclass(frozen=True)
class TelegramTransport:
    config: TelegramConfigLike
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS
    base_url: str = DEFAULT_TELEGRAM_BASE_URL

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _send_message_url(self, token: str) -> str:
        return f"{self.base_url.rstrip('/')}/bot{token}/sendMessage"

    def _send_text_payload(
        self,
        message: str,
        *,
        chat_id: str | None = None,
        disable_web_page_preview: bool = True,
        silent: bool = False,
        parse_mode: str | None = None,
    ) -> tuple[str, dict[str, Any]] | TelegramSendResult:
        resolved_chat_id = str(chat_id or self.config.chat_id).strip()
        token = str(self.config.bot_token).strip()
        if not token or not resolved_chat_id:
            return TelegramSendResult(sent=False, skipped=True, error="telegram_config_incomplete")

        payload: dict[str, Any] = {
            "chat_id": resolved_chat_id,
            "text": message,
            "disable_web_page_preview": "true" if disable_web_page_preview else "false",
            "disable_notification": "true" if silent else "false",
        }
        if parse_mode:
            payload["parse_mode"] = str(parse_mode).strip()

        return token, payload

    def _send_text_once(self, request: Request, payload: dict[str, Any]) -> TelegramSendResult:
        with _open_telegram_request(request, timeout=_bounded_timeout(self.timeout)) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", response.getcode()))
            if not 200 <= status_code < 300:
                return TelegramSendResult(
                    sent=False,
                    skipped=False,
                    status_code=status_code,
                    response_text=body,
                    error=f"telegram_http_{status_code}",
                    payload=payload,
                )

            message_id, response_error = _confirmed_telegram_message(body)
            return TelegramSendResult(
                sent=bool(message_id),
                skipped=False,
                status_code=status_code,
                response_text=body,
                error=response_error,
                message_id=message_id,
                payload=payload,
            )

    def _send_text_attempt(
        self, request: Request, payload: dict[str, Any]
    ) -> tuple[TelegramSendResult, bool]:
        try:
            result = self._send_text_once(request, payload)
            return result, False
        except HTTPError as exc:
            result = TelegramSendResult(
                sent=False,
                skipped=False,
                status_code=getattr(exc, "code", None),
                response_text=_read_http_error_body(exc),
                error=f"telegram_http_error:{exc}",
                payload=payload,
            )
            return result, _is_retryable_http_status(result.status_code)
        except URLError as exc:
            return TelegramSendResult(
                sent=False,
                skipped=False,
                error=f"telegram_url_error:{exc}",
                payload=payload,
            ), _should_retry_url_error(exc)
        except OSError as exc:
            return TelegramSendResult(
                sent=False,
                skipped=False,
                error=f"telegram_error:{exc}",
                payload=payload,
            ), _is_timeout_error(exc)

    def _retry_send_text(self, request: Request, payload: dict[str, Any]) -> TelegramSendResult:
        attempts = _bounded_attempts(self.max_attempts)
        total_delay = 0.0
        for attempt_index in range(1, attempts + 1):
            result, retryable = self._send_text_attempt(request, payload)
            if result.sent or attempt_index >= attempts or not retryable:
                return result
            delay = _bounded_backoff(self.retry_backoff_seconds)
            if result.status_code == 429:
                retry_after = _telegram_retry_after(result.response_text)
                if retry_after is not None:
                    delay = retry_after
            if (
                delay > _MAX_RETRY_DELAY_SECONDS
                or delay > _MAX_TOTAL_RETRY_DELAY_SECONDS - total_delay
            ):
                return replace(result, error="telegram_retry_after_exceeds_limit")
            total_delay += delay
            _sleep_before_retry(delay)
        return TelegramSendResult(
            sent=False, skipped=False, error="telegram_retry_exhausted", payload=payload
        )

    def send_text(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        disable_web_page_preview: bool = True,
        silent: bool = False,
        parse_mode: str | None = None,
    ) -> TelegramSendResult:
        message = str(text).strip()
        if not self.enabled:
            return TelegramSendResult(sent=False, skipped=True, error="telegram_disabled")
        if not message:
            return TelegramSendResult(sent=False, skipped=True, error="empty_message")

        payload_result = self._send_text_payload(
            message,
            chat_id=chat_id,
            disable_web_page_preview=disable_web_page_preview,
            silent=silent,
            parse_mode=parse_mode,
        )
        if isinstance(payload_result, TelegramSendResult):
            return payload_result

        token, payload = payload_result
        request = Request(
            self._send_message_url(token),
            data=urlencode(payload).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._retry_send_text(request, payload)


TelegramTransportFactory = Callable[[TelegramConfigLike], Any]


@dataclass(frozen=True)
class _TelegramChunkSendRequest:
    primary_text: str
    primary_parse_mode: str | None
    fallback_text: str


@dataclass(frozen=True)
class _TelegramChunkSendOutcome:
    ok: bool
    result: Any


def _confirmed_telegram_message(response_text: str) -> tuple[str, str]:
    try:
        decoded = json.loads(response_text)
    except (TypeError, ValueError):
        return "", "telegram_invalid_response"
    if not isinstance(decoded, dict):
        return "", "telegram_invalid_response"
    if decoded.get("ok") is not True:
        return "", "telegram_api_error"
    result = decoded.get("result")
    if not isinstance(result, dict):
        return "", "telegram_invalid_response"
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return "", "telegram_invalid_response"
    return str(message_id), ""


def _telegram_retry_after(response_text: str) -> float | None:
    try:
        decoded = json.loads(response_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    parameters = decoded.get("parameters")
    if not isinstance(parameters, dict):
        return None
    value = parameters.get("retry_after")
    if isinstance(value, bool):
        return None
    try:
        delay = float(str(value))
    except (TypeError, ValueError):
        return None
    if delay < 0 or delay != delay or delay == float("inf"):
        return None
    return delay


def _telegram_parse_error(result: Any) -> bool:
    """Return true only for Telegram's deterministic HTML entity failures.

    Retrying a timeout, 429, or 5xx without parse mode can duplicate a message
    whose first response was merely lost.  Plain-text fallback is therefore
    restricted to an explicit 400 entity/parser rejection.
    """

    if getattr(result, "status_code", None) != 400:
        return False
    body = str(getattr(result, "response_text", "") or "")
    try:
        decoded = json.loads(body)
    except (TypeError, ValueError):
        description = body
    else:
        description = str(decoded.get("description", "")) if isinstance(decoded, dict) else body
    normalized = description.casefold()
    return any(
        marker in normalized
        for marker in (
            "can't parse entities",
            "cannot parse entities",
            "can't find end tag",
            "unsupported start tag",
        )
    )


def _telegram_transport_or_none(
    config: TelegramConfigLike,
    *,
    logger: logging.Logger | None,
    transport_factory: TelegramTransportFactory | None,
) -> Any | None:
    if config.enabled:
        return (transport_factory or build_telegram_transport)(config)
    if logger is not None:
        logger.debug("telegram_notifier_disabled")
    return None


def _send_telegram_chunk_with_fallback(
    chunk: _TelegramChunkSendRequest,
    *,
    transport: Any,
    skipped_ok: bool,
    silent: bool = False,
) -> _TelegramChunkSendOutcome:
    result = transport.send_text(
        chunk.primary_text,
        parse_mode=chunk.primary_parse_mode,
        silent=silent,
    )
    if telegram_send_result_ok(result, skipped_ok=skipped_ok):
        return _TelegramChunkSendOutcome(ok=True, result=result)

    if not chunk.primary_parse_mode or not _telegram_parse_error(result):
        return _TelegramChunkSendOutcome(ok=False, result=result)

    fallback_result = transport.send_text(
        chunk.fallback_text,
        parse_mode=None,
        silent=silent,
    )
    if telegram_send_result_ok(fallback_result, skipped_ok=skipped_ok):
        return _TelegramChunkSendOutcome(ok=True, result=fallback_result)
    return _TelegramChunkSendOutcome(ok=False, result=fallback_result)


def _send_telegram_chunks(
    chunks: Iterable[_TelegramChunkSendRequest],
    *,
    transport: Any,
    skipped_ok: bool = False,
    logger: logging.Logger | None = None,
    silent: bool = False,
) -> TelegramDeliveryResult:
    requests = tuple(chunks)
    if not requests:
        return TelegramDeliveryResult(sent_count=0, total_count=0, error="empty_message")

    sent_count = 0
    message_ids: list[str] = []
    for chunk in requests:
        outcome = _send_telegram_chunk_with_fallback(
            chunk,
            transport=transport,
            skipped_ok=skipped_ok,
            silent=silent,
        )
        if outcome.ok:
            sent_count += 1
            message_id = str(getattr(outcome.result, "message_id", "") or "").strip()
            if message_id:
                message_ids.append(message_id)
            continue
        if logger is not None:
            log_telegram_send_failure(logger, outcome.result)
        return TelegramDeliveryResult(
            sent_count=sent_count,
            total_count=len(requests),
            skipped=bool(getattr(outcome.result, "skipped", False)),
            error=str(getattr(outcome.result, "error", "") or "telegram_send_failed"),
            message_ids=tuple(message_ids),
        )
    return TelegramDeliveryResult(
        sent_count=sent_count,
        total_count=len(requests),
        message_ids=tuple(message_ids),
    )


def send_rendered_telegram_chunks(
    config: TelegramConfigLike,
    chunks: Iterable[tuple[str, str]],
    *,
    parse_mode: str | None = "HTML",
    silent: bool = False,
    skipped_ok: bool = False,
    logger: logging.Logger | None = None,
    transport_factory: TelegramTransportFactory | None = None,
) -> TelegramDeliveryResult:
    """Send pre-rendered atomic chunks with independent plain fallbacks."""

    transport = _telegram_transport_or_none(
        config,
        logger=logger,
        transport_factory=transport_factory,
    )
    if transport is None:
        return TelegramDeliveryResult(
            sent_count=0,
            total_count=0,
            skipped=True,
            error="telegram_disabled",
        )

    return _send_telegram_chunks(
        (
            _TelegramChunkSendRequest(
                primary_text=primary,
                primary_parse_mode=parse_mode,
                fallback_text=fallback,
            )
            for primary, fallback in chunks
        ),
        transport=transport,
        skipped_ok=skipped_ok,
        logger=logger,
        silent=silent,
    )


def send_telegram_message(
    config: TelegramConfigLike,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    limit: int = MAX_TELEGRAM_MESSAGE_LENGTH,
    skipped_ok: bool = False,
    logger: logging.Logger | None = None,
    transport_factory: TelegramTransportFactory | None = None,
    silent: bool = False,
) -> bool:
    """Send a chunked Telegram message with parse-mode fallback."""
    transport = _telegram_transport_or_none(
        config,
        logger=logger,
        transport_factory=transport_factory,
    )
    if transport is None:
        return False

    chunks = split_telegram_message(text, limit=limit)
    if not chunks:
        return False

    result = _send_telegram_chunks(
        (
            _TelegramChunkSendRequest(
                primary_text=chunk,
                primary_parse_mode=parse_mode,
                fallback_text=chunk,
            )
            for chunk in chunks
        ),
        transport=transport,
        skipped_ok=skipped_ok,
        logger=logger,
        silent=silent,
    )
    return result.sent


def send_preformatted_telegram_message(
    config: TelegramConfigLike,
    text: str,
    *,
    limit: int = MAX_TELEGRAM_MESSAGE_LENGTH,
    logger: logging.Logger | None = None,
    transport_factory: TelegramTransportFactory | None = None,
) -> bool:
    """Send text as HTML ``<pre>`` chunks, falling back to plain text per chunk."""
    wrapper_prefix = "<pre>"
    wrapper_suffix = "</pre>"
    wrapper_overhead = len(wrapper_prefix) + len(wrapper_suffix)
    if limit <= wrapper_overhead:
        raise ValueError("preformatted message limit must exceed wrapper size")
    transport = _telegram_transport_or_none(
        config,
        logger=logger,
        transport_factory=transport_factory,
    )
    if transport is None:
        return False

    chunks = split_telegram_message(text, limit=limit - wrapper_overhead)
    result = _send_telegram_chunks(
        (
            _TelegramChunkSendRequest(
                primary_text=f"{wrapper_prefix}{escape_html(chunk)}{wrapper_suffix}",
                primary_parse_mode="HTML",
                fallback_text=chunk,
            )
            for chunk in chunks
        ),
        transport=transport,
        logger=logger,
    )
    return result.sent


def _sleep_before_retry(backoff_seconds: float) -> None:
    delay = max(0.0, float(backoff_seconds))
    if delay > 0:
        time.sleep(delay)


def build_telegram_transport(
    config: TelegramConfigLike,
    *,
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_backoff_seconds: float | None = None,
    base_url: str = DEFAULT_TELEGRAM_BASE_URL,
) -> TelegramTransport:
    resolved_timeout = _bounded_timeout(
        timeout
        if timeout is not None
        else getattr(config, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    )
    resolved_attempts = _bounded_attempts(
        max_attempts
        if max_attempts is not None
        else getattr(config, "max_attempts", DEFAULT_MAX_ATTEMPTS)
    )
    resolved_backoff = _bounded_backoff(
        retry_backoff_seconds
        if retry_backoff_seconds is not None
        else getattr(config, "retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS)
    )
    return TelegramTransport(
        config=config,
        timeout=resolved_timeout,
        max_attempts=resolved_attempts,
        retry_backoff_seconds=resolved_backoff,
        base_url=base_url,
    )


__all__ = [
    "TelegramSendResult",
    "TelegramDeliveryResult",
    "TelegramTransport",
    "TelegramTransportFactory",
    "_TelegramChunkSendRequest",
    "_send_telegram_chunks",
    "_sleep_before_retry",
    "_telegram_transport_or_none",
    "build_telegram_transport",
    "log_telegram_send_failure",
    "send_preformatted_telegram_message",
    "send_rendered_telegram_chunks",
    "send_telegram_message",
    "telegram_send_result_ok",
]
