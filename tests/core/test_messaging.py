"""Tests for the messenger-neutral notification layer (core/messaging)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterable
from email.message import Message as EmailMessage
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

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
    load_messenger_config_from_file,
    load_required_messenger_config_from_file,
    raw,
    render_discord_embed,
    render_telegram,
    text,
    title_heading,
)
from orca_auto.core.messaging import discord_webhook as discord_mod
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
        'orca_auto.core.messaging.discord_webhook',
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
    assert embed["title"] == "ORCA Started"
    assert embed["color"] == 0x2ECC71
    assert embed["fields"] == [
        {"name": "Job", "value": "rxn", "inline": False},
        {"name": "Attempt", "value": r"\#3", "inline": False},
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
    assert embed["title"] == r"\*literal\*"
    assert embed["fields"] == [
        {
            "name": r"\[key\]",
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
# Discord webhook channel
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status: int, body: bytes = b'{"id":"999"}') -> None:
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


def test_discord_channel_disabled_is_skipped() -> None:
    result = DiscordWebhookChannel(DiscordConfig()).send(Message(title="x"))
    assert result.skipped and not result.sent


def test_discord_webhook_channel_stays_disabled_for_bot_only_config() -> None:
    channel = DiscordWebhookChannel(DiscordConfig(bot_token="token", default_channel_id="123"))

    result = channel.send(Message(title="x"))

    assert not channel.enabled
    assert result.skipped
    assert result.error == "discord_disabled"


def test_discord_channel_posts_embed_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(request)
        return _FakeResponse(200)

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url=(
                "https://discord.com/api/v10/webhooks/123/secret?thread_id=456&wait=false&tag="
            )
        )
    )
    result = channel.send(
        Message(title="T", groups=(group(field_row("K", text("v")), heading=title_heading("T")),))
    )
    assert result.sent
    assert result.provider == "discord"
    assert result.message_id == "999"
    assert result.message_ids == ("999",)
    assert (result.sent_count, result.total_count) == (1, 1)
    assert len(calls) == 1
    query = parse_qs(urlsplit(calls[0].full_url).query, keep_blank_values=True)  # type: ignore[attr-defined]
    assert query == {"thread_id": ["456"], "tag": [""], "wait": ["true"]}
    payload = json.loads(calls[0].data)  # type: ignore[attr-defined]
    assert payload["embeds"][0]["title"] == "T"
    assert payload["embeds"][0]["fields"] == [{"name": "K", "value": "v", "inline": False}]
    assert payload["allowed_mentions"] == {"parse": []}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (204, b"", "discord_unconfirmed_delivery"),
        (200, b"not-json", "discord_invalid_response"),
        (200, b'{"content":"missing id"}', "discord_invalid_response"),
        (200, b'{"id":"not-a-snowflake"}', "discord_invalid_response"),
        (200, b'{"id":"0"}', "discord_invalid_response"),
    ],
)
def test_discord_channel_requires_confirmed_message(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
    error: str,
) -> None:
    monkeypatch.setattr(
        discord_mod, "urlopen", lambda *_args, **_kwargs: _FakeResponse(status, body)
    )
    channel = DiscordWebhookChannel(
        DiscordConfig(webhook_url="https://discord.com/api/webhooks/123/secret")
    )
    result = channel.send(Message(title="T"))
    assert not result.sent
    assert result.error == error


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123/secret",
        "https://discord.com.evil.example/api/webhooks/123/secret",
        "https://discord.com/api/not-webhooks/123/secret",
        "https://discord.com/api/webhooks/0/secret",
        "https://user:password@discord.com/api/webhooks/123/secret",
    ],
)
def test_discord_channel_rejects_non_discord_webhook_urls_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    called = False

    def fake_urlopen(*_args: object, **_kwargs: object) -> _FakeResponse:
        nonlocal called
        called = True
        return _FakeResponse(200)

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    result = DiscordWebhookChannel(DiscordConfig(webhook_url=url)).send(Message(title="T"))
    assert not called
    assert not result.sent
    assert result.error == "discord_invalid_webhook_url"
    assert "secret" not in result.error


def test_discord_channel_retries_on_http_429(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = EmailMessage()
    headers["Retry-After"] = "0.25"
    response_body = BytesIO(b'{"retry_after": 9}')
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError(
            "https://discord.com/api/webhooks/123/secret",
            429,
            "rate",
            headers,
            response_body,
        ),
        _FakeResponse(200),
    ]
    delays: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        item = sequence.pop(0)
        if isinstance(item, HTTPError):
            raise item
        return item

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/secret",
            max_attempts=2,
            retry_backoff_seconds=0.0,
        ),
        sleeper=delays.append,
    )
    result = channel.send(Message(title="T"))
    assert result.sent
    assert sequence == []
    assert delays == [0.25]
    assert response_body.closed


def test_discord_channel_uses_retry_after_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError(
            "https://discord.com/api/webhooks/123/secret",
            429,
            "rate",
            EmailMessage(),
            BytesIO(b'{"retry_after": 0.125}'),
        ),
        _FakeResponse(200),
    ]
    delays: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        item = sequence.pop(0)
        if isinstance(item, HTTPError):
            raise item
        return item

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/secret",
            max_attempts=2,
        ),
        sleeper=delays.append,
    )
    assert channel.send(Message(title="T")).sent
    assert delays == [0.125]


def test_discord_channel_allows_documented_global_retry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = EmailMessage()
    headers["Retry-After"] = "65"
    sequence: list[_FakeResponse | HTTPError] = [
        HTTPError(
            "https://discord.com/api/webhooks/123/secret",
            429,
            "global rate limit",
            headers,
            BytesIO(b'{"retry_after":65,"global":true}'),
        ),
        _FakeResponse(200),
    ]
    delays: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        item = sequence.pop(0)
        if isinstance(item, HTTPError):
            raise item
        return item

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/secret",
            max_attempts=2,
        ),
        sleeper=delays.append,
    )
    assert channel.send(Message(title="T")).sent
    assert delays == [65.0]


def test_discord_channel_rejects_excessive_retry_after_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = EmailMessage()
    headers["Retry-After"] = "1337"
    calls = 0

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        nonlocal calls
        calls += 1
        raise HTTPError(
            "https://discord.com/api/webhooks/123/super-secret-token",
            429,
            "rate",
            headers,
            BytesIO(b'{"retry_after": 0.1}'),
        )

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/super-secret-token",
            max_attempts=2,
        ),
        sleeper=lambda _delay: pytest.fail("must not sleep"),
    )
    result = channel.send(Message(title="T"))
    assert calls == 1
    assert not result.sent
    assert result.error == "discord_retry_after_exceeds_limit"
    assert "super-secret-token" not in result.error


def test_discord_channel_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        raise HTTPError(
            "https://discord.com/api/webhooks/123/super-secret-token",
            500,
            "err",
            EmailMessage(),
            None,
        )

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    monkeypatch.setattr(discord_mod.time, "sleep", lambda _seconds: None)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/super-secret-token",
            max_attempts=2,
            retry_backoff_seconds=0.0,
        )
    )
    result = channel.send(Message(title="T"))
    assert not result.sent
    assert result.error == "discord_http_500"
    assert "super-secret-token" not in result.error


def test_discord_channel_defensively_caps_programmatic_attempts_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        timeouts.append(timeout)
        raise HTTPError(
            "https://discord.com/api/webhooks/123/secret",
            500,
            "err",
            EmailMessage(),
            None,
        )

    monkeypatch.setattr(discord_mod, "urlopen", fake_urlopen)
    channel = DiscordWebhookChannel(
        DiscordConfig(
            webhook_url="https://discord.com/api/webhooks/123/secret",
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


# --------------------------------------------------------------------------- #
# Registry / config
# --------------------------------------------------------------------------- #
def test_build_channel_selects_provider() -> None:
    telegram = TelegramConfig(bot_token="1:A", chat_id="9")
    assert isinstance(
        build_channel(MessengerConfig(provider="telegram", telegram=telegram)), TelegramChannel
    )
    discord = build_channel(
        MessengerConfig(provider="discord", discord=DiscordConfig(webhook_url="https://x"))
    )
    assert isinstance(discord, DiscordWebhookChannel)
    with pytest.raises(ValueError, match="Unsupported messenger provider"):
        build_channel(MessengerConfig(provider="bogus", telegram=telegram))


def test_messenger_config_from_mapping() -> None:
    webhook_url = "https://discord.com/api/webhooks/123/secret"
    cfg = messenger_config_from_mapping(
        {"provider": "Discord", "discord": {"webhook_url": webhook_url}}
    )
    assert cfg.normalized_provider == "discord"
    assert cfg.discord.webhook_url == webhook_url
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
    with pytest.raises(ValueError, match="messenger.discord.webhook_url"):
        messenger_config_from_mapping(
            {"provider": "discord", "discord": {"webhook_url": "https://example.test/hook"}}
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("telegram", "messenger config"),
        ({"telegram": "token"}, "messenger.telegram"),
        ({"discord": ["webhook"]}, "messenger.discord"),
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
