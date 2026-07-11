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
    provider: str | None = None
    message_id: str | None = None
    message_ids: tuple[str, ...] = ()
    sent_count: int = 0
    total_count: int = 0

    @property
    def partial(self) -> bool:
        return 0 < self.sent_count < self.total_count


def send_ok(result: SendResult, *, skipped_ok: bool = False) -> bool:
    return result.sent or (skipped_ok and result.skipped)


@runtime_checkable
class MessageChannel(Protocol):
    """A destination that renders and delivers :class:`Message` notifications.

    Implementations own their own native rendering and transport (Telegram HTML
    over the Bot API, Discord embeds over the Bot API, …). Callers build a
    provider-neutral :class:`Message` and never see the wire format.
    """

    @property
    def enabled(self) -> bool: ...

    def send(self, message: Message, *, silent: bool = False) -> SendResult: ...


__all__ = ["MessageChannel", "SendResult", "send_ok"]
