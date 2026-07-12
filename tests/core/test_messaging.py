"""Tests for the messenger-neutral notification layer (core/messaging)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.config import (
    DiscordConfig,
    MessengerConfig,
    TelegramConfig,
    messenger_config_from_mapping,
)
from orca_auto.core.messaging import (
    DiscordBotChannel,
    Message,
    Severity,
    TelegramChannel,
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
    render_telegram,
    text,
    title_heading,
)
from orca_auto.core.messaging import telegram_channel as telegram_mod
from orca_auto.core.messaging.render_telegram import render_telegram_chunks
from orca_auto.core.notifications._engine_transport import _lines_message


def test_neutral_messaging_import_does_not_eagerly_load_adapters() -> None:
    code_under_test = """
import sys
import orca_auto.core.messaging
blocked = [
    name for name in (
        'orca_auto.core.messaging.discord_bot',
        'orca_auto.core.messaging.discord_http',
        'orca_auto.core.messaging.telegram_channel',
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
    assert render_telegram(message) == "<b>t</b>\n   indentedpadded"


def test_render_telegram_uses_semantic_title_without_caller_heading() -> None:
    assert render_telegram(Message(title="Only title")) == "<b>Only title</b>"


def test_render_telegram_chunks_keep_tags_atomic_and_plain_fallback_independent() -> None:
    chunks = render_telegram_chunks(
        Message(
            title="Long",
            groups=(group(line(code("<" * 5000)), heading=title_heading("Long")),),
        )
    )

    assert len(chunks) > 1
    assert all(len(chunk.html) <= 4096 for chunk in chunks)
    assert all(chunk.html.count("<code>") == chunk.html.count("</code>") for chunk in chunks)
    assert "".join(chunk.plain for chunk in chunks) == "Long\n" + ("<" * 5000)
    assert all("<code>" not in chunk.plain for chunk in chunks)


def test_render_telegram_chunks_rejects_limit_too_small_for_escaped_span() -> None:
    message = Message(title="", groups=(group(line(code("&"))),))

    with pytest.raises(ValueError, match="cannot fit one escaped character"):
        render_telegram_chunks(message, limit=14)


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

    assert render_telegram(message) == "Job queued\njob_id: one"
    assert render_discord_embed(message) == {
        "title": "Job queued",
        "color": 0x3498DB,
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


def test_engine_line_message_maps_terminal_headline_to_severity() -> None:
    from orca_auto.core.notifications._engine_rendering import severity_for_event_line

    assert severity_for_event_line("[orca_auto_xtb] Job failed") == "error"
    assert severity_for_event_line("[orca_auto_xtb] Job cancelled") == "warning"
    assert severity_for_event_line("[orca_auto_xtb] Job finished") == "success"
    assert severity_for_event_line("[orca_auto_xtb] Job queued") == "info"

    embed = render_discord_embed(
        _lines_message(["[orca_auto_xtb] Job failed", "status: failed"], "error")
    )
    assert embed["color"] == 0xE74C3C
    assert embed["title"] == "❌ [orca_auto_xtb] Job failed"


# --------------------------------------------------------------------------- #
# Telegram channel
# --------------------------------------------------------------------------- #
def test_telegram_channel_disabled_is_skipped() -> None:
    result = TelegramChannel(TelegramConfig()).send(Message(title="x"))
    assert result.skipped and not result.sent


def test_telegram_channel_sends_rendered_html(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(
        config: object,
        chunks: Iterable[tuple[str, str]],
        **kwargs: object,
    ) -> SimpleNamespace:
        captured["chunks"] = list(chunks)
        captured["parse_mode"] = kwargs.get("parse_mode")
        captured["silent"] = kwargs.get("silent")
        return SimpleNamespace(
            sent=True,
            skipped=False,
            error="",
            message_ids=("42",),
            sent_count=1,
            total_count=1,
        )

    monkeypatch.setattr(telegram_mod, "send_rendered_telegram_chunks", fake_send)
    channel = TelegramChannel(TelegramConfig(bot_token="1:A", chat_id="9"))
    result = channel.send(
        Message(
            title="Hi", groups=(group(field_row("K", text("v")), heading=title_heading("Hi")),)
        ),
        silent=True,
    )
    assert result.sent
    assert result.provider == "telegram"
    assert result.message_id == "42"
    assert result.message_ids == ("42",)
    assert captured["chunks"] == [("<b>Hi</b>\n<b>K</b>: v", "Hi\nK: v")]
    assert captured["parse_mode"] == "HTML"
    assert captured["silent"] is True


# --------------------------------------------------------------------------- #
# Registry / config
# --------------------------------------------------------------------------- #
def test_build_channel_selects_provider() -> None:
    telegram = TelegramConfig(bot_token="1:A", chat_id="9")
    assert isinstance(
        build_channel(MessengerConfig(provider="telegram", telegram=telegram)), TelegramChannel
    )
    discord = build_channel(
        MessengerConfig(
            provider="discord",
            discord=DiscordConfig(bot_token="token", default_channel_id="123"),
        )
    )
    assert isinstance(discord, DiscordBotChannel)
    with pytest.raises(ValueError, match="Unsupported messenger provider"):
        build_channel(MessengerConfig(provider="bogus", telegram=telegram))


def test_messenger_config_from_mapping() -> None:
    cfg = messenger_config_from_mapping(
        {
            "provider": "Discord",
            "discord": {"bot_token": "token", "default_channel_id": "123"},
        }
    )
    assert cfg.normalized_provider == "discord"
    assert cfg.discord.bot_token == "token"
    assert cfg.discord.enabled

    telegram = messenger_config_from_mapping(
        {"telegram": {"bot_token": "token", "chat_id": "chat"}}
    )
    assert telegram.telegram.enabled

    empty = messenger_config_from_mapping(None)
    assert empty.normalized_provider == "telegram"
    assert not empty.discord.enabled

    with pytest.raises(ValueError, match="messenger.provider"):
        messenger_config_from_mapping({"provider": "disocrd"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("telegram", "messenger config"),
        ({"telegram": "token"}, "messenger.telegram"),
        ({"discord": ["bot"]}, "messenger.discord"),
    ],
)
def test_messenger_config_rejects_malformed_sections(raw: object, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        messenger_config_from_mapping(raw)


def test_messenger_config_file_dual_reads_legacy_with_nested_precedence(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "telegram:\n  bot_token: legacy-token\n  chat_id: legacy-chat\n",
        encoding="utf-8",
    )
    legacy = load_messenger_config_from_file(config_path)
    assert legacy.telegram.bot_token == "legacy-token"
    assert legacy.telegram.chat_id == "legacy-chat"

    config_path.write_text(
        "\n".join(
            [
                "telegram:",
                "  bot_token: legacy-token",
                "  chat_id: legacy-chat",
                "messenger:",
                "  telegram:",
                "    chat_id: nested-chat",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    nested = load_messenger_config_from_file(config_path)
    assert nested.telegram.bot_token == ""
    assert nested.telegram.chat_id == "nested-chat"


def test_required_messenger_config_rejects_missing_and_invalid_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(FileNotFoundError, match="bot config does not exist"):
        load_required_messenger_config_from_file(missing)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("messenger: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML syntax"):
        load_required_messenger_config_from_file(invalid)
