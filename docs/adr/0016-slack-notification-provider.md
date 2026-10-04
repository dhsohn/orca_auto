# ADR 0016: Slack as an additional notification provider

- Status: Proposed
- Date: 2026-10-02

## Problem

Outbound notifications could reach only Discord: `messenger.provider` accepted
`discord` alone and `build_channel` returned a Discord bot channel or a null
channel. An operator who uses Slack had no supported destination. Removing
Discord was considered and rejected by the owner; Discord stays and remains the
default.

## Decision

Add Slack as a second provider behind the existing `MessageChannel` seam.

- `messenger.provider: slack` with a `messenger.slack` section (`bot_token`,
  `default_channel_id`, `timeout_seconds`, `max_attempts`) selects
  `SlackBotChannel`. Only the selected provider's section may be present, and
  only its settings can enable delivery; an incomplete Slack section yields the
  null channel and never falls back to Discord. Adapters are imported only when
  selected.
- `render_slack_text` renders a message as plain text with `&`, `<` and `>`
  escaped and its length capped; the request turns Markdown, name linking and
  link previews off and is posted to the fixed URL
  `https://slack.com/api/chat.postMessage` with a Bearer token.
- A send is delivered only when the response has `"ok": true`, `channel` equal
  to the configured ID and a `ts` of digits, a dot and six digits. The exact
  channel match and the six-digit form are ORCA_auto's local fail-closed policy,
  not Slack guarantees.
- Only HTTP 429 with a well-formed `Retry-After` is retried, within
  `max_attempts` (at most `MAX_MESSENGER_ATTEMPTS`, 10) and
  `MAX_MESSENGER_RETRY_BACKOFF_SECONDS` (120 s) of total waiting. Network
  errors, timeouts, other statuses and `"ok": false` are not retried. Redirects
  are never followed; each request uses its own opener and the process-wide
  urllib opener is unchanged.
- `orca_auto init` asks about Discord first and offers Slack only when Discord
  is declined. Accepting Discord keeps the same prompts and answers. Declining
  Discord now needs one more answer for the Slack question (`n` keeps the
  disabled Discord default); an older scripted or piped sequence that ended
  with that `n` reaches end of input there and `init` stops with `Cancelled.`
  without writing.
- Claims, dispatch and lifecycle sites are unchanged and provider-neutral:
  notifications stay best effort with a claim before dispatch, and a crash, a
  failed send or a saturated sender can lose a message.

Rejected: a provider-first `init` prompt and a new `--messenger` flag (both
change existing input sequences or the CLI surface), retrying 5xx or network
failures (possible duplicate posts), an outbox or exactly-once delivery, and a
generic provider framework.

## Verification and limits

Network-free tests, with sockets refused, cover the config schema
(`tests/core/config/test_schema.py`), the adapter's confirmation, 429 and
failure handling (`tests/core/messaging/test_slack_bot.py`), redirect refusal
through urllib's real handlers (`tests/core/messaging/test_slack_bot_redirects.py`),
the public loader, resolver and lazy imports
(`tests/core/messaging/test_slack_lifecycle.py`), the queued and terminal claims
with real background dispatch (`tests/orca/queue/test_slack_notifications.py`)
and `init` (`tests/orca/test_init_slack.py`); Discord controls run in each.

No real Slack workspace, token or message has been used. Behavior against the
live Slack API, proxies and certificate handling is not tested. The Slack
decision itself is additive. It ships in 10.0.0, a major release because the
same release also carries the publicly breaking `completed` change of
[ADR 0012](0012-positive-scientific-completion-evidence.md); 10.0 as a whole is
not a compatible release. Slack's official protocol pages are linked from
`docs/SLACK_SETUP.md`, which separates them from ORCA_auto's local policy.
