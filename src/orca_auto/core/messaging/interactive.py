"""Provider-neutral contracts for interactive messenger applications.

Native Telegram updates and Discord gateway events are translated into these
immutable values at the adapter boundary.  Application code can then dispatch
commands and button actions without importing either provider SDK or wire
format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from .channel import SendResult

BotReplyFormat = Literal["plain", "preformatted"]


@dataclass(frozen=True)
class ConversationAddress:
    """A provider-qualified channel and optional topic/thread."""

    provider: str
    channel_id: str
    thread_id: str | None = None

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower()
        channel_id = self.channel_id.strip()
        thread_id = self.thread_id.strip() if self.thread_id is not None else None
        if not provider:
            raise ValueError("conversation provider must not be empty")
        if not channel_id:
            raise ValueError("conversation channel_id must not be empty")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "channel_id", channel_id)
        object.__setattr__(self, "thread_id", thread_id or None)


@dataclass(frozen=True)
class Actor:
    """Provider-neutral identity attached to an inbound event."""

    user_id: str
    label: str = ""


@dataclass(frozen=True)
class CardAction:
    """One button whose opaque id round-trips through a provider adapter."""

    action_id: str
    label: str

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("card action_id must not be empty")
        if not self.label:
            raise ValueError("card action label must not be empty")


ActionRows: TypeAlias = tuple[tuple[CardAction, ...], ...]


@dataclass(frozen=True)
class BotReply:
    """Application reply plus optional provider-rendered action rows."""

    text: str
    format: BotReplyFormat = "plain"
    actions: ActionRows = ()

    def __post_init__(self) -> None:
        if self.format not in ("plain", "preformatted"):
            raise ValueError("bot reply format must be 'plain' or 'preformatted'")


@dataclass(frozen=True)
class IncomingCommand:
    """A normalized slash command parsed by a provider adapter."""

    address: ConversationAddress
    actor: Actor
    command: str
    args: str = ""
    message_id: str | None = None


@dataclass(frozen=True)
class IncomingAction:
    """A normalized button/component interaction."""

    address: ConversationAddress
    actor: Actor
    action_id: str
    ack_token: str
    message_id: str | None = None


@runtime_checkable
class InteractiveMessenger(Protocol):
    """Outbound capabilities required by provider-neutral bot dispatch."""

    @property
    def provider(self) -> str: ...

    def send_reply(
        self,
        address: ConversationAddress,
        reply: BotReply,
        *,
        silent: bool = False,
    ) -> SendResult: ...

    def edit_actions(
        self,
        address: ConversationAddress,
        message_id: str,
        actions: ActionRows | None,
    ) -> SendResult: ...

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult: ...


__all__ = [
    "ActionRows",
    "Actor",
    "BotReply",
    "BotReplyFormat",
    "CardAction",
    "ConversationAddress",
    "IncomingAction",
    "IncomingCommand",
    "InteractiveMessenger",
]
