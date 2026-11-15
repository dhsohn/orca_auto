"""Slack rich_text layout, literal text and payload budgets (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from orca_auto.core.config import SlackConfig
from orca_auto.core.messaging import Message, Severity, code, field_row, group, raw, text
from orca_auto.core.messaging.render_slack import render_slack_blocks
from orca_auto.core.messaging.slack_bot import SlackBotChannel


def _leaves(block: dict[str, Any]) -> list[dict[str, Any]]:
    assert block["type"] == "rich_text"
    section = block["elements"][0]
    assert section["type"] == "rich_text_section"
    return list(section["elements"])


def _section_leaves(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert len(blocks) == 1
    return _leaves(blocks[0])


def _plain(block: dict[str, Any]) -> str:
    return "".join(str(leaf.get("text", "")) for leaf in _leaves(block))


def _plain_body(blocks: list[dict[str, Any]]) -> str:
    return _plain(blocks[0])


def _row_text(blocks: list[dict[str, Any]]) -> list[str]:
    after_title = _plain_body(blocks).split("\n", 1)[1] if blocks else ""
    segments = after_title.split("\n\n") if after_title else []
    rows: list[str] = []
    for index, segment in enumerate(segments):
        rows.extend(line for line in segment.split("\n") if line or line == "")
        if index + 1 < len(segments):
            rows.append("\n")
    return rows


def _leaf_types(blocks: list[dict[str, Any]]) -> set[str]:
    return {leaf["type"] for block in blocks for leaf in _leaves(block)}


def _rendered_char_count(blocks: list[dict[str, Any]]) -> int:
    return sum(len(str(leaf.get("text", ""))) for leaf in _section_leaves(blocks))


@pytest.mark.parametrize(
    ("severity", "glyph"),
    [
        ("info", "ℹ️"),
        ("success", "✅"),
        ("warning", "⚠️"),
        ("error", "❌"),
        ("unknown", "ℹ️"),
    ],
)
def test_severity_controls_title_glyph(severity: str, glyph: str) -> None:
    message = Message(title="Event", severity=cast(Severity, severity))
    leaves = _section_leaves(render_slack_blocks(message))
    assert leaves[0] == {"type": "text", "text": f"{glyph} "}
    message = Message(
        title="Event",
        severity=cast(Severity, severity),
        groups=(group(field_row("L", text("v"))),),
    )
    blocks = render_slack_blocks(message)
    assert len(blocks) == 1 and blocks[0]["type"] == "rich_text"
    assert "L: v" in _plain_body(blocks)


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("L", "v" * 36),
        ("L", "v" * 37),
        ("L", "v" * 38),
        ("Long label" * 4, "v"),
        ("L\nL", "v"),
        ("L", "v\nv"),
        ("L", "v\rv"),
        ("L", "v\u2028v"),
    ],
)
def test_inline_hint_is_ignored_on_slack(label: str, value: str) -> None:
    message = Message(title="Event", groups=(group(field_row(label, raw(value), inline=True)),))
    blocks = render_slack_blocks(message)
    assert blocks[0]["type"] == "rich_text"
    assert f"{label}: {value}" in _plain_body(blocks)


def test_full_width_rows_preserve_field_and_group_order() -> None:
    message = Message(
        title="Event",
        groups=(
            group(
                field_row("A", text("1"), inline=True),
                field_row("B", text("2"), inline=True),
                field_row("C", text("3")),
                field_row("D", text("4"), inline=True),
                field_row("E", text("5")),
                field_row("F", text("6"), inline=True),
            ),
            group(field_row("G", text("7"), inline=True)),
        ),
    )
    assert _row_text(render_slack_blocks(message)) == [
        "A: 1",
        "B: 2",
        "C: 3",
        "D: 4",
        "E: 5",
        "F: 6",
        "\n",
        "G: 7",
    ]


@pytest.mark.parametrize("label", ["Job", "Queue ID", "Directory", "Arbitrary label"])
def test_long_values_stay_full_width(label: str) -> None:
    value = "/runs/synthetic_professor_pt_mode_retry_20261113/" + "a1" * 16
    message = Message(title="Event", groups=(group(field_row(label, code(value), inline=True)),))
    leaves = _section_leaves(render_slack_blocks(message))
    job_index = next(index for index, leaf in enumerate(leaves) if leaf.get("text") == f"{label}: ")
    assert leaves[job_index] == {"type": "text", "text": f"{label}: ", "style": {"bold": True}}
    assert leaves[job_index + 1] == {"type": "text", "text": value, "style": {"code": True}}


def test_untrusted_text_stays_literal_in_typed_leaves_only() -> None:
    malicious = (
        "<!channel> <@U123> <#C123|channel> <https://example.invalid|link> *bold* `code` :wave: &"
    )
    message = Message(
        title=malicious,
        author="orca_auto",
        groups=(
            group(
                field_row("<!here>", text("<@U123>"), inline=True),
                field_row(malicious, raw(malicious)),
            ),
        ),
    )
    blocks = render_slack_blocks(message)
    assert _leaf_types(blocks) == {"text"}
    leaves = _section_leaves(blocks)
    assert leaves[1] == {"type": "text", "text": malicious, "style": {"bold": True}}
    assert leaves[2] == {"type": "text", "text": " · orca_auto"}
    assert any(leaf.get("text") == "<@U123>" for leaf in leaves)
    assert any(leaf.get("text") == malicious and leaf.get("style") is None for leaf in leaves)
    channel = SlackBotChannel(SlackConfig())
    payload = channel._payload(message)
    escaped = malicious.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert payload["text"] == (
        f"orca_auto: {escaped}\n&lt;!here&gt;: &lt;@U123&gt;\n{escaped}: {escaped}"
    )
    assert payload["blocks"] == render_slack_blocks(message)
    assert "attachments" not in payload
    assert payload["mrkdwn"] is False
    assert payload["parse"] == "none"
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False


def test_field_lines_are_not_duplicated_in_title_and_body() -> None:
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
    blocks_value = payload["blocks"]
    assert isinstance(blocks_value, list)
    blocks = cast(list[dict[str, Any]], blocks_value)
    blocks_blob = json.dumps(blocks)
    assert "Job:" in blocks_blob
    assert "Result:" in blocks_blob
    title_prefix = json.dumps(blocks[0]["elements"][0]["elements"][:3])
    assert "Job:" not in title_prefix
    assert "Result:" not in title_prefix
    fallback_text = payload["text"]
    assert isinstance(fallback_text, str)
    assert job in fallback_text


def test_no_extra_blocks_when_message_has_no_fields() -> None:
    message = Message(title="Ping", author="orca_auto")
    assert render_slack_blocks(message) == [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "ℹ️ "},
                        {"type": "text", "text": "Ping", "style": {"bold": True}},
                        {"type": "text", "text": " · orca_auto"},
                    ],
                }
            ],
        }
    ]
    payload = SlackBotChannel(SlackConfig())._payload(message)
    assert "attachments" not in payload
    blocks_value = payload["blocks"]
    assert isinstance(blocks_value, list)
    assert len(blocks_value) == 1


def test_text_limits_and_empty_title_remain_valid() -> None:
    message = Message(
        title="T" * 200,
        author="A" * 2100,
        groups=(group(field_row("L", text("v" * 4000))),),
    )
    blocks = render_slack_blocks(message)
    leaves = _section_leaves(blocks)
    assert leaves[1]["text"] == "T" * 146 + "…"
    assert leaves[1]["text"].startswith("T")
    assert _rendered_char_count(blocks) == 3000
    payload = SlackBotChannel(SlackConfig())._payload(message)
    assert len(str(payload["text"])) == 3000
    assert str(payload["text"]).endswith("…")
    blank_title = render_slack_blocks(Message(title="  ", author="  "))[0]
    assert _leaves(blank_title)[1] == {"type": "text", "text": "-", "style": {"bold": True}}


def test_long_author_does_not_replace_title_with_ellipsis() -> None:
    glyph = "ℹ️ "
    title = "Stable title"
    message = Message(title=title, author="A" * 5000)
    leaves = _section_leaves(render_slack_blocks(message))
    assert leaves[0] == {"type": "text", "text": glyph}
    assert leaves[1] == {"type": "text", "text": title, "style": {"bold": True}}
    assert leaves[2]["text"].startswith(" · ")
    assert leaves[2]["text"].endswith("…")


def test_visible_body_stops_at_character_and_leaf_budgets() -> None:
    message = Message(
        title="Event",
        groups=(group(*(field_row("L", text(str(index))) for index in range(500))),),
    )
    blocks = render_slack_blocks(message)
    leaves = _section_leaves(blocks)
    assert len(leaves) <= 200
    assert _rendered_char_count(blocks) <= 3000
    assert leaves[-1]["text"].endswith("…")


def test_body_omission_marks_remaining_rows_at_leaf_limit() -> None:
    message = Message(
        title="Event",
        groups=(group(*(field_row("L", text(index)) for index in range(240))),),
    )
    body = _plain_body(render_slack_blocks(message))
    assert body.endswith("…")
    assert "L: 0" in body
    assert "L: 119" not in body


def test_empty_spans_are_skipped_and_missing_values_show_dash() -> None:
    message = Message(
        title="Event",
        groups=(
            group(
                field_row("Empty", raw(""), text("")),
                field_row("Gap", raw("  "), text("ok")),
            ),
        ),
    )
    leaves = _section_leaves(render_slack_blocks(message))
    texts = [str(leaf.get("text", "")) for leaf in leaves]
    assert "Empty: " in "".join(texts)
    assert "-" in texts
    assert any(leaf.get("text") == "  " for leaf in leaves)
    assert all(leaf.get("text") != "" for leaf in leaves)


def test_exact_character_boundary_adds_ellipsis_before_later_spans() -> None:
    first_span = "x" * 2989
    second_span = "tail"
    message = Message(
        title="E",
        groups=(group(field_row("L", text(first_span), text(second_span))),),
    )
    leaves = _section_leaves(render_slack_blocks(message))
    combined = "".join(str(leaf.get("text", "")) for leaf in leaves)
    assert len(combined) == 3000
    assert combined.endswith("…")
    assert "tail" not in combined


def test_code_and_plain_styles_are_preserved() -> None:
    message = Message(
        title="Event",
        groups=(group(field_row("Path", code("/tmp/a"), raw(" tail"))),),
    )
    leaves = _section_leaves(render_slack_blocks(message))
    assert any(
        leaf == {"type": "text", "text": "/tmp/a", "style": {"code": True}} for leaf in leaves
    )
    assert any(leaf == {"type": "text", "text": " tail"} for leaf in leaves)


def test_title_field_two_raw_spans_fill_exact_3000_chars() -> None:
    x_block = "x" * 2991
    message = Message(
        title="T",
        groups=(group(field_row("L", raw(x_block), raw("Y"))),),
    )
    blocks = render_slack_blocks(message)
    combined = _plain_body(blocks)
    assert len(combined) == 3000
    assert not combined.endswith("…")
    assert combined.endswith("Y")
    assert len(_section_leaves(blocks)) <= 200


def test_exact_fit_single_span_without_following_content_is_not_marked() -> None:
    """Title + one field whose value is exactly the remaining budget: no ellipsis."""

    title_overhead = len("ℹ️ ") + len("E") + len("\n") + len("L: ")
    value = "x" * (3000 - title_overhead)
    message = Message(title="E", groups=(group(field_row("L", raw(value))),))
    blocks = render_slack_blocks(message)
    combined = _plain_body(blocks)
    assert len(combined) == 3000
    assert not combined.endswith("…")
    assert combined.endswith(value)


def test_2991_chars_then_Y_exact_fit_without_omission_marker() -> None:
    first = "x" * 2991
    message = Message(title="T", groups=(group(field_row("L", raw(first), raw("Y"))),))
    blocks = render_slack_blocks(message)
    combined = _plain_body(blocks)
    assert len(combined) == 3000
    assert not combined.endswith("…")
    assert combined.endswith("Y")


def test_2991_chars_then_Y_with_following_Z_marks_omission() -> None:
    first = "x" * 2991
    message = Message(
        title="T",
        groups=(group(field_row("L", raw(first), raw("Y"), raw("Z"))),),
    )
    blocks = render_slack_blocks(message)
    combined = _plain_body(blocks)
    assert len(combined) == 3000
    assert combined.endswith("…")
    assert "Z" not in combined


def test_exact_fit_row_label_and_group_separator_without_marker() -> None:
    message = Message(
        title="Head",
        groups=(
            group(field_row("Only", text("done"))),
            group(field_row("Next", text("later"))),
        ),
    )
    blocks = render_slack_blocks(message)
    body = _plain_body(blocks)
    assert body.endswith("Only: done\n\nNext: later")
    assert not body.endswith("…")
    assert _rendered_char_count(blocks) < 3000


def test_title_body_newline_with_one_char_remaining_marks_omission() -> None:
    message = Message(
        title="T" * 200,
        groups=(group(field_row("L", raw("f" * 2990), raw("z"))),),
    )
    blocks = render_slack_blocks(message)
    combined = _plain_body(blocks)
    assert len(combined) == 3000
    assert combined.endswith("…")


def test_author_suffix_exact_fit_does_not_false_mark() -> None:
    message = Message(title="Event", author="orca_auto")
    blocks = render_slack_blocks(message)
    plain = _plain_body(blocks)
    assert " · orca_auto" in plain
    assert not plain.endswith("…")


def test_two_hundredth_leaf_marks_following_rows() -> None:
    message = Message(
        title="E",
        groups=(group(*(field_row("L", text(str(index))) for index in range(240))),),
    )
    blocks = render_slack_blocks(message)
    leaves = _section_leaves(blocks)
    assert len(leaves) <= 200
    assert leaves[-1]["text"].endswith("…")
    assert _rendered_char_count(blocks) <= 3000


def test_trailing_empty_spans_do_not_false_mark() -> None:
    message = Message(
        title="E",
        groups=(group(field_row("L", text("ok"), raw(""), text(""))),),
    )
    blocks = render_slack_blocks(message)
    plain = _plain_body(blocks)
    assert plain.endswith("L: ok")
    assert not plain.endswith("…")


def test_one_character_budget_emits_ellipsis_not_silent_halt() -> None:
    overhead = len("ℹ️ ") + len("E") + len("\n") + len("L: ")
    first = "a" * (3000 - overhead - 1)
    message = Message(
        title="E",
        groups=(group(field_row("L", raw(first), raw("z"), raw("w"))),),
    )
    blocks = render_slack_blocks(message)
    combined = _plain_body(blocks)
    assert len(combined) == 3000
    assert combined.endswith("…")
    assert "w" not in combined


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
