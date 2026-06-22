from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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
    payload: dict[str, Any] = field(default_factory=dict)


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
        with _open_telegram_request(request, timeout=float(self.timeout)) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", response.getcode()))
            return TelegramSendResult(
                sent=200 <= status_code < 300,
                skipped=False,
                status_code=status_code,
                response_text=body,
                error="" if 200 <= status_code < 300 else f"telegram_http_{status_code}",
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
        attempts = max(1, int(self.max_attempts))
        for attempt_index in range(1, attempts + 1):
            result, retryable = self._send_text_attempt(request, payload)
            if result.sent or attempt_index >= attempts or not retryable:
                return result
            _sleep_before_retry(self.retry_backoff_seconds)
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
) -> _TelegramChunkSendOutcome:
    result = transport.send_text(
        chunk.primary_text,
        parse_mode=chunk.primary_parse_mode,
    )
    if telegram_send_result_ok(result, skipped_ok=skipped_ok):
        return _TelegramChunkSendOutcome(ok=True, result=result)

    if not chunk.primary_parse_mode:
        return _TelegramChunkSendOutcome(ok=False, result=result)

    fallback_result = transport.send_text(chunk.fallback_text, parse_mode=None)
    if telegram_send_result_ok(fallback_result, skipped_ok=skipped_ok):
        return _TelegramChunkSendOutcome(ok=True, result=fallback_result)
    return _TelegramChunkSendOutcome(ok=False, result=fallback_result)


def _send_telegram_chunks(
    chunks: Iterable[_TelegramChunkSendRequest],
    *,
    transport: Any,
    skipped_ok: bool = False,
    logger: logging.Logger | None = None,
) -> bool:
    sent_any = False
    for chunk in chunks:
        outcome = _send_telegram_chunk_with_fallback(
            chunk,
            transport=transport,
            skipped_ok=skipped_ok,
        )
        if outcome.ok:
            sent_any = True
            continue
        if logger is not None:
            log_telegram_send_failure(logger, outcome.result)
        return False
    return sent_any


def send_telegram_message(
    config: TelegramConfigLike,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    limit: int = MAX_TELEGRAM_MESSAGE_LENGTH,
    skipped_ok: bool = False,
    logger: logging.Logger | None = None,
    transport_factory: TelegramTransportFactory | None = None,
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

    return _send_telegram_chunks(
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
    )


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
    return _send_telegram_chunks(
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
    resolved_timeout = float(
        timeout
        if timeout is not None
        else getattr(config, "timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    )
    resolved_attempts = max(
        1,
        int(
            max_attempts
            if max_attempts is not None
            else getattr(config, "max_attempts", DEFAULT_MAX_ATTEMPTS)
        ),
    )
    resolved_backoff = max(
        0.0,
        float(
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else getattr(config, "retry_backoff_seconds", DEFAULT_RETRY_BACKOFF_SECONDS)
        ),
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
    "TelegramTransport",
    "TelegramTransportFactory",
    "_TelegramChunkSendRequest",
    "_send_telegram_chunks",
    "_sleep_before_retry",
    "_telegram_transport_or_none",
    "build_telegram_transport",
    "log_telegram_send_failure",
    "send_preformatted_telegram_message",
    "send_telegram_message",
    "telegram_send_result_ok",
]
