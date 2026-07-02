"""Telegram API transport helpers for the orca_auto_flow bot."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from orca_auto.core.config import TelegramConfig
from orca_auto.core.notifications import (
    MAX_TELEGRAM_MESSAGE_LENGTH,
    TelegramApiClient,
    build_telegram_transport,
    send_preformatted_telegram_message,
    send_telegram_message,
)

API_BASE = "https://api.telegram.org/bot{token}"
POLL_TIMEOUT_SECONDS = 30
MAX_MESSAGE_LENGTH = MAX_TELEGRAM_MESSAGE_LENGTH
_LOGGER = logging.getLogger(__name__)


def api_call(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = POLL_TIMEOUT_SECONDS + 5,
    api_base: str = API_BASE,
    logger: logging.Logger | None = None,
) -> Any | None:
    client = TelegramApiClient(
        token=token,
        timeout=timeout,
        base_url=api_base.removesuffix("/bot{token}"),
        logger=logger or _LOGGER,
    )
    return client.api_call(method, payload, timeout=timeout)


def send_response(
    config: TelegramConfig,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    limit: int = MAX_MESSAGE_LENGTH,
    logger: logging.Logger | None = None,
    transport_factory: Callable[..., Any] = build_telegram_transport,
) -> bool:
    return send_telegram_message(
        config,
        text,
        parse_mode=parse_mode,
        limit=limit,
        logger=logger,
        transport_factory=transport_factory,
    )


def send_preformatted_response(
    config: TelegramConfig,
    text: str,
    *,
    limit: int = MAX_MESSAGE_LENGTH,
    logger: logging.Logger | None = None,
    transport_factory: Callable[..., Any] = build_telegram_transport,
) -> bool:
    return send_preformatted_telegram_message(
        config,
        text,
        limit=limit,
        logger=logger,
        transport_factory=transport_factory,
    )


__all__ = [
    "API_BASE",
    "MAX_MESSAGE_LENGTH",
    "POLL_TIMEOUT_SECONDS",
    "api_call",
    "send_preformatted_response",
    "send_response",
]
