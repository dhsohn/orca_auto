"""Tests for the messenger-neutral notification layer (core/messaging)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from orca_auto.core.config import (
    DiscordConfig,
    MessengerConfig,
    messenger_config_from_mapping,
)
from orca_auto.core.messaging import (
    DiscordBotChannel,
    Message,
    Severity,
    bold,
    build_channel,
    code,
    field_row,
    group,
    line,
    load_messenger_config_from_file,
    load_required_messenger_config_from_file,
    raw,
    render_discord_embed,
    text,
    title_heading,
)
from orca_auto.core.notifications.engines import _lines_message


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
    assert embed["title"] == "✅ ORCA Started"
    assert embed["color"] == 0x2ECC71
    assert embed["fields"] == [
        {"name": "Job", "value": "rxn", "inline": False},
        {"name": "Attempt", "value": r"\#3", "inline": False},
    ]
    assert "description" not in embed
    assert "author" not in embed


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


def test_engine_line_message_uses_native_discord_title_without_description_duplication() -> None:
    message = _lines_message(["Job queued", "job_id: one"])

    assert render_discord_embed(message) == {
        "title": "Job queued",
        "color": 0x3498DB,
        "author": {"name": "orca_auto"},
        "description": r"job\_id: one",
    }


def test_render_discord_embed_escapes_markdown_and_embedded_backticks() -> None:
    message = Message(
        title="*literal*",
        groups=(
            group(
                field_row("[key]", text("@everyone **not bold**"), code("a`b")),
                heading=title_heading("*literal*"),
            ),
        ),
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
        groups=(
            group(
                line(text("D" * 5000)),
                *(field_row(f"field-{index}", text("V" * 1024)) for index in range(25)),
            ),
        ),
    )
    embed = render_discord_embed(message)
    total = (
        len(embed["title"])
        + len(embed.get("description", ""))
        + sum(len(item["name"]) + len(item["value"]) for item in embed.get("fields", []))
    )
    assert total <= 6000
    assert len(embed["description"]) <= 4096
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


def test_engine_terminal_presentation_uses_structured_status_and_fails_closed() -> None:
    from orca_auto.core.notifications.engines import (
        terminal_headline,
        terminal_severity,
    )

    assert terminal_headline("completed") == "Job finished"
    assert terminal_headline("failed") == "Job failed"
    assert terminal_headline("cancelled") == "Job cancelled"
    assert terminal_severity("completed") == "success"
    assert terminal_severity("failed") == "error"
    assert terminal_severity("cancelled") == "warning"
    for unknown_status in ("running", "unknown", "", "COMPLETED", " completed "):
        assert terminal_headline(unknown_status) == "Job status unknown"
        assert terminal_severity(unknown_status) == "info"

    embed = render_discord_embed(_lines_message(["[xTB] Job failed", "status: failed"], "error"))
    assert embed["color"] == 0xE74C3C
    assert embed["title"] == "❌ [xTB] Job failed"
    assert embed["author"] == {"name": "orca_auto"}


# --------------------------------------------------------------------------- #
# Registry / config
# --------------------------------------------------------------------------- #
def test_build_channel_selects_provider() -> None:
    discord = build_channel(
        MessengerConfig(
            provider="discord",
            discord=DiscordConfig(bot_token="token", default_channel_id="123"),
        )
    )
    assert isinstance(discord, DiscordBotChannel)
    with pytest.raises(ValueError, match="Unsupported messenger provider"):
        build_channel(MessengerConfig(provider="bogus"))


def test_messenger_config_from_mapping() -> None:
    cfg = messenger_config_from_mapping(
        {
            "provider": "Discord",
            "discord": {"bot_token": "token", "default_channel_id": "123"},
        }
    )
    assert cfg.normalized_provider == "discord"
    assert cfg.discord.bot_token == "token"
    assert cfg.discord.bot_notification_enabled

    empty = messenger_config_from_mapping(None)
    assert empty.normalized_provider == "discord"
    assert not empty.discord.bot_notification_enabled

    with pytest.raises(ValueError, match="messenger.provider"):
        messenger_config_from_mapping({"provider": "disocrd"})


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


def test_messenger_config_file_rejects_leftover_telegram_block(
    tmp_path: Path,
) -> None:
    # Telegram is no longer supported: a leftover top-level block fails closed
    # with a migration hint rather than silently disabling notifications.
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "telegram:\n  bot_token: legacy-token\n  chat_id: legacy-chat\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no longer supported"):
        load_messenger_config_from_file(config_path)

    # A nested messenger.telegram block is likewise rejected as an unknown field.
    config_path.write_text(
        "messenger:\n  telegram:\n    chat_id: nested-chat\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unknown messenger config fields"):
        load_messenger_config_from_file(config_path)


def test_required_messenger_config_rejects_missing_and_invalid_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError, match="bot config does not exist"):
        load_required_messenger_config_from_file(missing)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("messenger: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML syntax"):
        load_required_messenger_config_from_file(invalid)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("schedulr: {}\n", "Unknown top-level config fields are not supported"),
        ("scheduler: []\n", "scheduler section must be a mapping"),
        (
            "messenger:\n  discord:\n    bot_token: []\n",
            "messenger.discord.bot_token must be a string",
        ),
    ],
)
def test_messenger_file_loaders_reject_invalid_shared_config_before_selection(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(payload, encoding="utf-8")

    for loader in (
        load_messenger_config_from_file,
        load_required_messenger_config_from_file,
    ):
        with pytest.raises(ValueError, match=message):
            loader(config_path)
