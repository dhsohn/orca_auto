"""The outbound messenger port shared by all notifiers, and its config resolver."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from orca_auto.core.config import MessengerConfig

from .richtext import Message


@dataclass(frozen=True)
class SendResult:
    sent: bool
    skipped: bool = False
    error: str = ""


@runtime_checkable
class MessageChannel(Protocol):
    """A destination that renders and delivers :class:`Message` notifications.

    Implementations own their own native rendering and transport (Discord embeds
    over the Bot API). Callers build a provider-neutral :class:`Message` and
    never see the wire format.
    """

    @property
    def enabled(self) -> bool: ...

    def send(self, message: Message) -> SendResult: ...


@dataclass(frozen=True)
class DisabledChannel:
    """Null channel used when no messenger is configured: every send is skipped."""

    @property
    def enabled(self) -> bool:
        return False

    def send(self, message: Message) -> SendResult:
        return SendResult(sent=False, skipped=True, error="messenger_disabled")


def build_channel(
    messenger: MessengerConfig,
    *,
    logger: logging.Logger | None = None,
) -> MessageChannel:
    """Return the Discord bot channel when its config is complete, else a null channel.

    The adapter is imported here, not at module import, so importing the
    messaging package never loads transport code.
    """
    if not messenger.discord.bot_notification_enabled:
        return DisabledChannel()
    from .discord_bot import DiscordBotChannel

    return DiscordBotChannel(messenger.discord, logger=logger)


__all__ = ["DisabledChannel", "MessageChannel", "SendResult", "build_channel"]
