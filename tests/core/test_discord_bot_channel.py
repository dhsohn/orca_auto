"""Focused tests for Discord bot-token REST notification delivery."""

from __future__ import annotations

import json
from email.message import Message as EmailMessage
from io import BytesIO
from typing import Literal
from urllib.error import HTTPError

import pytest

from orca_auto.core.config import DiscordConfig, MessengerConfig
from orca_auto.core.messaging import (
    DiscordBotChannel,
    Message,
    build_channel,
)
from orca_auto.core.messaging import discord_bot as bot_mod


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"id":"999"}') -> None:
        self.status = status
        self.body = body

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False


def _bot_config(**overrides: object) -> DiscordConfig:
    values: dict[str, object] = {
        "bot_token": "secret-token",
        "default_channel_id": "123",
    }
    values.update(overrides)
    return DiscordConfig(**values)  # type: ignore[arg-type]


def test_discord_bot_channel_requires_complete_bot_destination() -> None:
    result = DiscordBotChannel(DiscordConfig(bot_token="token")).send(Message(title="T"))

    assert result.skipped
    assert not result.sent
    assert result.error == "discord_bot_disabled"


def test_discord_bot_channel_posts_confirmed_embed_without_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    result = DiscordBotChannel(_bot_config()).send(Message(title="@everyone"))

    assert result.sent
    assert result.message_id == "999"
    request = captured["request"]
    assert request.full_url == "https://discord.com/api/v10/channels/123/messages"  # type: ignore[attr-defined]
    assert request.get_header("Authorization") == "Bot secret-token"  # type: ignore[attr-defined]
    payload = json.loads(request.data)  # type: ignore[attr-defined]
    assert payload["embeds"] == [{"title": "@everyone", "color": 0x3498DB}]
    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["nonce"]) == 24
    assert payload["enforce_nonce"] is True
    assert "flags" not in payload
    assert captured["timeout"] == 5.0


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (_FakeResponse(204, b""), "discord_unconfirmed_delivery"),
        (_FakeResponse(200, b"not-json"), "discord_invalid_response"),
        (_FakeResponse(200, b'{"content":"missing id"}'), "discord_invalid_response"),
        (_FakeResponse(200, b'{"id":"not-a-snowflake"}'), "discord_invalid_response"),
        (_FakeResponse(200, b'{"id":"0"}'), "discord_invalid_response"),
    ],
)
def test_discord_bot_channel_requires_confirmed_message_id(
    monkeypatch: pytest.MonkeyPatch,
    response: _FakeResponse,
    expected_error: str,
) -> None:
    monkeypatch.setattr(bot_mod, "urlopen", lambda *_args, **_kwargs: response)

    result = DiscordBotChannel(_bot_config()).send(Message(title="T"))

    assert not result.sent
    assert result.error == expected_error


def test_discord_bot_channel_retries_with_bounded_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = EmailMessage()
    headers["Retry-After"] = "0.25"
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError(
            "https://discord.com/api/v10/channels/123/messages",
            429,
            "rate",
            headers,
            BytesIO(b'{"retry_after": 9}'),
        ),
        _FakeResponse(),
    ]
    delays: list[float] = []
    request_bodies: list[bytes] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        del timeout
        request_bodies.append(request.data)  # type: ignore[attr-defined]
        response = sequence.pop(0)
        if isinstance(response, HTTPError):
            raise response
        return response

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    channel = DiscordBotChannel(
        _bot_config(max_attempts=2, retry_backoff_seconds=0.0),
        sleeper=delays.append,
    )

    assert channel.send(Message(title="T")).sent
    assert delays == [0.25]
    assert request_bodies[0] == request_bodies[1]
    assert sequence == []


def test_discord_bot_channel_errors_do_not_expose_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot_mod,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPError(
                "https://discord.com/api/v10/channels/123/messages",
                401,
                "unauthorized",
                EmailMessage(),
                None,
            )
        ),
    )

    result = DiscordBotChannel(_bot_config()).send(Message(title="T"))

    assert not result.sent
    assert result.error == "discord_http_401"
    assert "secret-token" not in result.error


def test_registry_always_builds_discord_bot_channel() -> None:
    complete = MessengerConfig(provider="discord", discord=_bot_config())
    assert isinstance(build_channel(complete), DiscordBotChannel)

    # An incomplete bot config still resolves to the bot channel; it fails closed
    # at send time rather than routing through another transport.
    incomplete_bot = MessengerConfig(
        provider="discord",
        discord=DiscordConfig(bot_token="token"),
    )
    assert isinstance(build_channel(incomplete_bot), DiscordBotChannel)


# --------------------------------------------------------------------------- #
# Shared HTTP retry/backoff behavior (discord_http helpers, via the bot sender)
# --------------------------------------------------------------------------- #
def test_discord_bot_channel_uses_retry_after_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError(
            "https://discord.com/api/v10/channels/123/messages",
            429,
            "rate",
            EmailMessage(),
            BytesIO(b'{"retry_after": 0.125}'),
        ),
        _FakeResponse(),
    ]
    delays: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        del request, timeout
        item = sequence.pop(0)
        if isinstance(item, HTTPError):
            raise item
        return item

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    channel = DiscordBotChannel(_bot_config(max_attempts=2), sleeper=delays.append)

    assert channel.send(Message(title="T")).sent
    assert delays == [0.125]


def test_discord_bot_channel_allows_documented_global_retry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = EmailMessage()
    headers["Retry-After"] = "65"
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError(
            "https://discord.com/api/v10/channels/123/messages",
            429,
            "global rate limit",
            headers,
            BytesIO(b'{"retry_after":65,"global":true}'),
        ),
        _FakeResponse(),
    ]
    delays: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        del request, timeout
        item = sequence.pop(0)
        if isinstance(item, HTTPError):
            raise item
        return item

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    channel = DiscordBotChannel(_bot_config(max_attempts=2), sleeper=delays.append)

    assert channel.send(Message(title="T")).sent
    assert delays == [65.0]


def test_discord_bot_channel_rejects_excessive_retry_after_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = EmailMessage()
    headers["Retry-After"] = "1337"
    calls = 0

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        nonlocal calls
        del request, timeout
        calls += 1
        raise HTTPError(
            "https://discord.com/api/v10/channels/123/messages",
            429,
            "rate",
            headers,
            BytesIO(b'{"retry_after": 0.1}'),
        )

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    channel = DiscordBotChannel(
        _bot_config(max_attempts=2),
        sleeper=lambda _delay: pytest.fail("must not sleep"),
    )

    result = channel.send(Message(title="T"))

    assert calls == 1
    assert not result.sent
    assert result.error == "discord_retry_after_exceeds_limit"
    assert "secret-token" not in result.error


def test_discord_bot_channel_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        del request, timeout
        raise HTTPError(
            "https://discord.com/api/v10/channels/123/messages",
            500,
            "err",
            EmailMessage(),
            None,
        )

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    monkeypatch.setattr(bot_mod.time, "sleep", lambda _seconds: None)
    channel = DiscordBotChannel(_bot_config(max_attempts=2, retry_backoff_seconds=0.0))

    result = channel.send(Message(title="T"))

    assert not result.sent
    assert result.error == "discord_http_500"
    assert "secret-token" not in result.error


def test_discord_bot_channel_defensively_caps_attempts_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        del request
        timeouts.append(timeout)
        raise HTTPError(
            "https://discord.com/api/v10/channels/123/messages",
            500,
            "err",
            EmailMessage(),
            None,
        )

    monkeypatch.setattr(bot_mod, "urlopen", fake_urlopen)
    channel = DiscordBotChannel(
        _bot_config(
            timeout_seconds=float("inf"),
            max_attempts=1_000_000_000,
            retry_backoff_seconds=0.0,
        ),
        sleeper=lambda _delay: None,
    )

    result = channel.send(Message(title="T"))

    assert not result.sent
    assert result.error == "discord_http_500"
    assert timeouts == [5.0] * 10
