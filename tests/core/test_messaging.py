"""Tests for the messenger-neutral notification layer (core/messaging)."""

from __future__ import annotations

import json
from email.message import Message as EmailMessage
from typing import Literal
from urllib.error import HTTPError

import pytest

from orca_auto.core.config import (
    DiscordConfig,
    MessengerConfig,
    TelegramConfig,
    messenger_config_from_mapping,
)
from orca_auto.core.messaging import (
    DiscordWebhookChannel,
    Message,
    TelegramChannel,
    bold,
    build_channel,
    code,
    field_row,
    group,
    line,
    raw,
    render_discord_embed,
    render_telegram,
    text,
    title_heading,
)
from orca_auto.core.messaging import discord_webhook as discord_mod
from orca_auto.core.messaging import telegram_channel as telegram_mod


# --------------------------------------------------------------------------- #
# Telegram rendering (byte-level)
# --------------------------------------------------------------------------- #
def test_render_telegram_reproduces_label_value_html() -> None:
    message = Message(
        title="orca_auto ORCA Started",
        groups=(
            group(
                field_row("Job", text("rxn/step1")),
                field_row("Attempt", raw("#3 ("), code("running"), raw(")")),
                field_row("Input", code("job.inp")),
                heading=title_heading("orca_auto ORCA Started"),
            ),
        ),
    )
    assert render_telegram(message) == (
        "<b>orca_auto ORCA Started</b>\n"
        "<b>Job</b>: rxn/step1\n"
        "<b>Attempt</b>: #3 (<code>running</code>)\n"
        "<b>Input</b>: <code>job.inp</code>"
    )


def test_render_telegram_escapes_html_special_chars() -> None:
    message = Message(
        title="t",
        groups=(group(field_row("K", text("a<b>&c")), heading=title_heading("t")),),
    )
    assert render_telegram(message) == "<b>t</b>\n<b>K</b>: a&lt;b&gt;&amp;c"


def test_render_telegram_joins_groups_with_blank_line() -> None:
    message = Message(
        title="T",
        groups=(
            group(field_row("A", text("1")), heading=title_heading("T")),
            group(line(raw("second paragraph")), heading=(bold("Section"),)),
        ),
    )
    assert render_telegram(message) == ("<b>T</b>\n<b>A</b>: 1\n\n<b>Section</b>\nsecond paragraph")


def test_raw_preserves_leading_whitespace_but_text_strips() -> None:
    message = Message(
        title="t",
        groups=(group(line(raw("   indented"), text("  padded  ")), heading=()),),
    )
    # raw keeps the leading spaces; text() strips its value like escape_html did.
    assert render_telegram(message) == "   indentedpadded"


# --------------------------------------------------------------------------- #
# Discord rendering (embed)
# --------------------------------------------------------------------------- #
def test_render_discord_embed_dedups_title_and_maps_fields() -> None:
    message = Message(
        title="ORCA Started",
        severity="success",
        groups=(
            group(
                field_row("Job", text("rxn")),
                field_row("Attempt", raw("#3")),
                heading=title_heading("ORCA Started"),
            ),
        ),
    )
    embed = render_discord_embed(message)
    assert embed["title"] == "ORCA Started"
    assert embed["color"] == 0x2ECC71
    assert embed["fields"] == [
        {"name": "Job", "value": "rxn", "inline": False},
        {"name": "Attempt", "value": "#3", "inline": False},
    ]
    assert "description" not in embed


def test_render_discord_embed_routes_lines_and_headings_to_description() -> None:
    message = Message(
        title="T",
        groups=(
            group(heading=title_heading("T")),
            group(line(raw("hello "), code("world")), heading=(bold("Section"),)),
        ),
    )
    embed = render_discord_embed(message)
    assert embed["description"] == "**Section**\nhello `world`"
    assert "fields" not in embed


# --------------------------------------------------------------------------- #
# Telegram channel
# --------------------------------------------------------------------------- #
def test_telegram_channel_disabled_is_skipped() -> None:
    result = TelegramChannel(TelegramConfig()).send(Message(title="x"))
    assert result.skipped and not result.sent


def test_telegram_channel_sends_rendered_html(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(config: object, body: str, **kwargs: object) -> bool:
        captured["body"] = body
        captured["parse_mode"] = kwargs.get("parse_mode")
        return True

    monkeypatch.setattr(telegram_mod, "send_telegram_message", fake_send)
    channel = TelegramChannel(TelegramConfig(bot_token="1:A", chat_id="9"))
    result = channel.send(
        Message(title="Hi", groups=(group(field_row("K", text("v")), heading=title_heading("Hi")),))
    )
    assert result.sent
    assert captured["body"] == "<b>Hi</b>\n<b>K</b>: v"
    assert captured["parse_mode"] == "HTML"


# --------------------------------------------------------------------------- #
# Discord webhook channel
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return b""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False


def test_discord_channel_disabled_is_skipped() -> None:
    result = DiscordWebhookChannel(DiscordConfig()).send(Message(title="x"))
    assert result.skipped and not result.sent


def test_discord_channel_posts_embed_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(request)
        return _FakeResponse(204)

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(DiscordConfig(webhook_url="https://discord.test/webhook/abc"))
    result = channel.send(
        Message(title="T", groups=(group(field_row("K", text("v")), heading=title_heading("T")),))
    )
    assert result.sent
    assert len(calls) == 1
    payload = json.loads(calls[0].data)  # type: ignore[attr-defined]
    assert payload["embeds"][0]["title"] == "T"
    assert payload["embeds"][0]["fields"] == [{"name": "K", "value": "v", "inline": False}]


def test_discord_channel_retries_on_http_429(monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError("u", 429, "rate", EmailMessage(), None),
        _FakeResponse(204),
    ]

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        item = sequence.pop(0)
        if isinstance(item, HTTPError):
            raise item
        return item

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    monkeypatch.setattr(discord_mod.time, "sleep", lambda _seconds: None)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.test/x", max_attempts=2, retry_backoff_seconds=0.0
        )
    )
    result = channel.send(Message(title="T"))
    assert result.sent
    assert sequence == []


def test_discord_channel_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        raise HTTPError("u", 500, "err", EmailMessage(), None)

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    monkeypatch.setattr(discord_mod.time, "sleep", lambda _seconds: None)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.test/x", max_attempts=2, retry_backoff_seconds=0.0
        )
    )
    result = channel.send(Message(title="T"))
    assert not result.sent
    assert "discord_http_error" in result.error


# --------------------------------------------------------------------------- #
# Registry / config
# --------------------------------------------------------------------------- #
def test_build_channel_selects_provider() -> None:
    telegram = TelegramConfig(bot_token="1:A", chat_id="9")
    assert isinstance(
        build_channel(MessengerConfig(provider="telegram"), telegram), TelegramChannel
    )
    discord = build_channel(
        MessengerConfig(provider="discord", discord=DiscordConfig(webhook_url="https://x")),
        telegram,
    )
    assert isinstance(discord, DiscordWebhookChannel)
    # Unknown provider degrades to Telegram rather than dropping notifications.
    assert isinstance(build_channel(MessengerConfig(provider="bogus"), telegram), TelegramChannel)


def test_messenger_config_from_mapping() -> None:
    cfg = messenger_config_from_mapping(
        {"provider": "Discord", "discord": {"webhook_url": "https://x"}}
    )
    assert cfg.normalized_provider == "discord"
    assert cfg.discord.webhook_url == "https://x"
    assert cfg.discord.enabled

    empty = messenger_config_from_mapping(None)
    assert empty.normalized_provider == "telegram"
    assert not empty.discord.enabled
