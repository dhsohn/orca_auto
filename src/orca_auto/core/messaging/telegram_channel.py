"""Telegram implementation of the :class:`MessageChannel` port.

Reuses the existing Telegram transport (chunking, HTML parse-mode fallback,
retries, IPv4 fallback) unchanged; only the message *body* now comes from the
Doc-model renderer instead of hand-written HTML.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from orca_auto.core.config import TelegramConfig
from orca_auto.core.notifications import build_telegram_transport, send_telegram_message

from .channel import SendResult
from .render_telegram import render_telegram
from .richtext import Message


@dataclass(frozen=True)
class TelegramChannel:
    config: TelegramConfig
    logger: logging.Logger | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        if not self.config.enabled:
            return SendResult(sent=False, skipped=True, error="telegram_disabled")
        body = render_telegram(message)
        sent = send_telegram_message(
            self.config,
            body,
            parse_mode="HTML",
            skipped_ok=True,
            logger=self.logger,
            transport_factory=build_telegram_transport,
        )
        return SendResult(sent=bool(sent), skipped=False)


__all__ = ["TelegramChannel"]
