# ADR 0019: Slack notifications as Block Kit cards

- Status: Accepted
- Date: 2026-11-13
- Revised: 2026-11-14 (split chrome and attachment field blocks)
- Supersedes: [ADR 0016's plain-text Slack rendering](0016-slack-notification-provider.md)

## Problem

[ADR 0016](0016-slack-notification-provider.md) posted every Slack notification
as one plain-text `text` value: an optional author prefix, the title and one
`Label: value` line per field. The Discord adapter renders the same
provider-neutral `Message` as an embed with a title, severity and grouped
fields, so a Slack channel showed the same lifecycle event as an undifferentiated
block of lines.

The first Block Kit revision put blocks at the top level and ignored
`Field.inline`. A supplied Slack desktop screenshot of the 00:19 queued and
started notifications showed headings and context but no colored card boundary;
long job names, queue IDs and directories wrapped in narrow columns.

Review identified a duplication risk in a proposed attachment-only payload:
without top-level `blocks`, Slack can treat the full escaped `text` as the
visible message body alongside the card. This was a contract review finding,
not an observed live Slack rendering.

## Decision

Keep `render_slack_text` unchanged. Split rendering in
`core/messaging/render_slack.py`:

- Top-level `blocks` hold the severity header (with ℹ️, ✅, ⚠️ or ❌) and an
  optional author `context` block only.
- Exactly one attachment, when the message has field sections, carries the
  severity `color` and those field `section` blocks only. Messages with no fields
  omit `attachments` entirely.
- The 50-block budget applies to top-level chrome plus attachment sections
  combined; overflow ends with a `More: …` section inside the attachment.

`SlackBotChannel` sends `text`, top-level `blocks`, and optionally one
attachment in the same `chat.postMessage` request.

- The attachment's `color` is blue `#3498DB` for info, green `#2ECC71` for
  success, amber `#F39C12` for warning and red `#E74C3C` for error. Unknown
  severities use the info style.
- Only fields with `inline=True`, a single-line label and value, and at most
  40 characters in `label: value` go into a section's two-column `fields`
  (as `label\nvalue`, at most ten fields per section). Other fields use a
  full-width section `text` as `Label: value`. Field and group order are
  preserved.
- Every dynamic label and value is a `plain_text` composition object with
  `emoji: false`. `mrkdwn` is never used. Text objects are stripped, empty text
  becomes `-`, and the header (150), field (2000) and section (3000) lengths are
  capped with `…`.
- Top-level `text` stays the full escaped, capped plain summary. Because
  top-level `blocks` are present, Slack treats it as the notification and
  screen-reader fallback, not the visible body. The request still sets
  `mrkdwn: false` and `parse: none` and turns link previews off.
- Transport is unchanged: the fixed endpoint, Bearer token, redirect refusal,
  delivery confirmation and the 429-only retry policy of ADR 0016. A Slack
  rejection of the blocks or attachment is an ordinary `"ok": false` failure; it
  is not retried and no text-only message is sent instead.

This replaces only ADR 0016's plain-text rendering. Provider selection,
configuration, `init`, claims, dispatch and the queue are unchanged, and the
Discord adapter and the provider-neutral `Message` are untouched.

The attachment wrapper is an explicit Slack presentation choice. Slack's
[migration guide](https://docs.slack.dev/legacy/legacy-messaging/migrating-outmoded-message-compositions-to-blocks/)
directs color-bar users to `blocks` within an attachment; its
[attachment reference](https://docs.slack.dev/legacy/legacy-messaging/legacy-secondary-message-attachments/)
documents `color` as the left border and `blocks` as supported attachment
content. Attachments remain a legacy feature with lower visual priority. This
depends on that documented wrapper, not on legacy attachment text/fields,
`rich_text`, or untrusted `mrkdwn`, and does not promise Discord's exact frame
or identical desktop/mobile rendering (live UI unverified).

## Verification and limits

`tests/core/messaging/test_render_slack.py` covers severity colors and glyphs
(including unknown severity), inline hints and the 40-character boundary,
long values without label exceptions, malicious text, field/group order,
non-duplication of field text between chrome and attachment, messages without
fields (no attachment), text limits and the ten-field/50-block boundaries.
The review fixtures `tests/fixtures/slack/queued-before.json` and
`queued-after.json` show the same synthetic queued event before and after the
correction; the after payload is checked by a test, and its escaped summary
matches the before payload.
`tests/core/messaging/test_messaging.py` checks that a posted request has
top-level chrome `blocks`, one attachment with field sections when present,
and unchanged parsing/unfurl controls.
`tests/core/messaging/test_slack_lifecycle.py` fixes the header, context and
first field placement for each lifecycle notification loaded from a
configuration file. `test_slack_bot.py` also checks that rejection of an
attachment or its blocks never causes a text-only resend. The transport and
queue-claim regression tests remain in place. All run without network access.

For the initial top-level-block revision on Linux, `make check` passed with
4463 tests, 8 skipped, and 93.05% coverage;
`make check-packages` and `examples/fake_orca_smoke/run.sh` passed. The skipped
tests need a real ORCA engine; ORCA execution and output parsing did not change.
No live Slack request was sent during validation. Rendering on desktop, mobile
and notification previews, and live payload acceptance remain unverified.
