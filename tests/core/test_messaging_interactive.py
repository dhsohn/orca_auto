"""Focused tests for provider-neutral interactive messaging contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from orca_auto.core.messaging import (
    Actor,
    BotReply,
    CardAction,
    ConversationAddress,
    IncomingAction,
    InteractiveMessenger,
    SendResult,
)


class _FakeInteractiveMessenger:
    provider = "fake"

    def send_reply(
        self,
        address: ConversationAddress,
        reply: BotReply,
        *,
        silent: bool = False,
    ) -> SendResult:
        del address, reply, silent
        return SendResult(sent=True, provider=self.provider, message_id="1")

    def edit_actions(
        self,
        address: ConversationAddress,
        message_id: str,
        actions: tuple[tuple[CardAction, ...], ...] | None,
    ) -> SendResult:
        del address, message_id, actions
        return SendResult(sent=True, provider=self.provider, message_id="1")

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult:
        del action, text
        return SendResult(sent=True, provider=self.provider)


def test_conversation_address_is_normalized_and_provider_qualified() -> None:
    address = ConversationAddress(" Discord ", " 123 ", " 456 ")

    assert address == ConversationAddress("discord", "123", "456")
    with pytest.raises(FrozenInstanceError):
        address.channel_id = "999"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("provider", "channel_id", "message"),
    [
        ("", "123", "provider"),
        ("discord", "", "channel_id"),
    ],
)
def test_conversation_address_rejects_empty_identity(
    provider: str,
    channel_id: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ConversationAddress(provider, channel_id)


def test_bot_reply_carries_immutable_action_rows() -> None:
    action = CardAction("cancel:abc", "Cancel")
    reply = BotReply(
        "Choose an action",
        format="preformatted",
        actions=((action,),),
    )

    assert reply.format == "preformatted"
    assert reply.actions == ((action,),)
    with pytest.raises(ValueError, match="format"):
        BotReply("bad", format="markdown")  # type: ignore[arg-type]


def test_interactive_messenger_protocol_uses_send_results() -> None:
    messenger = _FakeInteractiveMessenger()
    address = ConversationAddress("fake", "channel")
    actor = Actor("user", "User")
    action = IncomingAction(address, actor, "refresh", "ack")

    assert isinstance(messenger, InteractiveMessenger)
    assert messenger.send_reply(address, BotReply("ok")).sent
    assert messenger.edit_actions(address, "1", None).sent
    assert messenger.acknowledge(action, "done").sent
