"""Resolve the active :class:`MessageChannel` from configuration.

Adding a messenger is a one-line entry here plus an adapter module — the same
"name → factory" seam the engines use, kept as a plain dict because the adapters
live in this package (no cross-layer lazy import needed).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from orca_auto.core.config import MessengerConfig

from .channel import MessageChannel
from .discord_bot import DiscordBotChannel
from .telegram_channel import TelegramChannel

ChannelBuilder = Callable[[MessengerConfig, logging.Logger | None], MessageChannel]


def _build_telegram(
    messenger: MessengerConfig,
    logger: logging.Logger | None,
) -> MessageChannel:
    return TelegramChannel(messenger.telegram, logger=logger)


def _build_discord(
    messenger: MessengerConfig,
    logger: logging.Logger | None,
) -> MessageChannel:
    return DiscordBotChannel(messenger.discord, logger=logger)


_CHANNEL_BUILDERS: dict[str, ChannelBuilder] = {
    "telegram": _build_telegram,
    "discord": _build_discord,
}


def build_channel(
    messenger: MessengerConfig,
    *,
    logger: logging.Logger | None = None,
) -> MessageChannel:
    """Return the channel selected by ``messenger.provider``.

    Unsupported providers fail closed instead of silently routing credentials
    and messages through a different adapter.
    """
    provider = messenger.normalized_provider
    builder = _CHANNEL_BUILDERS.get(provider)
    if builder is None:
        raise ValueError("Unsupported messenger provider.") from None
    return builder(messenger, logger)


__all__ = ["ChannelBuilder", "build_channel"]
