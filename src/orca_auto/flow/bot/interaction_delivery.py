"""Shared provider-neutral delivery helpers for interactive bot applications."""

from __future__ import annotations

import logging

from orca_auto.core.messaging.interactive import (
    BotReply,
    ConversationAddress,
    IncomingAction,
    InteractiveMessenger,
)

LOGGER = logging.getLogger(__name__)


def check_provider(
    address: ConversationAddress,
    messenger: InteractiveMessenger,
) -> None:
    if messenger.provider.strip().lower() != address.provider:
        raise ValueError("interactive messenger provider does not match the incoming conversation")


def invalid_action_text(status: str) -> str:
    return {
        "expired": "Action expired. Run the command again.",
        "wrong_address": "Action belongs to another conversation.",
        "wrong_actor": "Action belongs to another user.",
        "wrong_binding": "Action belongs to another conversation or user.",
    }.get(status, "Action is unavailable or was already used.")


def clear_origin_actions(
    action: IncomingAction,
    messenger: InteractiveMessenger,
) -> None:
    if action.message_id is None:
        return
    try:
        result = messenger.edit_actions(action.address, action.message_id, None)
        if not result.sent:
            LOGGER.warning("interactive_action_cleanup_failed: %s", result.error)
    except Exception:  # noqa: BLE001 - best-effort UI cleanup at transport boundary
        LOGGER.warning("interactive_action_cleanup_failed", exc_info=True)


def acknowledge(
    incoming: IncomingAction,
    text: str,
    messenger: InteractiveMessenger,
) -> None:
    result = messenger.acknowledge(incoming, text)
    if not result.sent:
        LOGGER.warning("interactive_action_ack_failed: %s", result.error)


def send_action_reply(
    incoming: IncomingAction,
    reply: BotReply,
    messenger: InteractiveMessenger,
) -> None:
    result = messenger.send_reply(incoming.address, reply)
    if not result.sent:
        LOGGER.warning("interactive_action_reply_failed: %s", result.error)


__all__ = [
    "acknowledge",
    "check_provider",
    "clear_origin_actions",
    "invalid_action_text",
    "send_action_reply",
]
