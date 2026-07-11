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
    DiscordWebhookChannel,
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
    result = DiscordBotChannel(_bot_config()).send(Message(title="@everyone"), silent=True)

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
    assert payload["flags"] == 1 << 12
    assert captured["timeout"] == 5.0


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (_FakeResponse(204, b""), "discord_unconfirmed_delivery"),
        (_FakeResponse(200, b"not-json"), "discord_invalid_response"),
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


def test_registry_prefers_complete_bot_rest_then_legacy_webhook() -> None:
    complete = MessengerConfig(
        provider="discord",
        discord=_bot_config(webhook_url="https://discord.com/api/webhooks/123/secret"),
    )
    assert isinstance(build_channel(complete), DiscordBotChannel)

    webhook_only = MessengerConfig(
        provider="discord",
        discord=DiscordConfig(webhook_url="https://discord.com/api/webhooks/123/secret"),
    )
    assert isinstance(build_channel(webhook_only), DiscordWebhookChannel)

    incomplete_bot = MessengerConfig(
        provider="discord",
        discord=DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/secret",
            bot_token="token",
        ),
    )
    assert isinstance(build_channel(incomplete_bot), DiscordWebhookChannel)
