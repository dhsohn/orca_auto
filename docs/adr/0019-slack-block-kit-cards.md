# ADR 0019: Slack notifications as Block Kit cards

- Status: Accepted
- Date: 2026-11-13
- Revised: 2026-11-15 (single native `rich_text` body; notification-only visible budgets)
- Supersedes: [ADR 0016's plain-text Slack rendering](0016-slack-notification-provider.md)

## Problem

[ADR 0016](0016-slack-notification-provider.md) posted every Slack notification
as one plain-text `text` value: an optional author prefix, the title and one
`Label: value` line per field. The Discord adapter renders the same
provider-neutral `Message` as an embed with a title, severity and grouped
fields, so a Slack channel showed the same lifecycle event as an undifferentiated
block of lines.

The first Block Kit revision put blocks at the top level and used a large
`header`, a separate author `context`, per-field `section` blocks and a colored
attachment. Review against compact reference layouts (Pulse/Note style) showed
that presentation was visually heavy and oversized, ignored `Span.style` code
runs, and split chrome from fields with excessive spacing.

## Decision

Keep `render_slack_text` unchanged: escaped plain summary with the existing
3000-character cap and `…` truncation. Replace chrome and attachments with one
native top-level `rich_text` block in `core/messaging/render_slack.py`:

- That block has a single `rich_text_section` of typed `text` leaves and
  literal newline leaves: severity glyph, **bold** title (150 characters
  including the glyph prefix, independent of `author`), a non-bold ` · author`
  suffix on the same line when present, a newline, then each field row in order.
  Blank lines between non-empty `Group`s are a literal `\n\n` newline run. Each
  row is **bold** `Label:` followed by typed leaves for each non-empty `Span`
  (`code` style for `Span(style="code")`, plain otherwise) or `-` when the value
  is empty. `Field.inline` is ignored on Slack; Discord keeps using it.
- Only `{"type":"text","text":...}` leaves appear; optional `style.bold` or
  `style.code`. No `mrkdwn`, links, mentions, emoji elements or invented buttons.
  Dynamic strings stay literal; raw spans keep significant whitespace without
  re-stripping. Empty span text is skipped rather than emitted as empty leaves.
- The native block body is bounded for notifications only: at most 3000 rendered
  characters and 200 typed `text` leaves. Rendering stops at the budget with
  bounded lookahead; omitted visible content is marked with `…` (same convention
  as other caps). This is not a delivery error and does not affect
  `render_slack_text`.

`SlackBotChannel` sends `text`, top-level `blocks` only (no `attachments`) in the
same `chat.postMessage` request, still with `mrkdwn: false`, `parse: none` and
link previews off.

Transport is unchanged: the fixed endpoint, Bearer token, redirect refusal,
delivery confirmation and the 429-only retry policy of ADR 0016. A Slack
rejection of `blocks` is an ordinary `"ok": false` failure; it is not retried and
no text-only message is sent instead.

This replaces only ADR 0016's plain-text rendering and the attachment/header card
layout above. Provider selection, configuration, `init`, claims, dispatch and the
queue are unchanged, and the Discord adapter and the provider-neutral `Message`
are untouched.

`rich_text` is Slack's supported block type for styled inline runs
([rich text block reference](https://docs.slack.dev/reference/block-kit/blocks/rich-text-block/)).
This does not promise Discord's exact frame or identical desktop/mobile rendering;
mobile Slack was not reviewed for this layout.

## Verification and limits

`tests/core/messaging/test_render_slack.py` covers severity glyphs (including
unknown severity), ignored `inline` hints, long paths and queue IDs, malicious
literal text in typed leaves only, field/group order and spacing, non-duplication
of field text in title vs body, messages without fields, text limits, author/title
independence, exact boundary ellipsis and the 3000-character / 200-leaf visible
budget.
The review fixtures `tests/fixtures/slack/queued-before.json` and
`queued-after.json` show the same synthetic queued event before and after the
correction; the after payload is checked by a test, and its escaped summary
matches the before payload.
`tests/core/messaging/test_messaging.py` checks that a posted request has
`rich_text` blocks and unchanged parsing/unfurl controls.
`tests/core/messaging/test_slack_lifecycle.py` fixes title and first-field
placement for each lifecycle notification loaded from a configuration file.
`test_slack_bot.py` also checks that rejection of `blocks` never causes a
text-only resend. The transport and queue-claim regression tests remain in
place. All run without network access.

Automated validation runs without network access. On 2026-11-15 a synthetic
lifecycle notification was accepted by the Slack API (`"ok": true`) and its
posted `rich_text` body was reviewed on desktop Slack: compact body-sized
**bold** title and labels, inline-code dynamic IDs and paths, no attachment bar
and no visible clipping. Mobile clients and notification-preview rendering were
not exercised.