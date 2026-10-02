# Slack Setup

**English** | [한국어](SLACK_SETUP.ko.md)

> **New in 10.0.0.** 9.0.x and earlier versions do not support Slack. Discord remains the default provider; see [Discord Setup](DISCORD_SETUP.md).

ORCA_auto can post a plain-text notification to one Slack channel when a job is queued, started and finished. Delivery is outbound only: ORCA_auto calls one Slack Web API method and accepts no commands from Slack.

## 1. Provision a bot token (owner action)

ORCA_auto does not provision Slack apps, tokens or authorizations, and it does not add scopes or channel access. It only uses the token you provide: `orca_auto init` writes that token to your local configuration file with mode `0600`. The workspace owner provisions the token once, outside ORCA_auto:

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
| Redirects | Never followed, to any host or scheme; a redirect is reported as `slack_http_<status>` and the token is not sent on |
| Confirmation | A send counts as delivered only when the response has `"ok": true`, `channel` equal to the configured ID and a `ts` of digits, a dot and six digits. This is ORCA_auto's own fail-closed check, not a Slack guarantee |
| Rate limits | Only HTTP 429 with a well-formed `Retry-After` is retried, after that wait, within `max_attempts` sends (at most 10) and 120 seconds of total waiting |
| Other failures | Network errors, timeouts, other HTTP statuses, `"ok": false` and unreadable responses are not retried, because a resend could post the message twice |
| Ownership | The queued and finished messages are claimed once in durable state before a bounded background sender delivers them; the started message is sent once by the worker child. A crash, a failed send or a full sender can lose a message, which is never resent. Delivery never changes job state |

Notifications are best effort, not exactly-once. Use `orca_auto queue list` and the generation reports to confirm calculation status.

## 5. Apply changes and test

After reviewing the configuration, restart the worker service on your own installation and submit a job. The restart guard refuses while the edited configuration is newer than the running worker; confirm the host is idle (`active_simulations: 0`) and then use `orca_auto service restart --force`, as [RELEASE](RELEASE.md) describes for configuration changes:

```bash
orca_auto service restart
orca_auto service status
orca_auto run-dir <job_path>
```

## 6. Before rolling back to 9.0.x

9.0.x rejects `messenger.provider: slack` and the `slack` section, so a 9.0.x worker does not start with this configuration. Before a rollback, and only with the owner's approval of the configuration change, return the `messenger` block to its Discord settings or to the disabled default. This guide does not make that change for you.

## Troubleshooting

Failures are logged as stable codes only, never the token, the message or the response body, for example `slack_bot_send_failed: slack_api_error`.

- **`slack_api_error`:** Slack did not answer `"ok": true`. Check that the app is in the channel and has `chat:write`.
- **`slack_invalid_response`:** The response did not confirm delivery to the configured channel. Check `default_channel_id`.
- **`slack_http_<status>`:** An HTTP error or a refused redirect. `slack_http_429` after the last attempt means the rate limit persisted.
- **`slack_network_error`:** The connection failed or timed out. It is not retried.
- **`slack_unconfirmed_delivery`:** Slack answered with a success status other than 200, so delivery was not confirmed. It is not retried.
- **`slack_request_error`:** The request could not be built, most often because the message text could not be encoded (for example a path that is not valid UTF-8). A token loaded from the configuration file has already passed validation, so this code alone does not mean the token is wrong. It is not retried.

## References

Slack's official protocol documentation:

- [`chat.postMessage` method](https://docs.slack.dev/reference/methods/chat.postMessage/)
- [Web API rate limits](https://docs.slack.dev/apis/web-api/rate-limits/)

ORCA_auto's own local policy, which is not taken from those pages: the fixed endpoint, never following redirects, the exact channel and six-digit `ts` confirmation, retrying only HTTP 429 within 10 attempts and 120 seconds of waiting, not retrying any other failure, and best-effort delivery with a claim before dispatch.
