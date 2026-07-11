"""Discord gateway adapter for the provider-neutral bot application."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from orca_auto.core.config import DiscordConfig
from orca_auto.core.messaging import SendResult
from orca_auto.core.messaging.interactive import (
    ActionRows,
    Actor,
    BotReply,
    ConversationAddress,
    IncomingAction,
    IncomingCommand,
    IncomingUpload,
)

from ..application import BotApplication

LOGGER = logging.getLogger(__name__)
PROVIDER = "discord"
_BRIDGE_TIMEOUT_SECONDS = 20
_DISCORD_TEXT_LIMIT = 2000
_DISCORD_CODE_LIMIT = 1992
_VIEW_TIMEOUT_SECONDS = 300
_MAX_PENDING_CHANNELS = 16


@dataclass
class _ChannelAdmission:
    max_pending: int = _MAX_PENDING_CHANNELS
    pending: set[str] = field(default_factory=set)

    def acquire(self, channel_id: str) -> bool:
        if channel_id in self.pending or len(self.pending) >= self.max_pending:
            return False
        self.pending.add(channel_id)
        return True

    def release(self, channel_id: str) -> None:
        self.pending.discard(channel_id)


class _PartialDiscordSendError(RuntimeError):
    def __init__(self, message_ids: list[str], total_count: int) -> None:
        super().__init__("Discord reply failed after partial delivery")
        self.message_ids = tuple(message_ids)
        self.total_count = total_count


def _chunks(text: str, *, limit: int) -> list[str]:
    remaining = text.strip()
    if not remaining:
        return [" "]
    chunks: list[str] = []
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _reply_chunks(reply: BotReply) -> list[str]:
    if reply.format == "preformatted":
        safe = reply.text.replace("```", "`\u200b``")
        return [f"```\n{chunk}\n```" for chunk in _chunks(safe, limit=_DISCORD_CODE_LIMIT)]
    return _chunks(reply.text, limit=_DISCORD_TEXT_LIMIT)


def _view(actions: ActionRows | None) -> Any:
    import discord

    rows = actions or ()
    if len(rows) > 5:
        raise ValueError("Discord supports at most five action rows")
    view = discord.ui.View(timeout=_VIEW_TIMEOUT_SECONDS)
    for row_index, row in enumerate(rows):
        if len(row) > 5:
            raise ValueError("Discord supports at most five buttons per row")
        for action in row:
            if len(action.action_id) > 100:
                raise ValueError("Discord action ids must not exceed 100 characters")
            if len(action.label) > 80:
                raise ValueError("Discord action labels must not exceed 80 characters")
            view.add_item(
                discord.ui.Button(
                    label=action.label,
                    custom_id=action.action_id,
                    style=discord.ButtonStyle.secondary,
                    row=row_index,
                )
            )
    return view


def _actor(user: Any) -> Actor:
    user_id = str(getattr(user, "id", "")).strip()
    label = str(
        getattr(user, "global_name", None)
        or getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or user_id
    ).strip()
    return Actor(user_id=user_id, label=label)


@dataclass
class DiscordInteractiveMessenger:
    client: Any
    loop: Any
    config: DiscordConfig
    provider: str = PROVIDER
    _interactions: dict[str, Any] = field(default_factory=dict)
    _interaction_lock: threading.Lock = field(default_factory=threading.Lock)

    def _run(self, coroutine: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=_BRIDGE_TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            raise

    async def _resolve_channel(self, channel_id: str) -> Any:
        numeric_id = int(channel_id)
        channel = self.client.get_channel(numeric_id)
        if channel is None:
            channel = await self.client.fetch_channel(numeric_id)
        return channel

    async def _send_reply(
        self,
        address: ConversationAddress,
        chunks: list[str],
        actions: ActionRows,
        message_ids: list[str],
        *,
        silent: bool,
    ) -> list[str]:
        import discord

        channel = await self._resolve_channel(address.channel_id)
        for index, chunk in enumerate(chunks):
            try:
                message = await channel.send(
                    chunk,
                    view=(_view(actions) if index == len(chunks) - 1 and actions else None),
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                )
            except Exception as exc:  # noqa: BLE001 - preserve partial SDK delivery receipt
                raise _PartialDiscordSendError(message_ids, len(chunks)) from exc
            message_ids.append(str(message.id))
        return message_ids

    def send_reply(
        self,
        address: ConversationAddress,
        reply: BotReply,
        *,
        silent: bool = False,
    ) -> SendResult:
        chunks = _reply_chunks(reply)
        message_ids: list[str] = []
        try:
            self._run(
                self._send_reply(
                    address,
                    chunks,
                    reply.actions,
                    message_ids,
                    silent=silent,
                )
            )
        except _PartialDiscordSendError as exc:
            LOGGER.warning("discord_reply_failed_after_%d_chunks", len(exc.message_ids))
            return SendResult(
                sent=False,
                error="discord_partial_reply",
                provider=self.provider,
                message_id=exc.message_ids[-1] if exc.message_ids else None,
                message_ids=exc.message_ids,
                sent_count=len(exc.message_ids),
                total_count=exc.total_count,
            )
        except TimeoutError:
            LOGGER.warning("discord_reply_timed_out_after_%d_chunks", len(message_ids))
            return SendResult(
                sent=False,
                error="discord_reply_timeout",
                provider=self.provider,
                message_id=message_ids[-1] if message_ids else None,
                message_ids=tuple(message_ids),
                sent_count=len(message_ids),
                total_count=len(chunks),
            )
        except Exception as exc:  # noqa: BLE001 - SDK exceptions stay behind adapter boundary
            LOGGER.warning("discord_reply_failed: %s", type(exc).__name__)
            return SendResult(
                sent=False,
                error="discord_reply_failed",
                provider=self.provider,
                message_id=message_ids[-1] if message_ids else None,
                message_ids=tuple(message_ids),
                sent_count=len(message_ids),
                total_count=len(chunks),
            )
        return SendResult(
            sent=True,
            provider=self.provider,
            message_id=message_ids[-1] if message_ids else None,
            message_ids=tuple(message_ids),
            sent_count=len(message_ids),
            total_count=len(message_ids),
        )

    async def _edit_actions(
        self,
        address: ConversationAddress,
        message_id: str,
        actions: ActionRows | None,
    ) -> None:
        import discord

        channel = await self._resolve_channel(address.channel_id)
        await channel.get_partial_message(int(message_id)).edit(
            view=_view(actions) if actions else None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def edit_actions(
        self,
        address: ConversationAddress,
        message_id: str,
        actions: ActionRows | None,
    ) -> SendResult:
        try:
            self._run(self._edit_actions(address, message_id, actions))
        except Exception as exc:  # noqa: BLE001 - SDK exceptions stay behind adapter boundary
            LOGGER.warning("discord_action_edit_failed: %s", type(exc).__name__)
            return SendResult(
                sent=False,
                error="discord_action_edit_failed",
                provider=self.provider,
                message_id=message_id,
                total_count=1,
            )
        return SendResult(
            sent=True,
            provider=self.provider,
            message_id=message_id,
            sent_count=1,
            total_count=1,
        )

    def register_interaction(self, token: str, interaction: Any) -> None:
        with self._interaction_lock:
            self._interactions[token] = interaction

    def discard_interaction(self, token: str) -> None:
        with self._interaction_lock:
            self._interactions.pop(token, None)

    async def _acknowledge(self, interaction: Any, text: str) -> None:
        import discord

        if interaction.response.is_done():
            await interaction.followup.send(
                text[:2000],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                text[:2000],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    def acknowledge(self, action: IncomingAction, text: str) -> SendResult:
        with self._interaction_lock:
            interaction = self._interactions.pop(action.ack_token, None)
        if interaction is None:
            return SendResult(
                sent=False,
                skipped=True,
                error="discord_interaction_missing",
                provider=self.provider,
            )
        try:
            self._run(self._acknowledge(interaction, text))
        except Exception as exc:  # noqa: BLE001 - SDK exceptions stay behind adapter boundary
            LOGGER.warning("discord_interaction_ack_failed: %s", type(exc).__name__)
            return SendResult(
                sent=False,
                error="discord_interaction_ack_failed",
                provider=self.provider,
                total_count=1,
            )
        return SendResult(sent=True, provider=self.provider, sent_count=1, total_count=1)


def _is_run_command(text: str) -> bool:
    if not text.startswith(("/", "!")):
        return False
    parts = text.split(maxsplit=1)
    return parts[0][1:].split("@", 1)[0].strip().lower() == "run"


def _command_from_message(message: Any) -> IncomingCommand | None:
    text = str(getattr(message, "content", "") or "").strip()
    if not text.startswith(("/", "!")):
        return None
    parts = text.split(maxsplit=1)
    command = parts[0][1:].strip().lower()
    if not command:
        return None
    channel_id = str(getattr(getattr(message, "channel", None), "id", "")).strip()
    if not channel_id:
        return None
    return IncomingCommand(
        address=ConversationAddress(PROVIDER, channel_id),
        actor=_actor(getattr(message, "author", None)),
        command=command,
        args=parts[1] if len(parts) > 1 else "",
        message_id=str(getattr(message, "id", "")).strip() or None,
    )


def _action_from_interaction(interaction: Any) -> IncomingAction | None:
    data = getattr(interaction, "data", None)
    data = data if isinstance(data, dict) else {}
    action_id = str(data.get("custom_id") or "").strip()
    channel_id = str(getattr(interaction, "channel_id", "") or "").strip()
    ack_token = str(getattr(interaction, "id", "") or "").strip()
    if not action_id or not channel_id or not ack_token:
        return None
    message = getattr(interaction, "message", None)
    return IncomingAction(
        address=ConversationAddress(PROVIDER, channel_id),
        actor=_actor(getattr(interaction, "user", None)),
        action_id=action_id,
        ack_token=ack_token,
        message_id=str(getattr(message, "id", "")).strip() or None,
    )


def run_discord_bot(application: BotApplication, config: DiscordConfig) -> int:
    if not config.interactive_enabled:
        raise ValueError(
            "messenger.discord.bot_token, allowed_user_ids, and at least one channel_ids "
            "entry are required"
        )

    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="orca-auto-discord")
    admission = _ChannelAdmission()
    messenger: DiscordInteractiveMessenger | None = None

    def _dispatch_command(incoming: IncomingCommand) -> None:
        if messenger is None:
            return
        application.dispatch_command(incoming, messenger=messenger)

    def _dispatch_action(incoming: IncomingAction) -> None:
        if messenger is None:
            return
        try:
            application.dispatch_action(incoming, messenger=messenger)
        except Exception:
            LOGGER.exception("discord_action_dispatch_failed")
            messenger.acknowledge(incoming, "Action failed. Try the command again.")
        finally:
            messenger.discard_interaction(incoming.ack_token)

    def _dispatch_upload(incoming: IncomingUpload) -> None:
        if messenger is None:
            return
        application.dispatch_upload(incoming, messenger=messenger)

    async def _submit(channel_id: str, callback: Any, incoming: Any) -> str:
        if not admission.acquire(channel_id):
            return "busy"
        try:
            await asyncio.get_running_loop().run_in_executor(executor, callback, incoming)
            return "ok"
        except Exception:  # noqa: BLE001 - worker exceptions are reported at adapter boundary
            LOGGER.exception("discord_command_dispatch_failed")
            return "failed"
        finally:
            admission.release(channel_id)

    @client.event
    async def on_ready() -> None:
        nonlocal messenger
        # discord.py may emit on_ready again after a reconnect. Keep the same
        # bridge so deferred interactions registered before the reconnect are
        # still available to the worker that acknowledges them.
        if messenger is None:
            messenger = DiscordInteractiveMessenger(
                client=client,
                loop=asyncio.get_running_loop(),
                config=config,
            )
        LOGGER.info("orca_auto Discord gateway ready as %s", client.user)

    async def _reply_plain(channel: Any, text: str) -> None:
        if channel is not None:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _handle_upload(
        message: Any,
        channel: Any,
        channel_id: str,
        actor: Actor,
        attachment: Any,
    ) -> None:
        if messenger is None:
            return
        if not config.uploads.enabled:
            await _reply_plain(channel, "File uploads are disabled.")
            return
        size = int(getattr(attachment, "size", 0) or 0)
        if size > config.uploads.max_archive_bytes:
            limit_mib = config.uploads.max_archive_bytes / (1024 * 1024)
            await _reply_plain(channel, f"Attachment too large (limit {limit_mib:.0f} MiB).")
            return
        filename = str(getattr(attachment, "filename", "upload"))
        try:
            staged = application.stage_upload_path(filename)
            await attachment.save(staged)
        except Exception:  # noqa: BLE001 - download failures are reported to the channel
            LOGGER.exception("discord_upload_download_failed")
            await _reply_plain(channel, "Could not download the attachment. Try again.")
            return
        upload = IncomingUpload(
            address=ConversationAddress(PROVIDER, channel_id),
            actor=actor,
            filename=filename,
            size=size,
            archive_path=str(staged),
            message_id=str(getattr(message, "id", "")).strip() or None,
        )
        outcome = await _submit(channel_id, _dispatch_upload, upload)
        if outcome != "ok":
            # dispatch (which owns staged-file cleanup) never ran on a busy/failed
            # admission, so remove the archive we already downloaded rather than
            # leave it for the hour-long staging sweep.
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("discord_upload_staging_cleanup_skip")
            await _reply_plain(
                channel,
                (
                    "orca_auto is already processing a request for this channel. Try again."
                    if outcome == "busy"
                    else "orca_auto could not process that upload. Try again."
                ),
            )

    @client.event
    async def on_message(message: Any) -> None:
        author = getattr(message, "author", None)
        if bool(getattr(author, "bot", False)):
            return
        channel = getattr(message, "channel", None)
        channel_id = str(getattr(channel, "id", "")).strip()
        if not channel_id or channel_id not in config.channel_ids:
            return
        actor = _actor(author)
        if config.allowed_user_ids and actor.user_id not in config.allowed_user_ids:
            return
        text = str(getattr(message, "content", "") or "").strip()
        attachments = list(getattr(message, "attachments", ()) or ())
        if _is_run_command(text) and attachments:
            await _handle_upload(message, channel, channel_id, actor, attachments[0])
            return
        incoming = _command_from_message(message)
        if incoming is None:
            return
        outcome = await _submit(channel_id, _dispatch_command, incoming)
        if outcome != "ok":
            await _reply_plain(
                channel,
                (
                    "orca_auto is already processing a command for this channel. Try again."
                    if outcome == "busy"
                    else "orca_auto could not process that command. Try again."
                ),
            )

    @client.event
    async def on_interaction(interaction: Any) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        incoming = _action_from_interaction(interaction)
        if incoming is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if incoming.address.channel_id not in config.interaction_channel_ids:
            await interaction.followup.send(
                "This channel is no longer authorized for orca_auto controls.",
                ephemeral=True,
            )
            return
        if config.allowed_user_ids and incoming.actor.user_id not in config.allowed_user_ids:
            await interaction.followup.send(
                "Not authorized for orca_auto controls.", ephemeral=True
            )
            return
        if messenger is None:
            await interaction.followup.send("Bot is still starting. Try again.", ephemeral=True)
            return
        messenger.register_interaction(incoming.ack_token, interaction)
        outcome = await _submit(incoming.address.channel_id, _dispatch_action, incoming)
        if outcome != "ok":
            messenger.discard_interaction(incoming.ack_token)
            await interaction.followup.send(
                (
                    "orca_auto is already processing an action for this channel. Try again."
                    if outcome == "busy"
                    else "orca_auto could not process that action. Try again."
                ),
                ephemeral=True,
            )

    try:
        client.run(config.bot_token, log_handler=None)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return 0


__all__ = [
    "DiscordInteractiveMessenger",
    "run_discord_bot",
]
