"""Slack attachment layout, literal text and payload budgets (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from orca_auto.core.config import SlackConfig
from orca_auto.core.messaging import Message, Severity, code, field_row, group, raw, text
from orca_auto.core.messaging.render_slack import (
    render_slack_attachment,
    render_slack_blocks,
    render_slack_chrome_blocks,
    render_slack_detail_blocks,
)
from orca_auto.core.messaging.slack_bot import SlackBotChannel


def _detail(message: Message) -> list[dict[str, Any]]:
    chrome = render_slack_chrome_blocks(message)
    return render_slack_detail_blocks(message, chrome_block_count=len(chrome))


@pytest.mark.parametrize(
    ("severity", "color", "glyph"),
    [
        ("info", "#3498DB", "ℹ️"),
        ("success", "#2ECC71", "✅"),
        ("warning", "#F39C12", "⚠️"),
        ("error", "#E74C3C", "❌"),
        ("unknown", "#3498DB", "ℹ️"),
    ],
)
def test_severity_controls_attachment_color_and_chrome_header(
    severity: str, color: str, glyph: str
) -> None:
    message = Message(title="Event", severity=cast(Severity, severity))
    assert render_slack_attachment(message) is None
    assert render_slack_chrome_blocks(message) == [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{glyph} Event", "emoji": False},
        }
    ]
    message = Message(
        title="Event",
        severity=cast(Severity, severity),
        groups=(group(field_row("L", text("v"))),),
    )
    attachment = render_slack_attachment(message)
    assert attachment is not None
    assert attachment["color"] == color
    assert attachment["blocks"] == [
        {
            "type": "section",
            "text": {"type": "plain_text", "text": "L: v", "emoji": False},
        }
    ]


@pytest.mark.parametrize(
    ("label", "value", "compact"),
    [
        ("L", "v" * 36, True),
        ("L", "v" * 37, True),  # Exactly 40 characters in "label: value".
        ("L", "v" * 38, False),
        ("Long label" * 4, "v", False),
        ("L\nL", "v", False),
        ("L", "v\nv", False),
        ("L", "v\rv", False),
        ("L", "v\u2028v", False),
    ],
)
def test_inline_requires_short_single_line_label_and_value(
    label: str, value: str, compact: bool
) -> None:
    message = Message(title="Event", groups=(group(field_row(label, raw(value), inline=True)),))
    assert render_slack_blocks(message)[0]["type"] == "header"
    [section] = _detail(message)
    if compact:
        assert section == {
            "type": "section",
            "fields": [{"type": "plain_text", "text": f"{label}\n{value}", "emoji": False}],
        }
    else:
        assert section == {
            "type": "section",
            "text": {"type": "plain_text", "text": f"{label}: {value}", "emoji": False},
        }


@pytest.mark.parametrize("line_break", ["\n", "\r", "\u2028"])
def test_trailing_line_break_does_not_make_a_field_compact(line_break: str) -> None:
    message = Message(
        title="Event", groups=(group(field_row("L", raw(f"v{line_break}"), inline=True)),)
    )
    assert _detail(message)[0] == {
        "type": "section",
        "text": {"type": "plain_text", "text": "L: v", "emoji": False},
    }


def test_inline_runs_and_full_width_rows_preserve_field_and_group_order() -> None:
    message = Message(
        title="Event",
        groups=(
            group(
                field_row("A", text("1"), inline=True),
                field_row("B", text("2"), inline=True),
                field_row("C", text("3")),  # Short, but explicitly full width.
                field_row("D", text("4"), inline=True),
                field_row("E", text("5")),
                field_row("F", text("6"), inline=True),
            ),
            group(field_row("G", text("7"), inline=True)),
        ),
    )
    sections = _detail(message)
    assert [
        [item["text"] for item in section["fields"]]
        if "fields" in section
        else section["text"]["text"]
        for section in sections
    ] == [["A\n1", "B\n2"], "C: 3", ["D\n4"], "E: 5", ["F\n6"], ["G\n7"]]


@pytest.mark.parametrize("label", ["Job", "Queue ID", "Directory", "Arbitrary label"])
def test_long_values_are_full_width_even_with_inline_hint(label: str) -> None:
    value = "/runs/synthetic_professor_pt_mode_retry_20261113/" + "a1" * 16
    message = Message(title="Event", groups=(group(field_row(label, code(value), inline=True)),))
    assert _detail(message)[0] == {
        "type": "section",
        "text": {"type": "plain_text", "text": f"{label}: {value}", "emoji": False},
    }


def test_untrusted_text_stays_literal_in_every_block_text_object() -> None:
    malicious = (
        "<!channel> <@U123> <#C123|channel> <https://example.invalid|link> *bold* `code` :wave: &"
    )
    message = Message(
        title=malicious,
        author=malicious,
        groups=(
            group(
                field_row("<!here>", text("<@U123>"), inline=True),
                field_row(malicious, raw(malicious)),
            ),
        ),
    )
    header, context = render_slack_blocks(message)
    attachment = render_slack_attachment(message)
    assert attachment is not None
    compact, wide = attachment["blocks"]
    objects = [header["text"], context["elements"][0], compact["fields"][0], wide["text"]]
    assert objects == [
        {"type": "plain_text", "text": f"ℹ️ {malicious}", "emoji": False},
        {"type": "plain_text", "text": malicious, "emoji": False},
        {"type": "plain_text", "text": "<!here>\n<@U123>", "emoji": False},
        {"type": "plain_text", "text": f"{malicious}: {malicious}", "emoji": False},
    ]
    channel = SlackBotChannel(SlackConfig())
    payload = channel._payload(message)
    escaped = malicious.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert payload["text"] == (
        f"{escaped}: {escaped}\n&lt;!here&gt;: &lt;@U123&gt;\n{escaped}: {escaped}"
    )
    assert payload["blocks"] == render_slack_blocks(message)
    assert payload["attachments"] == [render_slack_attachment(message)]
    assert payload["mrkdwn"] is False
    assert payload["parse"] == "none"
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False


def test_field_lines_are_not_duplicated_between_chrome_and_attachment() -> None:
    job = "water-opt"
    message = Message(
        title="ORCA completed",
        author="orca_auto",
        severity="success",
        groups=(
            group(
                field_row("Job", text(job)),
                field_row("Result", text("ok")),
            ),
        ),
    )
    payload = SlackBotChannel(SlackConfig())._payload(message)
    chrome_blob = json.dumps(payload["blocks"])
    attachments = payload["attachments"]
    assert isinstance(attachments, list)
    [attachment] = attachments
    assert isinstance(attachment, dict)
    detail_blob = json.dumps(attachment["blocks"])
    assert "Job:" in detail_blob
    assert "Result:" in detail_blob
    assert "Job:" not in chrome_blob
    assert "Result:" not in chrome_blob
    fallback_text = payload["text"]
    assert isinstance(fallback_text, str)
    assert job in fallback_text


def test_no_attachment_when_message_has_no_field_sections() -> None:
    message = Message(title="Ping", author="orca_auto")
    assert render_slack_attachment(message) is None
    payload = SlackBotChannel(SlackConfig())._payload(message)
    assert "attachments" not in payload
    assert payload["blocks"] == render_slack_chrome_blocks(message)


def test_text_limits_and_empty_header_remain_valid() -> None:
    message = Message(
        title="T" * 200,
        author="A" * 2100,
        groups=(group(field_row("L", text("v" * 4000))),),
    )
    header, context = render_slack_blocks(message)
    attachment = render_slack_attachment(message)
    assert attachment is not None
    [section] = attachment["blocks"]
    assert header["text"]["text"] == "ℹ️ " + "T" * 146 + "…"
    assert context["elements"][0]["text"] == "A" * 1999 + "…"
    assert section["text"]["text"] == "L: " + "v" * 2996 + "…"
    payload = SlackBotChannel(SlackConfig())._payload(message)
    assert len(str(payload["text"])) == 3000
    assert str(payload["text"]).endswith("…")
    assert render_slack_blocks(Message(title="  ", author="  ")) == [
        {"type": "header", "text": {"type": "plain_text", "text": "ℹ️ -", "emoji": False}}
    ]


@pytest.mark.parametrize("count", [9, 10, 11])
def test_compact_sections_respect_ten_field_limit_without_losing_values(count: int) -> None:
    message = Message(
        title="Event",
        groups=(group(*(field_row("L", text(i), inline=True) for i in range(count))),),
    )
    sections = _detail(message)
    assert [len(section["fields"]) for section in sections] == ([count] if count <= 10 else [10, 1])
    assert [item["text"] for section in sections for item in section["fields"]] == [
        f"L\n{i}" for i in range(count)
    ]


@pytest.mark.parametrize("author", [None, "orca_auto"])
@pytest.mark.parametrize("extra", [-1, 0, 1])
def test_combined_block_budget_across_chrome_and_attachment(author: str | None, extra: int) -> None:
    chrome_count = 1 if author is None else 2
    count = (50 - chrome_count) + extra
    message = Message(
        title="Event",
        author=author,
        groups=(group(*(field_row("L", text(i)) for i in range(count))),),
    )
    chrome = render_slack_chrome_blocks(message)
    attachment = render_slack_attachment(message, chrome_block_count=len(chrome))
    assert len(chrome) == chrome_count
    assert attachment is not None
    detail = attachment["blocks"]
    assert len(chrome) + len(detail) == min(50, 50 + extra)
    assert detail[-1]["text"]["text"] == ("More: …" if extra > 0 else f"L: {count - 1}")
    assert detail[0]["text"]["text"] == "L: 0"
    assert detail[-2]["text"]["text"] == f"L: {len(detail) - 2}"


def test_queued_payload_matches_review_fixture() -> None:
    job = "synthetic_professor_pt_mode_retry_20261113"
    message = Message(
        title="ORCA queued",
        author="orca_auto",
        groups=(
            group(
                field_row("Job", text(job)),
                field_row("Queue ID", code("q_20261113_151926_" + "a1" * 16)),
                field_row("Priority", text(10)),
                field_row("Directory", code(f"/runs/{job}")),
            ),
        ),
    )
    payload = SlackBotChannel(SlackConfig(default_channel_id="C0SYNTHETIC1"))._payload(message)
    fixture_dir = Path(__file__).parents[2] / "fixtures" / "slack"
    assert payload == json.loads((fixture_dir / "queued-after.json").read_text(encoding="utf-8"))
    before = json.loads((fixture_dir / "queued-before.json").read_text(encoding="utf-8"))
    assert "attachments" not in before
    assert "blocks" in before
    assert "blocks" in payload
    assert payload["text"] == before["text"]
