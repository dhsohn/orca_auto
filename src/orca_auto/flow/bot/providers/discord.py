"""Discord gateway adapter for the provider-neutral bot application."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from orca_auto.core.config import DiscordConfig
from orca_auto.core.ingest import (
    MAX_CONCURRENT_UPLOAD_DOWNLOADS,
    UploadQuotaExceededError,
    UploadSessionError,
    UploadState,
)
from orca_auto.core.messaging import SendResult, render_discord_embed
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
_MAX_PENDING_CHANNELS = MAX_CONCURRENT_UPLOAD_DOWNLOADS
_UPLOAD_SWEEP_INTERVAL_SECONDS = 60.0
_DISCORD_ATTACHMENT_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


class AttachmentDownloadRejected(RuntimeError):
    """Raised when Discord attachment bytes violate the bounded download contract."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _trusted_attachment_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise AttachmentDownloadRejected("Discord attachment URL is not a trusted CDN URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or host not in _DISCORD_ATTACHMENT_HOSTS
    ):
        raise AttachmentDownloadRejected("Discord attachment URL is not a trusted CDN URL")
    return text


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _download_attachment_url_bounded(
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> int:
    """Stream one trusted CDN attachment to an exclusive file with a hard byte cap."""

    trusted_url = _trusted_attachment_url(url)
    request = Request(
        trusted_url,
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": "orca_auto-discord-upload/1",
        },
    )
    opener = build_opener(_NoRedirect)
    timeout = max(0.1, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    written = 0
    created = False
    try:
        with opener.open(request, timeout=timeout) as response:
            if int(getattr(response, "status", response.getcode())) != 200:
                raise AttachmentDownloadRejected("Discord CDN returned a non-success status")
            length_text = str(response.headers.get("Content-Length") or "").strip()
            if length_text:
                try:
                    content_length = int(length_text)
                except ValueError as exc:
                    raise AttachmentDownloadRejected(
                        "Discord CDN returned an invalid Content-Length"
                    ) from exc
                if content_length < 0 or content_length > max_bytes:
                    raise AttachmentDownloadRejected("Discord attachment exceeds the byte limit")
            with destination.open("xb") as sink:
                created = True
                while True:
                    if time.monotonic() >= deadline:
                        raise AttachmentDownloadRejected("Discord attachment download timed out")
                    chunk = response.read(min(1024 * 1024, max_bytes + 1 - written))
                    if time.monotonic() >= deadline:
                        raise AttachmentDownloadRejected("Discord attachment download timed out")
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise AttachmentDownloadRejected(
                            "Discord attachment exceeds the byte limit"
                        )
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            _fsync_directory(destination.parent)
    except Exception:  # noqa: BLE001 - remove only the partial file this call created
        if created:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("discord_upload_partial_cleanup_skip")
        raise
    return written


async def _download_attachment_bounded(
    attachment: Any,
    destination: Path,
    *,
    max_bytes: int,
    timeout_seconds: float,
    executor: ThreadPoolExecutor,
) -> int:
    url = _trusted_attachment_url(getattr(attachment, "url", ""))
    callback = functools.partial(
        _download_attachment_url_bounded,
        url,
        destination,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    )
    future = asyncio.get_running_loop().run_in_executor(executor, callback)
    try:
        return int(await asyncio.shield(future))
    except asyncio.CancelledError:
        # Cancelling an asyncio wrapper does not stop a running executor thread.
        # Wait for its bounded network timeout and file cleanup before allowing
        # the caller to delete the durable reservation directory.
        try:
            await asyncio.shield(future)
        except Exception:  # noqa: BLE001 - original cancellation stays authoritative
            pass
        raise


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


@dataclass
class _CounterAdmission:
    """Event-loop-owned non-blocking cap for concurrent attachment downloads."""

    max_pending: int
    pending: int = 0

    def acquire(self) -> bool:
        if self.pending >= self.max_pending:
            return False
        self.pending += 1
        return True

    def release(self) -> None:
        if self.pending > 0:
            self.pending -= 1


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


def _embed_object(reply: BotReply) -> Any:
    """Build a Discord embed from ``reply.message``, or ``None`` for plain text."""
    if reply.message is None:
        return None
    import discord

    return discord.Embed.from_dict(render_discord_embed(reply.message))


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
        embed: Any = None,
        silent: bool,
    ) -> list[str]:
        import discord

        channel = await self._resolve_channel(address.channel_id)
        if embed is not None:
            # A rich reply is one embed message carrying the action buttons.
            try:
                message = await channel.send(
                    embed=embed,
                    view=(_view(actions) if actions else None),
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=silent,
                )
            except Exception as exc:  # noqa: BLE001 - preserve partial SDK delivery receipt
                raise _PartialDiscordSendError(message_ids, 1) from exc
            message_ids.append(str(message.id))
            return message_ids
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
        embed = _embed_object(reply)
        chunks = [] if embed is not None else _reply_chunks(reply)
        total = 1 if embed is not None else len(chunks)
        message_ids: list[str] = []
        try:
            self._run(
                self._send_reply(
                    address,
                    chunks,
                    reply.actions,
                    message_ids,
                    embed=embed,
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
                total_count=total,
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
                total_count=total,
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
    download_executor = ThreadPoolExecutor(
        max_workers=config.uploads.max_concurrent_downloads,
        thread_name_prefix="orca-auto-discord-download",
    )
    admission = _ChannelAdmission()
    upload_admission = _CounterAdmission(config.uploads.max_concurrent_downloads)
    messenger: DiscordInteractiveMessenger | None = None
    sweep_task: asyncio.Task[None] | None = None

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

    async def _sweep_uploads() -> None:
        while True:
            await asyncio.sleep(_UPLOAD_SWEEP_INTERVAL_SECONDS)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    executor,
                    application.sweep_upload_sessions,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - periodic maintenance must not stop the gateway
                LOGGER.exception("discord_upload_sweep_failed")

    @client.event
    async def on_ready() -> None:
        nonlocal messenger, sweep_task
        # discord.py may emit on_ready again after a reconnect. Keep the same
        # bridge so deferred interactions registered before the reconnect are
        # still available to the worker that acknowledges them.
        if messenger is None:
            messenger = DiscordInteractiveMessenger(
                client=client,
                loop=asyncio.get_running_loop(),
                config=config,
            )
        if config.uploads.enabled and (sweep_task is None or sweep_task.done()):
            sweep_task = asyncio.create_task(
                _sweep_uploads(),
                name="orca-auto-discord-upload-sweep",
            )
        LOGGER.info("orca_auto Discord gateway ready as %s", client.user)

    async def _reply_plain(channel: Any, text: str) -> None:
        if channel is not None:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def _abandon_upload(upload_id: str, reason: str) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                executor,
                application.abandon_upload,
                upload_id,
                reason,
            )
        except Exception:  # noqa: BLE001 - retain the original adapter failure
            LOGGER.exception("discord_upload_abandon_failed")

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
        raw_size: object = getattr(attachment, "size", None)
        size = raw_size if type(raw_size) is int else -1
        if size < 0:
            await _reply_plain(channel, "Attachment size is invalid.")
            return
        if size > config.uploads.max_archive_bytes:
            limit_mib = config.uploads.max_archive_bytes / (1024 * 1024)
            await _reply_plain(channel, f"Attachment too large (limit {limit_mib:.0f} MiB).")
            return
        message_id = str(getattr(message, "id", "") or "").strip()
        attachment_id = str(getattr(attachment, "id", "") or "").strip()
        if not message_id or not attachment_id:
            await _reply_plain(channel, "Attachment identity is unavailable. Try again.")
            return
        if not admission.acquire(channel_id):
            await _reply_plain(
                channel,
                "orca_auto is already processing a request for this channel. Try again.",
            )
            return
        if not upload_admission.acquire():
            admission.release(channel_id)
            await _reply_plain(channel, "Too many uploads are being downloaded. Try again later.")
            return

        filename = str(getattr(attachment, "filename", "upload"))
        upload_id: str | None = None
        download_slot_acquired = True
        try:
            address = ConversationAddress(PROVIDER, channel_id)
            reserve = functools.partial(
                application.reserve_upload,
                address=address,
                actor=actor,
                message_id=message_id,
                attachment_ids=(attachment_id,),
                expected_bytes=size,
            )
            reservation_future = asyncio.get_running_loop().run_in_executor(
                executor,
                reserve,
            )
            try:
                reservation = await asyncio.shield(reservation_future)
            except asyncio.CancelledError:
                # The store operation may have committed a reservation even
                # though gateway shutdown cancelled this task. Recover its id
                # before returning so no RECEIVING quota record is orphaned.
                try:
                    cancelled_reservation = await asyncio.shield(reservation_future)
                except Exception:  # noqa: BLE001 - no usable reservation was returned
                    pass
                else:
                    await asyncio.shield(
                        _abandon_upload(
                            cancelled_reservation.session.upload_id,
                            "Discord upload reservation cancelled",
                        )
                    )
                raise
            except UploadQuotaExceededError:
                await _reply_plain(channel, "Upload staging quota is full. Try again later.")
                return
            except (UploadSessionError, RuntimeError, OSError, TimeoutError):
                LOGGER.exception("discord_upload_reservation_failed")
                await _reply_plain(channel, "Could not reserve upload staging. Try again later.")
                return

            session = reservation.session
            upload_id = session.upload_id
            if reservation.created:
                try:
                    written = await _download_attachment_bounded(
                        attachment,
                        session.archive_path,
                        max_bytes=config.uploads.max_archive_bytes,
                        timeout_seconds=config.timeout_seconds,
                        executor=download_executor,
                    )
                    if written != size:
                        raise AttachmentDownloadRejected(
                            "Discord attachment size changed during download"
                        )
                    session = await asyncio.get_running_loop().run_in_executor(
                        executor,
                        application.finalize_upload,
                        upload_id,
                    )
                except asyncio.CancelledError:
                    await asyncio.shield(
                        _abandon_upload(upload_id, "Discord attachment download cancelled")
                    )
                    raise
                except Exception:  # noqa: BLE001 - normalize CDN/session failures at boundary
                    LOGGER.exception("discord_upload_download_failed")
                    await _abandon_upload(
                        upload_id,
                        "Discord attachment download failed",
                    )
                    await _reply_plain(channel, "Could not download the attachment. Try again.")
                    return
            upload_admission.release()
            download_slot_acquired = False

            if not reservation.created:
                if session.state is UploadState.RECEIVING and session.actual_bytes is None:
                    await _abandon_upload(upload_id, "interrupted Discord attachment download")
                    await _reply_plain(
                        channel,
                        "The previous download was interrupted. Attach the archive in a new message.",
                    )
                    return
                if session.state is UploadState.AWAITING_CONFIRM:
                    await _reply_plain(
                        channel,
                        "This upload is already awaiting confirmation; use the original buttons.",
                    )
                    return
                if session.state in {
                    UploadState.PROCESSING,
                    UploadState.PUBLISHED,
                    UploadState.AMBIGUOUS,
                }:
                    await _reply_plain(
                        channel,
                        "This upload is already being processed; check the list command.",
                    )
                    return
                if session.state is UploadState.COMMITTED:
                    await _reply_plain(channel, "This upload was already submitted.")
                    return
                if session.state in {UploadState.DISCARDED, UploadState.FAILED}:
                    await _reply_plain(channel, "This upload is no longer available.")
                    return

            upload = IncomingUpload(
                address=address,
                actor=actor,
                filename=filename,
                size=session.actual_bytes if session.actual_bytes is not None else size,
                archive_path=str(session.archive_path),
                message_id=message_id,
                attachment_id=attachment_id,
                upload_id=upload_id,
            )
            try:
                await asyncio.get_running_loop().run_in_executor(
                    executor,
                    _dispatch_upload,
                    upload,
                )
            except Exception:  # noqa: BLE001 - release only this pre-submit session
                LOGGER.exception("discord_upload_dispatch_failed")
                await _abandon_upload(
                    upload_id,
                    "Discord upload dispatch failed",
                )
                await _reply_plain(channel, "orca_auto could not process that upload. Try again.")
        finally:
            if download_slot_acquired:
                upload_admission.release()
            admission.release(channel_id)

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
            if len(attachments) != 1:
                await _reply_plain(channel, "Attach exactly one archive to the run command.")
                return
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
        try:
            outcome = await _submit(incoming.address.channel_id, _dispatch_action, incoming)
        finally:
            messenger.discard_interaction(incoming.ack_token)
        if outcome != "ok":
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
        download_executor.shutdown(wait=True, cancel_futures=True)
    return 0


__all__ = [
    "DiscordInteractiveMessenger",
    "run_discord_bot",
]
