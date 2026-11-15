# Slack Setup

**English** | [한국어](SLACK_SETUP.ko.md)

> **New in 10.0.0.** 9.0.x and earlier versions do not support Slack. Discord remains the default provider; see [Discord Setup](DISCORD_SETUP.md).

ORCA_auto can post a card to one Slack channel when a job is queued, started or finished. Each card shows the event and job details, with a plain-text summary for notification previews and screen readers. Delivery is outbound only: ORCA_auto calls one Slack Web API method and accepts no commands from Slack.

## 1. Provision a bot token (owner action)

Create a Slack app in your workspace and supply its bot token (`orca_auto init` writes the config with mode `0600`):

1. Create a Slack app for the workspace and add the bot token scope `chat:write`.
2. Install the app to the workspace and copy its bot token (it starts with `xoxb-`).
3. Invite the app to the notification channel.

> **Security Note**: The bot token can post as the app in every channel the app joins. Store it only in your local configuration file, never in Git, logs or issue reports.

## 2. Copy the channel ID

Use the channel's ID (for example `C0123456789`), not its name. ORCA_auto accepts only an ID that starts with `C`, `G` or `D` followed by 2 to 31 upper-case letters or digits.

## 3. Configure ORCA_auto

Edit `~/orca_auto/config/orca_auto.yaml` (or the file passed with `--config` / `ORCA_AUTO_CONFIG`). The `messenger` block may hold only the selected provider's section, so replace a `discord` section instead of adding `slack` next to it:

```yaml
messenger:
  provider: slack
  slack:
    bot_token: "YOUR_SLACK_BOT_TOKEN"
    default_channel_id: "YOUR_CHANNEL_ID"
    timeout_seconds: 5.0
    max_attempts: 1
```

Restrict file permissions:

```bash
chmod 600 ~/orca_auto/config/orca_auto.yaml
```

`orca_auto init` asks about Discord first. When you decline Discord it offers Slack, reads the token without echoing it and writes the file with mode `0600`. A blank token or channel ID disables delivery.

## 4. Delivery behavior

| Behavior | What ORCA_auto does |
| :--- | :--- |
| Endpoint | Posts JSON to the fixed URL `https://slack.com/api/chat.postMessage`; the configured channel is the only destination |
| Message shape | Top-level Block Kit header and author context, plus one colored attachment with job fields when present; escaped top-level `text` is the notification and screen-reader fallback |
| Redirects | Never followed, to any host or scheme; a redirect is reported as `slack_http_<status>` and the token is not sent on |
| Confirmation | A send counts as delivered only when the response has `"ok": true`, `channel` equal to the configured ID and a `ts` of digits, a dot and six digits |
| Rate limits | Only HTTP 429 with a well-formed `Retry-After` is retried, after that wait, within `max_attempts` sends (at most 10) and 120 seconds of total waiting |
| Other failures | Network errors, timeouts, other HTTP statuses, `"ok": false` and unreadable responses are not retried, because a resend could post the message twice |
| Ownership | The queued and finished messages are claimed once in durable state before a bounded background sender delivers them; the started message is sent once by the worker child. A crash, a failed send or a full sender can lose a message, which is never resent. Delivery never changes job state |

Notifications are best effort, not exactly-once. Use `orca_auto queue list` and the generation reports to confirm calculation status.

The header and optional author sit in top-level `blocks`; field sections live in a single attachment with a left color bar (blue for info and unknown severity, green for success, amber for warning, red for error). The heading adds ℹ️, ✅, ⚠️ or ❌ respectively. Messages without fields omit the attachment. Dynamic labels and values use only `plain_text` with `emoji: false`; Markdown, mentions and link parsing remain disabled. Fields keep their order. Only an `inline=True` field whose `label: value` is a single line of at most 40 characters can share two columns; all other fields use full-width `Label: value` rows, including long job names, queue IDs and directories. Chrome and attachment blocks share the 50-block budget; omitted content is marked with `…`.

This explicitly uses Slack's native attachment wrapper because the [official migration guide](https://docs.slack.dev/legacy/legacy-messaging/migrating-outmoded-message-compositions-to-blocks/) recommends `attachment.blocks` when a color bar is needed. [Attachments are a legacy Slack feature](https://docs.slack.dev/legacy/legacy-messaging/legacy-secondary-message-attachments/) with lower visual priority; this choice does not promise an identical Discord frame or rendering across clients. Payload tests cover the structure, escaping and limits. The corrected card's live Slack acceptance, desktop/mobile appearance and notification previews remain unverified; see [ADR 0019](adr/0019-slack-block-kit-cards.md).

## 5. Apply changes and test

Restart the worker service and submit a job. Because editing the configuration makes it newer than the running worker, confirm the host is idle (`active_simulations: 0`) and run `orca_auto service restart --force` (see [RELEASE](RELEASE.md)):

```bash
orca_auto service restart
orca_auto service status
orca_auto run-dir <job_path>
```

## 6. Before rolling back to 9.0.x

9.0.x rejects `messenger.provider: slack` and the `slack` section. Before rolling back to 9.0.x, revert the `messenger` block to Discord settings or the disabled default.

## Troubleshooting

Failures are logged as stable codes only, never the token, the message or the response body, for example `slack_bot_send_failed: slack_api_error`.

- **`slack_api_error`:** Slack did not answer `"ok": true`. Check that the app is in the channel and has `chat:write`.
- **`slack_invalid_response`:** The response did not confirm delivery to the configured channel. Check `default_channel_id`.
- **`slack_http_<status>`:** An HTTP error or a refused redirect. `slack_http_429` after the last attempt means the rate limit persisted.
- **`slack_network_error`:** The connection failed or timed out. Not retried.
- **`slack_unconfirmed_delivery`:** Slack answered with a success status other than 200, so delivery was not confirmed. Not retried.
- **`slack_request_error`:** The request could not be built, usually because message text (such as a path) is not valid UTF-8; tokens in the configuration file are validated at load time. Not retried.

## References

- [`chat.postMessage` method](https://docs.slack.dev/reference/methods/chat.postMessage/)
- [Web API rate limits](https://docs.slack.dev/apis/web-api/rate-limits/)
