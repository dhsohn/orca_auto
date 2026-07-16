"""Telegram implementation of the :class:`MessageChannel` port.

Reuses the existing Telegram transport (retries and IPv4 fallback), while the
Doc renderer owns HTML-safe chunk boundaries and an independent plain fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from orca_auto.core.config import TelegramConfig

from .channel import SendResult
from .render_telegram import render_telegram_chunks
from .richtext import Message
from .telegram_transport import build_telegram_transport, send_rendered_telegram_chunks


@dataclass(frozen=True)
class TelegramChannel:
    config: TelegramConfig
    logger: logging.Logger | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def send(self, message: Message, *, silent: bool = False) -> SendResult:
        if not self.config.enabled:
            return SendResult(
                sent=False,
                skipped=True,
                error="telegram_disabled",
                provider="telegram",
            )
        chunks = render_telegram_chunks(message)
        delivery = send_rendered_telegram_chunks(
            self.config,
            ((chunk.html, chunk.plain) for chunk in chunks),
            parse_mode="HTML",
            silent=silent,
            logger=self.logger,
            transport_factory=build_telegram_transport,
        )
        return SendResult(
            sent=delivery.sent,
            skipped=delivery.skipped,
            error=delivery.error,
            provider="telegram",
            message_id=delivery.message_ids[0] if len(delivery.message_ids) == 1 else None,
            message_ids=delivery.message_ids,
            sent_count=delivery.sent_count,
            total_count=delivery.total_count,
        )


__all__ = ["TelegramChannel"]
