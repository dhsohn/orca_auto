"""Telegram polling adapter for the provider-neutral bot application."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from orca_auto.core.config import TelegramConfig
from orca_auto.core.messaging import SendResult
from orca_auto.core.messaging.interactive import (
    ActionRows,
    Actor,
    BotReply,
    ConversationAddress,
    IncomingAction,
    IncomingCommand,
)
from orca_auto.core.messaging.render_telegram import render_telegram_chunks
from orca_auto.core.notifications import split_telegram_message
from orca_auto.flow.telegram.bot_api import POLL_TIMEOUT_SECONDS, api_call

from ..application import BotApplication

LOGGER = logging.getLogger(__name__)
PROVIDER = "telegram"
_PRE_WRAPPER_LENGTH = len("<pre></pre>")


def _actor(raw: object) -> Actor:
    data = raw if isinstance(raw, dict) else {}
    user_id = str(data.get("id") or "").strip()
    label = str(data.get("username") or data.get("first_name") or user_id).strip()
    return Actor(user_id=user_id, label=label)


def _address(message: dict[str, Any]) -> ConversationAddress | None:
    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    channel_id = str(chat.get("id") or "").strip()
    if not channel_id:
        return None
    thread_id = str(message.get("message_thread_id") or "").strip() or None
    return ConversationAddress(PROVIDER, channel_id, thread_id)


def _is_authorized(
    message: dict[str, Any],
    actor: Actor,
    allowed_user_ids: tuple[str, ...],
) -> bool:
    if allowed_user_ids:
        return actor.user_id in allowed_user_ids
    chat = message.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    # A private chat is single-user. Groups must opt into an explicit actor
    # allowlist before destructive controls are enabled.
    return str(chat.get("type") or "").strip().lower() == "private"


def parse_update(
    update: object,
    *,
    allowed_chat_id: str,
    allowed_user_ids: tuple[str, ...] = (),
) -> tuple[int | None, IncomingCommand | IncomingAction | None]:
    """Translate one Telegram update into a neutral command or action."""

    if not isinstance(update, dict):
        return None, None
    try:
        update_id = int(update.get("update_id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return None, None
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        message = message if isinstance(message, dict) else {}
        address = _address(message)
        if address is None or address.channel_id != allowed_chat_id:
            return update_id, None
        actor = _actor(callback.get("from"))
        if not _is_authorized(message, actor, allowed_user_ids):
            return update_id, None
        action_id = str(callback.get("data") or "").strip()
        ack_token = str(callback.get("id") or "").strip()
        if not action_id or not ack_token:
            return update_id, None
        return (
            update_id,
            IncomingAction(
                address=address,
                actor=actor,
                action_id=action_id,
                ack_token=ack_token,
                message_id=str(message.get("message_id") or "").strip() or None,
            ),
        )

    message = update.get("message")
    if not isinstance(message, dict):
        return update_id, None
    address = _address(message)
    if address is None or address.channel_id != allowed_chat_id:
        return update_id, None
    actor = _actor(message.get("from"))
    if not _is_authorized(message, actor, allowed_user_ids):
        return update_id, None
    text = str(message.get("text") or "").strip()
    if not text.startswith(("/", "!")):
        return update_id, None
    parts = text.split(maxsplit=1)
    command = parts[0][1:].split("@", 1)[0].strip().lower()
    if not command:
        return update_id, None
    return (
        update_id,
        IncomingCommand(
            address=address,
            actor=actor,
            command=command,
            args=parts[1] if len(parts) > 1 else "",
            message_id=str(message.get("message_id") or "").strip() or None,
        ),
    )


def _keyboard(actions: ActionRows | None) -> dict[str, object]:
    rows = actions or ()
    return {
        "inline_keyboard": [
            [{"text": action.label, "callback_data": action.action_id} for action in row]
            for row in rows
            if row
        ]
    }


def _escaped_preformatted_chunks(text: str) -> list[str]:
    limit = 4096 - _PRE_WRAPPER_LENGTH
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for character in text:
        escaped = html.escape(character, quote=False)
        if current and current_size + len(escaped) > limit:
            chunks.append("".join(current))
            current = []
            current_size = 0
        current.append(escaped)
        current_size += len(escaped)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def _reply_chunks(reply: BotReply) -> list[tuple[str, str | None]]:
    if reply.message is not None:
        return [(chunk.html, "HTML") for chunk in render_telegram_chunks(reply.message)]
    if reply.format == "preformatted":
        return [
            (f"<pre>{chunk}</pre>", "HTML") for chunk in _escaped_preformatted_chunks(reply.text)
        ]
    return [(chunk, None) for chunk in split_telegram_message(reply.text, limit=4096)]


def _telegram_numeric(value: str) -> int | str:
    stripped = value.strip()
    if stripped.removeprefix("-").isdigit():
        return int(stripped)
    return stripped


@dataclass(frozen=True)
class TelegramInteractiveMessenger:
    config: TelegramConfig
    provider: str = PROVIDER
    _deferred_tokens: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
        compare=False,
    )

    def _base_payload(self, address: ConversationAddress) -> dict[str, object]:
        payload: dict[str, object] = {"chat_id": _telegram_numeric(address.channel_id)}
        if address.thread_id is not None:
            payload["message_thread_id"] = _telegram_numeric(address.thread_id)
        return payload

    def send_reply(
        self,
        address: ConversationAddress,
        reply: BotReply,
        *,
        silent: bool = False,
    ) -> SendResult:
        chunks = _reply_chunks(reply)
        message_ids: list[str] = []
        for index, (text, parse_mode) in enumerate(chunks):
            payload = self._base_payload(address)
            payload["text"] = text
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            if silent:
                payload["disable_notification"] = True
            if index == len(chunks) - 1 and reply.actions:
                payload["reply_markup"] = _keyboard(reply.actions)
            result = api_call(self.config.bot_token, "sendMessage", payload)
            if not isinstance(result, dict) or result.get("message_id") is None:
                return SendResult(
                    sent=False,
                    error="telegram_unconfirmed_delivery",
                    provider=self.provider,
                    message_ids=tuple(message_ids),
                    sent_count=len(message_ids),
                    total_count=len(chunks),
                )
            message_ids.append(str(result["message_id"]))
        return SendResult(
            sent=True,
            provider=self.provider,
            message_id=message_ids[-1] if message_ids else None,
            message_ids=tuple(message_ids),
            sent_count=len(message_ids),
            total_count=len(chunks),
        )

    def edit_actions(
        self,
        address: ConversationAddress,
        message_id: str,
        actions: ActionRows | None,
    ) -> SendResult:
        payload = self._base_payload(address)
        payload.update(
            {"message_id": _telegram_numeric(message_id), "reply_markup": _keyboard(actions)}
        )
        result = api_call(self.config.bot_token, "editMessageReplyMarkup", payload)
        return SendResult(
            sent=result is not None,
            error="" if result is not None else "telegram_edit_failed",
            provider=self.provider,
            message_id=message_id,
            sent_count=1 if result is not None else 0,
            total_count=1,
        )

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult:
        if action.ack_token in self._deferred_tokens:
            self._deferred_tokens.discard(action.ack_token)
            return SendResult(
                sent=True,
                skipped=True,
                provider=self.provider,
                sent_count=1,
                total_count=1,
            )
        result = api_call(
            self.config.bot_token,
            "answerCallbackQuery",
            {"callback_query_id": action.ack_token, "text": text[:200]},
        )
        return SendResult(
            sent=result is not None,
            error="" if result is not None else "telegram_ack_failed",
            provider=self.provider,
            sent_count=1 if result is not None else 0,
            total_count=1,
        )

    def defer_action(self, action: IncomingAction) -> SendResult:
        """Acknowledge a callback before filesystem/application work begins."""

        result = api_call(
            self.config.bot_token,
            "answerCallbackQuery",
            {"callback_query_id": action.ack_token, "text": "Processing…"},
        )
        if result is not None:
            self._deferred_tokens.add(action.ack_token)
        return SendResult(
            sent=result is not None,
            error="" if result is not None else "telegram_defer_failed",
            provider=self.provider,
            sent_count=1 if result is not None else 0,
            total_count=1,
        )


def _set_commands(config: TelegramConfig) -> None:
    api_call(
        config.bot_token,
        "setMyCommands",
        {
            "commands": [
                {"command": "list", "description": "Show unified activities"},
                {"command": "cancel", "description": "Cancel a workflow or job"},
                {"command": "help", "description": "Show help"},
            ]
        },
    )


def run_telegram_bot(
    application: BotApplication,
    config: TelegramConfig,
    *,
    sleep: Any = time.sleep,
) -> int:
    if not config.interactive_enabled:
        raise ValueError(
            "messenger.telegram.bot_token and chat_id are required; group chats also require "
            "allowed_user_ids"
        )
    messenger = TelegramInteractiveMessenger(config)
    _set_commands(config)
    offset = 0
    failure_count = 0
    LOGGER.info("orca_auto Telegram gateway ready")
    while True:
        try:
            updates = api_call(
                config.bot_token,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": POLL_TIMEOUT_SECONDS,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=POLL_TIMEOUT_SECONDS + 5,
            )
            if not isinstance(updates, list):
                failure_count += 1
                sleep(min(30, 2 ** min(failure_count, 5)))
                continue
            failure_count = 0
            for update in updates:
                update_id, incoming = parse_update(
                    update,
                    allowed_chat_id=config.chat_id,
                    allowed_user_ids=config.allowed_user_ids,
                )
                try:
                    if incoming is None and isinstance(update, dict):
                        callback = update.get("callback_query")
                        callback = callback if isinstance(callback, dict) else {}
                        callback_id = str(callback.get("id") or "").strip()
                        if callback_id:
                            api_call(
                                config.bot_token,
                                "answerCallbackQuery",
                                {
                                    "callback_query_id": callback_id,
                                    "text": "Action is not authorized or is unavailable.",
                                },
                            )
                    if isinstance(incoming, IncomingCommand):
                        application.dispatch_command(incoming, messenger=messenger)
                    elif isinstance(incoming, IncomingAction):
                        messenger.defer_action(incoming)
                        application.dispatch_action(incoming, messenger=messenger)
                except Exception:  # noqa: BLE001 - one poison update must not block polling
                    LOGGER.exception("telegram_update_dispatch_failed")
                    if isinstance(incoming, IncomingAction):
                        messenger.acknowledge(incoming, "Action failed. Run the command again.")
                        messenger.send_reply(
                            incoming.address,
                            BotReply("Action failed. Run the command again."),
                            silent=True,
                        )
                finally:
                    # Telegram returns the same update until the offset passes
                    # it. Retrying an event after an application-side partial
                    # effect could repeat a destructive action and starve all
                    # later updates, so consume it and let the user retry.
                    if update_id is not None:
                        offset = max(offset, update_id + 1)
        except KeyboardInterrupt:
            return 0
        except Exception:
            LOGGER.exception("telegram_gateway_error")
            sleep(5)


__all__ = [
    "TelegramInteractiveMessenger",
    "parse_update",
    "run_telegram_bot",
]
