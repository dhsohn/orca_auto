"""Tests for the messenger-neutral notification layer (core/messaging)."""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
import urllib.request
from typing import Literal, Self

import pytest

from orca_auto.core.config import (
    DiscordConfig,
    MessengerConfig,
    messenger_config_from_mapping,
)
from orca_auto.core.messaging import (
    DisabledChannel,
    Message,
    Severity,
    build_channel,
    code,
    field_row,
    group,
    raw,
    render_discord_embed,
    text,
)
from orca_auto.core.messaging.discord_bot import DiscordBotChannel

# Obviously synthetic values: no real Slack workspace, token or channel.
_SLACK_TOKEN = "xoxb-synthetic-test-token"
_SLACK_CHANNEL = "C0SYNTHETIC1"


def _refuse_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a messenger test must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


class _FakeSlackResponse:
    status = 200
    headers = {"Content-Type": "application/json; charset=utf-8"}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def getcode(self) -> int:
        return self.status

    def read(self, *_args: object) -> bytes:
        return self._body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> Literal[False]:
        return False


def test_neutral_messaging_import_does_not_eagerly_load_adapters() -> None:
    code_under_test = """
import sys
import orca_auto.core.messaging
blocked = [
    name for name in ('orca_auto.core.messaging.discord_bot',)
    if name in sys.modules
]
if blocked:
    raise SystemExit(','.join(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code_under_test],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


# --------------------------------------------------------------------------- #
# Discord rendering (embed)
# --------------------------------------------------------------------------- #
def test_render_discord_embed_maps_fields() -> None:
    message = Message(
        title="ORCA Started",
        severity="success",
        groups=(group(field_row("Job", text("rxn")), field_row("Attempt", raw("#3"))),),
    )
    embed = render_discord_embed(message)
    assert embed["title"] == "✅ ORCA Started"
    assert embed["color"] == 0x2ECC71
    assert embed["fields"] == [
        {"name": "Job", "value": "rxn", "inline": False},
        {"name": "Attempt", "value": r"\#3", "inline": False},
    ]
    assert "description" not in embed
    assert "author" not in embed


def test_render_discord_embed_escapes_markdown_and_embedded_backticks() -> None:
    message = Message(
        title="*literal*",
        groups=(group(field_row("[key]", text("@everyone **not bold**"), code("a`b"))),),
    )
    embed = render_discord_embed(message)
    # The embed title is literal text on Discord (no Markdown), so it is not
    # escaped; field values do parse Markdown and stay escaped.
    assert embed["title"] == "*literal*"
    assert embed["fields"] == [
        {
            "name": "[key]",
            "value": r"@everyone \*\*not bold\*\*`` a`b ``",
            "inline": False,
        }
    ]


def test_render_discord_embed_enforces_aggregate_budget_and_marks_omissions() -> None:
    message = Message(
        title="T" * 256,
        groups=(group(*(field_row(f"field-{index}", text("V" * 1024)) for index in range(25))),),
    )
    embed = render_discord_embed(message)
    total = len(embed["title"]) + sum(
        len(item["name"]) + len(item["value"]) for item in embed.get("fields", [])
    )
    assert total <= 6000
    assert "description" not in embed
    # The budget must preserve useful content, not merely emit an omission marker.
    assert embed["fields"][:5] == [
        {"name": f"field-{index}", "value": "V" * 1024, "inline": False} for index in range(5)
    ]
    assert len(embed["fields"]) == 7
    truncated = embed["fields"][5]
    assert truncated["name"] == "field-5"
    assert truncated["value"].startswith("V")
    assert truncated["value"].endswith("…")
    assert embed["fields"][-1] == {"name": "More", "value": "…", "inline": False}


def test_render_discord_embed_prefixes_title_emoji_by_severity() -> None:
    def title_for(severity: Severity) -> str:
        return render_discord_embed(Message(title="Job", severity=severity))["title"]

    assert title_for("success") == "✅ Job"
    assert title_for("warning") == "⚠️ Job"
    assert title_for("error") == "❌ Job"
    # info stays bare so routine, non-outcome events read quietly.
    assert title_for("info") == "Job"


def test_render_discord_embed_renders_author_when_present() -> None:
    embed = render_discord_embed(Message(title="Stage completed", author="orca_auto"))
    assert embed["author"] == {"name": "orca_auto"}


def test_render_discord_embed_marks_inline_fields() -> None:
    message = Message(
        title="T",
        groups=(
            group(
                field_row("Workflow", text("wf1"), inline=True),
                field_row("Reason", text("boom")),
            ),
        ),
    )
    fields = render_discord_embed(message)["fields"]
    assert fields[0] == {"name": "Workflow", "value": "wf1", "inline": True}
    assert fields[1] == {"name": "Reason", "value": "boom", "inline": False}


# --------------------------------------------------------------------------- #
# Channel resolution / config
# --------------------------------------------------------------------------- #
def test_build_channel_returns_discord_bot_when_config_complete() -> None:
    discord = build_channel(
        MessengerConfig(discord=DiscordConfig(bot_token="token", default_channel_id="123"))
    )
    assert isinstance(discord, DiscordBotChannel)
    assert discord.enabled


@pytest.mark.parametrize(
    "messenger",
    [
        MessengerConfig(),
        MessengerConfig(discord=DiscordConfig(bot_token="token")),
        MessengerConfig(discord=DiscordConfig(default_channel_id="123")),
    ],
)
def test_build_channel_returns_null_channel_when_config_incomplete(
    messenger: MessengerConfig,
) -> None:
    channel = build_channel(messenger)
    assert isinstance(channel, DisabledChannel)
    assert not channel.enabled
    result = channel.send(Message(title="T"))
    assert (result.sent, result.skipped, result.error) == (False, True, "messenger_disabled")


def test_configured_slack_channel_posts_one_message_to_chat_post_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _refuse_network(monkeypatch)
    requests: list[tuple[urllib.request.Request, object]] = []

    def fake_open(
        _opener: object,
        request: urllib.request.Request,
        data: object = None,
        timeout: object = None,
    ) -> _FakeSlackResponse:
        requests.append((request, timeout))
        return _FakeSlackResponse(
            b'{"ok": true, "channel": "C0SYNTHETIC1", "ts": "1700000000.000100"}'
        )

    # Every urlopen call, whichever module imported it, goes through an opener.
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)

    config = messenger_config_from_mapping(
        {
            "provider": "slack",
            "slack": {
                "bot_token": _SLACK_TOKEN,
                "default_channel_id": _SLACK_CHANNEL,
                "timeout_seconds": 3,
                "max_attempts": 1,
            },
        }
    )
    assert config.provider == "slack"
    assert config.enabled
    channel = build_channel(config)
    assert channel.enabled
    assert not isinstance(channel, DisabledChannel | DiscordBotChannel)

    message = Message(
        title="ORCA completed",
        severity="success",
        groups=(group(field_row("Job", text("water-opt"))),),
        author="orca_auto",
    )
    with caplog.at_level(logging.DEBUG):
        result = channel.send(message)

    assert (result.sent, result.skipped, result.error) == (True, False, "")
    [(request, timeout)] = requests
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == "https://slack.com/api/chat.postMessage"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {_SLACK_TOKEN}"
    assert str(request.get_header("Content-type", "")).startswith("application/json")
    assert timeout == 3.0
    assert isinstance(request.data, bytes)
    body = json.loads(request.data)
    assert body["channel"] == _SLACK_CHANNEL
    assert "ORCA completed" in body["text"]
    assert "water-opt" in json.dumps(body)
    assert _SLACK_TOKEN not in json.dumps(body)
    # Neither the token nor the message payload is logged.
    assert _SLACK_TOKEN not in caplog.text
    assert "water-opt" not in caplog.text


def test_selected_slack_without_slack_settings_never_routes_through_discord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_network(monkeypatch)
    complete_discord = DiscordConfig(bot_token="synthetic-discord-token", default_channel_id="123")

    selected_slack = MessengerConfig(provider="slack", discord=complete_discord)
    assert not selected_slack.enabled
    channel = build_channel(selected_slack)
    assert isinstance(channel, DisabledChannel)
    result = channel.send(Message(title="T"))
    assert (result.sent, result.skipped) == (False, True)

    # The matching provider still builds Discord; nothing is sent.
    discord = build_channel(MessengerConfig(provider="discord", discord=complete_discord))
    assert isinstance(discord, DiscordBotChannel)
    assert discord.enabled


def test_messenger_config_from_mapping() -> None:
    cfg = messenger_config_from_mapping(
        {
            "provider": "Discord",
            "discord": {"bot_token": "token", "default_channel_id": "123"},
        }
    )
    assert cfg.discord.bot_token == "token"
    assert cfg.discord.bot_notification_enabled
    assert cfg.enabled

    empty = messenger_config_from_mapping(None)
    assert not empty.discord.bot_notification_enabled
    assert not empty.enabled

    with pytest.raises(ValueError, match="messenger.provider"):
        messenger_config_from_mapping({"provider": "disocrd"})
    with pytest.raises(ValueError, match="messenger.provider"):
        messenger_config_from_mapping({"provider": ""})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("discord", "messenger config"),
        ({"discord": None}, "messenger.discord"),
        ({"discord": ["bot"]}, "messenger.discord"),
    ],
)
def test_messenger_config_rejects_malformed_sections(raw: object, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        messenger_config_from_mapping(raw)
