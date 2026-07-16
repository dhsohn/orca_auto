from __future__ import annotations

import asyncio
import concurrent.futures
import html
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.config import DiscordConfig, MessengerConfig, TelegramConfig
from orca_auto.core.ingest import UploadPolicy, UploadState
from orca_auto.core.messaging.interactive import (
    Actor,
    BotReply,
    CardAction,
    ConversationAddress,
    IncomingAction,
    IncomingCommand,
    IncomingUpload,
)
from orca_auto.flow.bot import BotApplication, BotSettings, runner
from orca_auto.flow.bot.providers import discord as discord_provider
from orca_auto.flow.bot.providers import telegram as telegram_provider


def test_discord_reply_message_becomes_an_embed() -> None:
    from orca_auto.flow.bot.replies import reply_message

    assert discord_provider._embed_object(BotReply("plain")) is None
    embed = discord_provider._embed_object(BotReply("fallback", message=reply_message("Commands")))
    assert embed is not None
    assert embed.title == "Commands"
    assert embed.author.name == "orca_auto"


def test_telegram_reply_message_renders_html_chunks() -> None:
    from orca_auto.flow.bot.replies import reply_message

    chunks = telegram_provider._reply_chunks(
        BotReply("fallback", message=reply_message("Commands"))
    )
    assert chunks[0][1] == "HTML"
    assert "<b>Commands</b>" in chunks[0][0]
    assert chunks[0][0].startswith("orca_auto")


def test_telegram_parses_allowed_command_and_callback_into_neutral_events() -> None:
    command_update = {
        "update_id": 10,
        "message": {
            "message_id": 20,
            "message_thread_id": 30,
            "chat": {"id": "-100", "type": "private"},
            "from": {"id": 7, "username": "chemist"},
            "text": "/list@orca_auto running",
        },
    }

    update_id, command = telegram_provider.parse_update(
        command_update,
        allowed_chat_id="-100",
    )

    assert update_id == 10
    assert command == IncomingCommand(
        address=ConversationAddress("telegram", "-100", "30"),
        actor=Actor("7", "chemist"),
        command="list",
        args="running",
        message_id="20",
    )

    callback_update = {
        "update_id": 11,
        "callback_query": {
            "id": "ack-1",
            "data": "oa:short",
            "from": {"id": 7, "first_name": "Ada"},
            "message": {
                "message_id": 21,
                "chat": {"id": "-100", "type": "private"},
            },
        },
    }
    update_id, action = telegram_provider.parse_update(
        callback_update,
        allowed_chat_id="-100",
    )

    assert update_id == 11
    assert action == IncomingAction(
        address=ConversationAddress("telegram", "-100"),
        actor=Actor("7", "Ada"),
        action_id="oa:short",
        ack_token="ack-1",
        message_id="21",
    )


def test_telegram_rejects_other_chats_plain_text_and_malformed_update_ids() -> None:
    wrong_chat = {
        "update_id": 12,
        "message": {
            "chat": {"id": "other", "type": "private"},
            "from": {"id": 7},
            "text": "/list",
        },
    }
    plain_text = {
        "update_id": 13,
        "message": {
            "chat": {"id": "-100", "type": "private"},
            "from": {"id": 7},
            "text": "hello",
        },
    }

    assert telegram_provider.parse_update(wrong_chat, allowed_chat_id="-100") == (12, None)
    assert telegram_provider.parse_update(plain_text, allowed_chat_id="-100") == (13, None)
    assert telegram_provider.parse_update(
        {"update_id": "not-an-integer"},
        allowed_chat_id="-100",
    ) == (None, None)

    group_command = {
        "update_id": 14,
        "message": {
            "chat": {"id": "-100", "type": "supergroup"},
            "from": {"id": 7},
            "text": "/list",
        },
    }
    assert telegram_provider.parse_update(group_command, allowed_chat_id="-100") == (14, None)
    _, authorized = telegram_provider.parse_update(
        group_command,
        allowed_chat_id="-100",
        allowed_user_ids=("7",),
    )
    assert isinstance(authorized, IncomingCommand)


def test_telegram_renders_preformatted_text_and_native_keyboard() -> None:
    actions = (
        (CardAction("oa:yes", "Yes"), CardAction("oa:no", "No")),
        (CardAction("oa:refresh", "Refresh"),),
    )

    assert telegram_provider._keyboard(actions) == {
        "inline_keyboard": [
            [
                {"text": "Yes", "callback_data": "oa:yes"},
                {"text": "No", "callback_data": "oa:no"},
            ],
            [{"text": "Refresh", "callback_data": "oa:refresh"}],
        ]
    }
    assert telegram_provider._reply_chunks(BotReply("a < b & c", format="preformatted")) == [
        ("<pre>a &lt; b &amp; c</pre>", "HTML")
    ]


def test_telegram_preformatted_escaping_never_exceeds_wire_limit() -> None:
    source = "<&>" * 3000

    chunks = telegram_provider._reply_chunks(BotReply(source, format="preformatted"))

    assert len(chunks) > 1
    assert all(parse_mode == "HTML" for _text, parse_mode in chunks)
    assert all(len(text) <= 4096 for text, _parse_mode in chunks)
    escaped_body = "".join(text.removeprefix("<pre>").removesuffix("</pre>") for text, _ in chunks)
    assert html.unescape(escaped_body) == source


def test_telegram_callback_defer_satisfies_final_ack_without_duplicate_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_api_call(
        _token: str,
        method: str,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> object:
        calls.append((method, payload))
        return {"message_id": 99} if method == "sendMessage" else True

    monkeypatch.setattr(telegram_provider, "api_call", fake_api_call)
    messenger = telegram_provider.TelegramInteractiveMessenger(
        TelegramConfig(bot_token="token", chat_id="100")
    )
    action = IncomingAction(
        address=ConversationAddress("telegram", "100"),
        actor=Actor("7"),
        action_id="oa:one",
        ack_token="ack-1",
    )

    assert messenger.defer_action(action).sent
    assert messenger.acknowledge(action, "Finished.").sent

    assert [method for method, _payload in calls] == ["answerCallbackQuery"]
    assert calls[0][1]["text"] == "Processing…"


def test_telegram_none_poll_result_uses_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    methods: list[str] = []
    delays: list[float] = []

    def fake_api_call(
        _token: str,
        method: str,
        _payload: object,
        **_kwargs: object,
    ) -> object:
        methods.append(method)
        return {} if method == "setMyCommands" else None

    def stop_after_backoff(seconds: float) -> None:
        delays.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(telegram_provider, "api_call", fake_api_call)
    application = SimpleNamespace(dispatch_command=lambda *_args, **_kwargs: None)

    assert (
        telegram_provider.run_telegram_bot(
            application,  # type: ignore[arg-type]
            TelegramConfig(bot_token="secret-token", chat_id="100123"),
            sleep=stop_after_backoff,
        )
        == 0
    )
    assert methods == ["setMyCommands", "getUpdates"]
    assert delays == [2]
    assert "secret-token" not in caplog.text
    assert "100123" not in caplog.text


def test_telegram_dispatch_failure_does_not_starve_later_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = [
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 7},
                "text": "/list",
            },
        }
        for update_id in (10, 11)
    ]
    dispatched: list[str] = []

    def fake_api_call(
        _token: str,
        method: str,
        _payload: object,
        **_kwargs: object,
    ) -> object:
        return updates if method == "getUpdates" else {}

    class Application:
        def dispatch_command(self, incoming: IncomingCommand, *, messenger: object) -> None:
            del messenger
            dispatched.append(incoming.message_id or "")
            if incoming.message_id == "10":
                raise RuntimeError("poison update")
            raise KeyboardInterrupt

    monkeypatch.setattr(telegram_provider, "api_call", fake_api_call)

    assert (
        telegram_provider.run_telegram_bot(
            Application(),  # type: ignore[arg-type]
            TelegramConfig(bot_token="token", chat_id="100"),
        )
        == 0
    )
    assert dispatched == ["10", "11"]


def test_telegram_deferred_action_exception_sends_failure_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates = [
        {
            "update_id": 10,
            "callback_query": {
                "id": "ack-1",
                "data": "oa:one",
                "from": {"id": 7},
                "message": {"message_id": 20, "chat": {"id": 100, "type": "private"}},
            },
        },
        {
            "update_id": 11,
            "message": {
                "message_id": 21,
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 7},
                "text": "/help",
            },
        },
    ]
    calls: list[tuple[str, object]] = []

    def fake_api_call(
        _token: str,
        method: str,
        payload: object,
        **_kwargs: object,
    ) -> object:
        calls.append((method, payload))
        if method == "getUpdates":
            return updates
        if method == "sendMessage":
            return {"message_id": 99}
        return True

    class Application:
        def dispatch_action(self, _incoming: IncomingAction, *, messenger: object) -> None:
            del messenger
            raise RuntimeError("action failed")

        def dispatch_command(self, _incoming: IncomingCommand, *, messenger: object) -> None:
            del messenger
            raise KeyboardInterrupt

    monkeypatch.setattr(telegram_provider, "api_call", fake_api_call)

    assert (
        telegram_provider.run_telegram_bot(
            Application(),  # type: ignore[arg-type]
            TelegramConfig(bot_token="token", chat_id="100"),
        )
        == 0
    )
    send_payloads = [payload for method, payload in calls if method == "sendMessage"]
    assert len(send_payloads) == 1
    assert send_payloads[0]["text"] == "Action failed. Run the command again."  # type: ignore[index]


def test_discord_parses_commands_and_actions_without_sdk_types() -> None:
    message = SimpleNamespace(
        id=41,
        content="!list failed",
        channel=SimpleNamespace(id=100),
        author=SimpleNamespace(id=7, global_name="Ada"),
    )
    interaction = SimpleNamespace(
        id=42,
        channel_id=200,
        data={"custom_id": "oa:confirm"},
        user=SimpleNamespace(id=7, display_name="Ada"),
        message=SimpleNamespace(id=43),
    )

    assert discord_provider._command_from_message(message) == IncomingCommand(
        address=ConversationAddress("discord", "100"),
        actor=Actor("7", "Ada"),
        command="list",
        args="failed",
        message_id="41",
    )
    assert discord_provider._action_from_interaction(interaction) == IncomingAction(
        address=ConversationAddress("discord", "200"),
        actor=Actor("7", "Ada"),
        action_id="oa:confirm",
        ack_token="42",
        message_id="43",
    )
    assert (
        discord_provider._command_from_message(
            SimpleNamespace(content="hello", channel=SimpleNamespace(id=100))
        )
        is None
    )
    assert (
        discord_provider._action_from_interaction(
            SimpleNamespace(
                id=None,
                channel_id=200,
                data={"custom_id": "oa:confirm"},
                user=SimpleNamespace(id=7),
                message=SimpleNamespace(id=43),
            )
        )
        is None
    )


class _FakeView:
    def __init__(self, *, timeout: object) -> None:
        self.timeout = timeout
        self.children: list[Any] = []

    def add_item(self, item: object) -> None:
        self.children.append(item)


class _FakeButton:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _fake_discord_sdk(**extra: object) -> SimpleNamespace:
    allowed_mentions = object()

    class AllowedMentions:
        @staticmethod
        def none() -> object:
            return allowed_mentions

    return SimpleNamespace(
        ui=SimpleNamespace(View=_FakeView, Button=_FakeButton),
        ButtonStyle=SimpleNamespace(secondary=2),
        AllowedMentions=AllowedMentions,
        _allowed_mentions=allowed_mentions,
        **extra,
    )


@pytest.mark.parametrize(
    "actions",
    [
        tuple((CardAction(f"a-{index}", "A"),) for index in range(6)),
        ((*(CardAction(f"a-{index}", "A") for index in range(6)),),),
        ((CardAction("x" * 101, "A"),),),
        ((CardAction("id", "A" * 81),),),
    ],
)
def test_discord_component_limits_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    actions: object,
) -> None:
    monkeypatch.setitem(sys.modules, "discord", _fake_discord_sdk())

    with pytest.raises(ValueError):
        discord_provider._view(actions)  # type: ignore[arg-type]


def test_discord_reply_uses_no_mentions_and_attaches_buttons_to_last_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _fake_discord_sdk()
    monkeypatch.setitem(sys.modules, "discord", sdk)

    class Channel:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def send(self, content: str, **kwargs: Any) -> object:
            self.calls.append((content, kwargs))
            return SimpleNamespace(id=len(self.calls))

    channel = Channel()
    client = SimpleNamespace(get_channel=lambda _channel_id: channel)
    messenger = discord_provider.DiscordInteractiveMessenger(
        client=client,
        loop=None,
        config=DiscordConfig(bot_token="token", default_channel_id="100"),
    )
    monkeypatch.setattr(messenger, "_run", lambda coroutine: asyncio.run(coroutine))
    reply = BotReply(
        "@everyone\n" + "x" * 2100,
        actions=((CardAction("oa:one", "Run"),),),
    )

    result = messenger.send_reply(
        ConversationAddress("discord", "100"),
        reply,
        silent=True,
    )

    assert result.sent and result.message_ids == ("1", "2")
    assert len(channel.calls) == 2
    assert all(call[1]["allowed_mentions"] is sdk._allowed_mentions for call in channel.calls)
    assert all(call[1]["silent"] is True for call in channel.calls)
    assert channel.calls[0][1]["view"] is None
    assert isinstance(channel.calls[-1][1]["view"], _FakeView)


def test_discord_partial_multi_chunk_send_preserves_delivery_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "discord", _fake_discord_sdk())

    class Channel:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _content: str, **_kwargs: Any) -> object:
            self.calls += 1
            if self.calls == 2:
                raise OSError("connection lost")
            return SimpleNamespace(id=900 + self.calls)

    channel = Channel()
    messenger = discord_provider.DiscordInteractiveMessenger(
        client=SimpleNamespace(get_channel=lambda _channel_id: channel),
        loop=None,
        config=DiscordConfig(bot_token="token", default_channel_id="100"),
    )
    monkeypatch.setattr(messenger, "_run", lambda coroutine: asyncio.run(coroutine))

    result = messenger.send_reply(
        ConversationAddress("discord", "100"),
        BotReply("a" * 2100),
    )

    assert not result.sent
    assert result.partial
    assert result.error == "discord_partial_reply"
    assert result.message_id == "901"
    assert result.message_ids == ("901",)
    assert (result.sent_count, result.total_count) == (1, 2)


def test_discord_bridge_timeout_cancels_scheduled_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Future:
        def __init__(self) -> None:
            self.timeout: float | None = None
            self.cancelled = False

        def result(self, *, timeout: float) -> object:
            self.timeout = timeout
            raise TimeoutError

        def cancel(self) -> None:
            self.cancelled = True

    future = Future()
    monkeypatch.setattr(
        discord_provider.asyncio,
        "run_coroutine_threadsafe",
        lambda _coroutine, _loop: future,
    )
    messenger = discord_provider.DiscordInteractiveMessenger(
        client=None,
        loop=object(),
        config=DiscordConfig(bot_token="token", default_channel_id="100"),
    )

    with pytest.raises(TimeoutError):
        messenger._run(object())

    assert future.timeout == discord_provider._BRIDGE_TIMEOUT_SECONDS
    assert future.cancelled


def test_discord_reply_timeout_preserves_partial_delivery_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "discord", _fake_discord_sdk())

    class Channel:
        def __init__(self) -> None:
            self.calls = 0

        async def send(self, _content: str, **_kwargs: object) -> object:
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(id=901)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    channel = Channel()
    messenger = discord_provider.DiscordInteractiveMessenger(
        client=SimpleNamespace(get_channel=lambda _channel_id: channel),
        loop=None,
        config=DiscordConfig(bot_token="token", default_channel_id="100"),
    )
    monkeypatch.setattr(
        messenger,
        "_run",
        lambda coroutine: asyncio.run(asyncio.wait_for(coroutine, timeout=0.01)),
    )

    result = messenger.send_reply(
        ConversationAddress("discord", "100"),
        BotReply("a" * 2100),
    )

    assert not result.sent
    assert result.partial
    assert result.error == "discord_reply_timeout"
    assert result.message_id == "901"
    assert result.message_ids == ("901",)
    assert (result.sent_count, result.total_count) == (1, 2)


def test_discord_view_has_finite_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "discord", _fake_discord_sdk())

    view = discord_provider._view(((CardAction("oa:one", "Run"),),))

    assert isinstance(view, _FakeView)
    assert view.timeout == discord_provider._VIEW_TIMEOUT_SECONDS
    assert isinstance(view.timeout, int) and view.timeout > 0


class _InlineExecutor:
    def __init__(self, **_kwargs: object) -> None:
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> concurrent.futures.Future[Any]:
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_result(fn(*args, **kwargs))
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


def test_discord_channel_admission_is_single_flight_and_globally_bounded() -> None:
    admission = discord_provider._ChannelAdmission(max_pending=2)

    assert admission.acquire("100")
    assert not admission.acquire("100")
    assert admission.acquire("200")
    assert not admission.acquire("300")

    admission.release("100")
    assert admission.acquire("300")


def test_discord_upload_admission_is_nonblocking_and_bounded() -> None:
    admission = discord_provider._CounterAdmission(max_pending=2)

    assert admission.acquire()
    assert admission.acquire()
    assert not admission.acquire()

    admission.release()
    assert admission.acquire()


@pytest.mark.parametrize(
    "url",
    (
        "http://cdn.discordapp.com/attachments/1/2/job.zip",
        "https://example.com/job.zip",
        "https://cdn.discordapp.com.evil.test/job.zip",
        "https://user@cdn.discordapp.com/job.zip",
    ),
)
def test_discord_attachment_url_validation_fails_closed(url: str) -> None:
    with pytest.raises(discord_provider.AttachmentDownloadRejected):
        discord_provider._trusted_attachment_url(url)


def test_discord_attachment_download_streams_with_actual_byte_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"archive-bytes"

    class Response:
        status = 200
        headers = {"Content-Length": str(len(payload))}

        def __init__(self) -> None:
            self.remaining = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int) -> bytes:
            chunk, self.remaining = self.remaining[:amount], self.remaining[amount:]
            return chunk

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(discord_provider, "build_opener", lambda *_args: Opener())
    destination = tmp_path / "archive"

    written = discord_provider._download_attachment_url_bounded(
        "https://cdn.discordapp.com/attachments/1/2/job.zip",
        destination,
        max_bytes=len(payload),
        timeout_seconds=1,
    )

    assert written == len(payload)
    assert destination.read_bytes() == payload


def test_discord_attachment_download_removes_oversize_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"x" * 9

    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.remaining = payload

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self, amount: int) -> bytes:
            chunk, self.remaining = self.remaining[:amount], self.remaining[amount:]
            return chunk

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(discord_provider, "build_opener", lambda *_args: Opener())
    destination = tmp_path / "partial"

    with pytest.raises(discord_provider.AttachmentDownloadRejected, match="byte limit"):
        discord_provider._download_attachment_url_bounded(
            "https://media.discordapp.net/attachments/1/2/job.zip",
            destination,
            max_bytes=8,
            timeout_seconds=1,
        )

    assert not destination.exists()


def test_discord_attachment_download_does_not_delete_preexisting_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200
        headers = {"Content-Length": "1"}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

    class Opener:
        def open(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(discord_provider, "build_opener", lambda *_args: Opener())
    destination = tmp_path / "existing"
    destination.write_bytes(b"owned-by-another-operation")

    with pytest.raises(FileExistsError):
        discord_provider._download_attachment_url_bounded(
            "https://cdn.discordapp.com/attachments/1/2/job.zip",
            destination,
            max_bytes=8,
            timeout_seconds=1,
        )

    assert destination.read_bytes() == b"owned-by-another-operation"


def test_discord_download_cancellation_waits_for_executor_file_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    destination = tmp_path / "cancelled-download"

    def blocking_download(
        url: str,
        path: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> int:
        del url, max_bytes, timeout_seconds
        started.set()
        assert release.wait(timeout=2)
        path.write_bytes(b"x")
        return 1

    monkeypatch.setattr(
        discord_provider,
        "_download_attachment_url_bounded",
        blocking_download,
    )

    async def exercise() -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            attachment = SimpleNamespace(url="https://cdn.discordapp.com/attachments/1/2/job.zip")
            task = asyncio.create_task(
                discord_provider._download_attachment_bounded(
                    attachment,
                    destination,
                    max_bytes=8,
                    timeout_seconds=1,
                    executor=executor,
                )
            )
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert destination.read_bytes() == b"x"


def test_discord_gateway_reserves_before_download_without_redispatching_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    uploads: list[IncomingUpload] = []
    archive_path = tmp_path / "archive"
    session = SimpleNamespace(
        upload_id="upl_test",
        archive_path=archive_path,
        actual_bytes=None,
        state=UploadState.RECEIVING,
    )
    reserve_count = 0

    class Application:
        def reserve_upload(self, **kwargs: object) -> object:
            nonlocal reserve_count
            events.append("reserve")
            assert kwargs["message_id"] == "501"
            assert kwargs["attachment_ids"] == ("601",)
            assert kwargs["expected_bytes"] == 7
            reserve_count += 1
            return SimpleNamespace(session=session, created=reserve_count == 1)

        def finalize_upload(self, upload_id: str) -> object:
            events.append("finalize")
            assert upload_id == "upl_test"
            session.actual_bytes = archive_path.stat().st_size
            return session

        def dispatch_upload(self, incoming: IncomingUpload, *, messenger: object) -> None:
            del messenger
            events.append("dispatch")
            uploads.append(incoming)
            session.state = UploadState.AWAITING_CONFIRM

        def abandon_upload(self, upload_id: str, reason: str) -> None:
            raise AssertionError(f"unexpected abandon: {upload_id}: {reason}")

        def sweep_upload_sessions(self) -> None:
            events.append("sweep")

    async def bounded_download(
        attachment: object,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
        executor: object,
    ) -> int:
        del attachment, timeout_seconds, executor
        events.append("download")
        assert max_bytes == 64
        destination.write_bytes(b"archive")
        return 7

    class Channel:
        id = 100

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str, **_kwargs: object) -> object:
            self.sent.append(text)
            return SimpleNamespace(id=len(self.sent))

    channel = Channel()
    attachment = SimpleNamespace(
        id=601,
        filename="job.zip",
        size=7,
        url="https://cdn.discordapp.com/attachments/1/2/job.zip",
    )
    message = SimpleNamespace(
        id=501,
        content="!run",
        channel=channel,
        author=SimpleNamespace(id=7, name="operator", bot=False),
        attachments=[attachment],
    )

    class Intents:
        @staticmethod
        def default() -> object:
            return SimpleNamespace(message_content=False)

    class Client:
        def __init__(self, *, intents: object) -> None:
            self.intents = intents
            self.events: dict[str, Any] = {}
            self.user = "orca-test"

        def event(self, fn: Any) -> Any:
            self.events[fn.__name__] = fn
            return fn

        def run(self, _token: str, *, log_handler: object) -> None:
            del log_handler

            async def drive() -> None:
                await self.events["on_ready"]()
                await self.events["on_message"](message)
                await self.events["on_message"](message)

            asyncio.run(drive())

        def get_channel(self, _channel_id: int) -> Channel:
            return channel

    sdk = _fake_discord_sdk(
        Intents=Intents,
        Client=Client,
        InteractionType=SimpleNamespace(component=object()),
    )
    monkeypatch.setitem(sys.modules, "discord", sdk)
    monkeypatch.setattr(discord_provider, "ThreadPoolExecutor", _InlineExecutor)
    monkeypatch.setattr(discord_provider, "_download_attachment_bounded", bounded_download)
    config = DiscordConfig(
        bot_token="token",
        channel_ids=("100",),
        allowed_user_ids=("7",),
        uploads=UploadPolicy(
            enabled=True,
            max_archive_bytes=64,
            max_staged_bytes=64,
        ),
    )

    assert discord_provider.run_discord_bot(Application(), config) == 0  # type: ignore[arg-type]

    assert events == ["reserve", "download", "finalize", "dispatch", "reserve"]
    assert len(uploads) == 1
    assert uploads[0].upload_id == "upl_test"
    assert uploads[0].attachment_id == "601"
    assert uploads[0].archive_path == str(archive_path)
    assert channel.sent == [
        "This upload is already awaiting confirmation; use the original buttons."
    ]


def test_discord_gateway_enforces_bot_user_and_channel_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[IncomingCommand] = []
    actions: list[IncomingAction] = []
    interactions: dict[str, Any] = {}
    component_type = object()

    class Application:
        def dispatch_command(self, incoming: IncomingCommand, *, messenger: object) -> None:
            del messenger
            commands.append(incoming)

        def dispatch_action(self, incoming: IncomingAction, *, messenger: object) -> None:
            del messenger
            actions.append(incoming)

    class Response:
        def __init__(self) -> None:
            self.deferred = False

        async def defer(self, **_kwargs: object) -> None:
            self.deferred = True

    class Followup:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, text: str, **_kwargs: object) -> None:
            self.messages.append(text)

    def message(channel_id: int, user_id: int, *, bot: bool = False) -> object:
        return SimpleNamespace(
            id=1000 + channel_id + user_id,
            content="/list",
            channel=SimpleNamespace(id=channel_id),
            author=SimpleNamespace(id=user_id, name=str(user_id), bot=bot),
        )

    def interaction(name: str, channel_id: int, user_id: int) -> object:
        value = SimpleNamespace(
            id=name,
            type=component_type,
            channel_id=channel_id,
            data={"custom_id": "oa:one"},
            user=SimpleNamespace(id=user_id, name=str(user_id)),
            message=SimpleNamespace(id=500),
            response=Response(),
            followup=Followup(),
        )
        interactions[name] = value
        return value

    class Intents:
        @staticmethod
        def default() -> object:
            return SimpleNamespace(message_content=False)

    class Client:
        def __init__(self, *, intents: object) -> None:
            self.intents = intents
            self.events: dict[str, Any] = {}
            self.user = "orca-test"

        def event(self, fn: Any) -> Any:
            self.events[fn.__name__] = fn
            return fn

        def run(self, token: str, *, log_handler: object) -> None:
            assert token == "token"
            assert log_handler is None

            async def drive() -> None:
                await self.events["on_ready"]()
                await self.events["on_message"](message(100, 7, bot=True))
                await self.events["on_message"](message(999, 7))
                await self.events["on_message"](message(100, 8))
                await self.events["on_message"](message(200, 7))
                await self.events["on_message"](message(100, 7))
                await self.events["on_interaction"](interaction("wrong-channel", 999, 7))
                await self.events["on_interaction"](interaction("wrong-user", 200, 8))
                await self.events["on_interaction"](interaction("allowed", 200, 7))
                await asyncio.sleep(0)

            asyncio.run(drive())

        def get_channel(self, _channel_id: int) -> None:
            return None

        async def fetch_channel(self, _channel_id: int) -> None:
            return None

    sdk = _fake_discord_sdk(
        Intents=Intents,
        Client=Client,
        InteractionType=SimpleNamespace(component=component_type),
    )
    monkeypatch.setitem(sys.modules, "discord", sdk)
    monkeypatch.setattr(discord_provider, "ThreadPoolExecutor", _InlineExecutor)
    config = DiscordConfig(
        bot_token="token",
        channel_ids=("100",),
        default_channel_id="200",
        allowed_user_ids=("7",),
    )

    assert discord_provider.run_discord_bot(Application(), config) == 0  # type: ignore[arg-type]

    assert [(item.address.channel_id, item.actor.user_id) for item in commands] == [("100", "7")]
    assert [(item.address.channel_id, item.actor.user_id) for item in actions] == [("200", "7")]
    assert interactions["wrong-channel"].response.deferred is True
    assert interactions["wrong-channel"].followup.messages == [
        "This channel is no longer authorized for orca_auto controls."
    ]
    assert interactions["wrong-user"].response.deferred is True
    assert interactions["wrong-user"].followup.messages == [
        "Not authorized for orca_auto controls."
    ]
    assert interactions["allowed"].response.deferred is True


def test_discord_reconnect_keeps_the_same_interaction_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messengers: list[object] = []

    class Application:
        def dispatch_command(self, _incoming: IncomingCommand, *, messenger: object) -> None:
            messengers.append(messenger)

    class Intents:
        @staticmethod
        def default() -> object:
            return SimpleNamespace(message_content=False)

    class Client:
        def __init__(self, *, intents: object) -> None:
            self.intents = intents
            self.events: dict[str, Any] = {}
            self.user = "orca-test"

        def event(self, fn: Any) -> Any:
            self.events[fn.__name__] = fn
            return fn

        def run(self, _token: str, *, log_handler: object) -> None:
            del log_handler

            async def drive() -> None:
                message = SimpleNamespace(
                    id=1,
                    content="!help",
                    channel=SimpleNamespace(id=100),
                    author=SimpleNamespace(id=7, name="operator", bot=False),
                )
                await self.events["on_ready"]()
                await self.events["on_message"](message)
                await self.events["on_ready"]()
                await self.events["on_message"](message)

            asyncio.run(drive())

    sdk = _fake_discord_sdk(
        Intents=Intents,
        Client=Client,
        InteractionType=SimpleNamespace(component=object()),
    )
    monkeypatch.setitem(sys.modules, "discord", sdk)
    monkeypatch.setattr(discord_provider, "ThreadPoolExecutor", _InlineExecutor)

    config = DiscordConfig(
        bot_token="token",
        channel_ids=("100",),
        allowed_user_ids=("7",),
    )
    assert discord_provider.run_discord_bot(Application(), config) == 0  # type: ignore[arg-type]
    assert len(messengers) == 2
    assert messengers[0] is messengers[1]


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [
        ("discord", None, "discord"),
        ("discord", "telegram", "telegram"),
        ("telegram", "discord", "discord"),
    ],
)
def test_runner_selects_configured_or_explicit_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    configured: str,
    override: str | None,
    expected: str,
) -> None:
    calls: list[tuple[str, object]] = []
    config = MessengerConfig(
        provider=configured,
        telegram=TelegramConfig(bot_token="telegram-token", chat_id="-100"),
        discord=DiscordConfig(
            bot_token="discord-token",
            channel_ids=("100",),
            allowed_user_ids=("7",),
        ),
    )
    settings = BotSettings(None, None, None, None, None, runs_root=str(tmp_path))
    monkeypatch.setattr(
        runner,
        "load_required_messenger_config_from_file",
        lambda _path: config,
    )
    monkeypatch.setattr(runner, "settings_from_config", lambda _path: settings)

    def run_telegram(_application: object, adapter: object) -> int:
        calls.append(("telegram", adapter))
        return 11

    def run_discord(_application: BotApplication, adapter: object) -> int:
        assert _application.upload_policy == config.discord.uploads
        assert _application.upload_sessions is not None
        calls.append(("discord", adapter))
        return 12

    monkeypatch.setattr(telegram_provider, "run_telegram_bot", run_telegram)
    monkeypatch.setattr(discord_provider, "run_discord_bot", run_discord)

    result = runner.run_bot(config_path="/config.yaml", provider=override)

    assert result == {"telegram": 11, "discord": 12}[expected]
    assert calls == [
        (
            expected,
            config.telegram if expected == "telegram" else config.discord,
        )
    ]


def test_runner_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MessengerConfig()
    monkeypatch.setattr(
        runner,
        "load_required_messenger_config_from_file",
        lambda _path: config,
    )
    monkeypatch.setattr(
        runner,
        "settings_from_config",
        lambda _path: BotSettings(None, None, None, None, None),
    )

    with pytest.raises(ValueError, match="unsupported messenger provider"):
        runner.run_bot(provider="matrix")


def test_provider_api_call_constructs_transport_client(monkeypatch) -> None:
    # The provider-local wrapper owns the transport wiring since the legacy
    # bot_api facade was retired: token, poll-timeout math, and logger must
    # reach the core client unchanged.
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, token: str, timeout: int, logger: object) -> None:
            captured["token"] = token
            captured["timeout"] = timeout
            captured["logger"] = logger

        def api_call(
            self, method: str, payload: dict | None = None, *, timeout: int | None = None
        ) -> dict:
            captured["method"] = method
            captured["payload"] = payload
            captured["call_timeout"] = timeout
            return {"ok": True}

    monkeypatch.setattr(telegram_provider, "TelegramApiClient", FakeClient)

    result = telegram_provider.api_call("tok", "getMe", {"a": 1})

    assert result == {"ok": True}
    assert captured["token"] == "tok"
    assert captured["timeout"] == telegram_provider.POLL_TIMEOUT_SECONDS + 5
    assert captured["call_timeout"] == telegram_provider.POLL_TIMEOUT_SECONDS + 5
    assert captured["logger"] is telegram_provider.LOGGER
    assert captured["method"] == "getMe"
    assert captured["payload"] == {"a": 1}
