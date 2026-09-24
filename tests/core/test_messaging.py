"""Tests for the messenger-neutral notification layer (core/messaging)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from orca_auto.core.config import (
    DiscordConfig,
    MessengerConfig,
    messenger_config_from_mapping,
)
from orca_auto.core.messaging import (
    DisabledChannel,
    DiscordBotChannel,
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


def test_neutral_messaging_import_does_not_eagerly_load_adapters() -> None:
    code_under_test = """
import sys
import orca_auto.core.messaging
blocked = [
    name for name in (
        'orca_auto.core.messaging.discord_bot',
        'orca_auto.core.messaging.discord_http',
    )
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
    assert len(embed["fields"]) <= 25
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
