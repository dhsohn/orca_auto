"""Resolve the active :class:`MessageChannel` from configuration.

Adding a messenger is a one-line entry here plus an adapter module — the same
"name → factory" seam the engines use, kept as a plain dict because the adapters
live in this package (no cross-layer lazy import needed).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from orca_auto.core.config import MessengerConfig, TelegramConfig

from .channel import MessageChannel
from .discord_webhook import DiscordWebhookChannel
from .telegram_channel import TelegramChannel

ChannelBuilder = Callable[[MessengerConfig, TelegramConfig, logging.Logger | None], MessageChannel]


def _build_telegram(
    messenger: MessengerConfig,
    telegram: TelegramConfig,
    logger: logging.Logger | None,
) -> MessageChannel:
    return TelegramChannel(telegram, logger=logger)


def _build_discord(
    messenger: MessengerConfig,
    telegram: TelegramConfig,
    logger: logging.Logger | None,
) -> MessageChannel:
    return DiscordWebhookChannel(messenger.discord, logger=logger)


_CHANNEL_BUILDERS: dict[str, ChannelBuilder] = {
    "telegram": _build_telegram,
    "discord": _build_discord,
}


def build_channel(
    messenger: MessengerConfig,
    telegram: TelegramConfig,
    *,
    logger: logging.Logger | None = None,
) -> MessageChannel:
    """Return the channel selected by ``messenger.provider``.

    Unknown providers fall back to Telegram so a config typo degrades to the
    historical default rather than dropping notifications silently.
    """
    builder = _CHANNEL_BUILDERS.get(messenger.normalized_provider, _build_telegram)
    return builder(messenger, telegram, logger)


__all__ = ["ChannelBuilder", "build_channel"]
