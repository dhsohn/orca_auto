# ADR 0019: Slack notifications as Block Kit cards

- Status: Accepted
- Date: 2026-11-13
- Supersedes: [ADR 0016's plain-text Slack rendering](0016-slack-notification-provider.md)

## Problem

[ADR 0016](0016-slack-notification-provider.md) posted every Slack notification
as one plain-text `text` value: an optional author prefix, the title and one
`Label: value` line per field. The Discord adapter renders the same
provider-neutral `Message` as an embed with a title, severity and grouped
fields, so a Slack channel showed the same lifecycle event as an undifferentiated
block of lines.

## Decision

Keep `render_slack_text` and add `render_slack_blocks` in
`core/messaging/render_slack.py`; `SlackBotChannel` sends both in the same
`chat.postMessage` request.

- `blocks` holds a `header` with the title, prefixed by ✅, ⚠️ or ❌ for the
  success, warning and error severities, a `context` block with the author when
  there is one, and `section` blocks for the field groups. A single-line field
  whose `label\nvalue` text fits 80 characters goes into a section's `fields`,
  at most ten per section; any other field gets its own section `text`.
- Every dynamic label and value is a `plain_text` composition object with
  `emoji: false`. `mrkdwn` is never used, so a message can still not mention,
  link or format. Text is stripped, an empty value becomes `-`, and the header
  (150), field (2000) and section (3000) lengths are capped with `…`. A message
  that would exceed 50 blocks ends with a `More: …` section.
- The top-level `text` is unchanged: the same escaped, capped plain text, now
  the fallback for notification previews and screen readers. The request still
  sets `mrkdwn: false` and `parse: none` and turns link previews off.
- Transport is unchanged: the fixed endpoint, Bearer token, redirect refusal,
  delivery confirmation and the 429-only retry policy of ADR 0016. A Slack
  rejection of the blocks is an ordinary `"ok": false` failure; it is not
  retried and no text-only message is sent instead.

This replaces only ADR 0016's plain-text rendering. Provider selection,
configuration, `init`, claims, dispatch and the queue are unchanged, and the
Discord adapter and the provider-neutral `Message` are untouched.

## Verification and limits

`tests/core/messaging/test_messaging.py` fixes the compact and wide field
layout, literal `<!channel>` and `#3` values in `plain_text`, the 50-block
budget with its `More: …` marker, the unchanged escaping of the fallback text
and the header and author blocks of a posted request.
`tests/core/messaging/test_slack_lifecycle.py` fixes the header, context and
first field for each lifecycle notification loaded from a configuration file.
All of these run without network access.

On Linux, `make check` passed with 4463 tests, 8 skipped, and 93.05% coverage;
`make check-packages` and `examples/fake_orca_smoke/run.sh` passed. The skipped
tests need a real ORCA engine; ORCA execution and output parsing did not change.
No live Slack request was sent during validation. Rendering on desktop, mobile
and notification previews, and live payload acceptance remain unverified.
