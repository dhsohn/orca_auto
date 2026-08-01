"""The outbound messenger port shared by all notifiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

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


__all__ = ["MessageChannel", "SendResult"]
