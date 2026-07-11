# Discord Setup

orca_auto uses one Discord bot for both outbound notifications and interactive
queue controls. Create a dedicated application for orca_auto; do not reuse the
`ollama_bot` token in two gateway processes.

## 1. Create and invite the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, add a bot, and copy its bot token.
2. On **Bot → Privileged Gateway Intents**, enable **Message Content Intent**.
   orca_auto currently parses `!list`, `!cancel`, and `!help` from ordinary
   channel messages, so the gateway needs message content. See Discord's
   [Gateway intent documentation](https://docs.discord.com/developers/events/gateway#message-content-intent).
3. On **OAuth2 → URL Generator**, select the `bot` scope and grant the minimum
   permissions needed in the selected channels:
   **View Channel**, **Send Messages**, **Read Message History**, and
   **Embed Links**. Open the generated URL and add the bot to the server.
4. Check channel-level permission overrides too. The bot must have those
   permissions in both the command channel and the notification channel.

Treat the bot token like a password. Store it only in the local config, keep
that file out of Git, and never paste the token into an issue, PR, or chat.

## 2. Copy IDs

Enable **User Settings → Advanced → Developer Mode** in Discord. Then use
**Copy Channel ID** on each channel. Discord's
[ID guide](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID)
also shows how to copy a user ID.

Choose the values as follows:

- `channel_ids`: channels where ordinary bot commands are accepted. This is
  an inbound allowlist and may contain more than one numeric ID.
- `default_channel_id`: destination for scheduled notifications and bot cards.
  It may be the same as a command channel or a separate notification-only
  channel. Buttons on cards sent there are accepted, but ordinary messages in
  that channel are ignored unless its ID is also in `channel_ids`.
- `allowed_user_ids`: required operator allowlist for commands and buttons.
  Discord channels are multi-user, so the gateway fails closed when this is
  empty rather than exposing queue cancellation to every channel member.

## 3. Configure orca_auto

Edit the active `orca_auto.yaml` (normally `config/orca_auto.yaml`):

You can also rerun `orca_auto init`; when it asks whether to keep the existing
messenger settings, answer **No**, then select `discord`.

```yaml
messenger:
  provider: discord
  discord:
    bot_token: "YOUR_ORCA_AUTO_BOT_TOKEN"
    channel_ids:
      - "COMMAND_CHANNEL_ID"
    default_channel_id: "NOTIFICATION_CHANNEL_ID"
    allowed_user_ids:
      - "YOUR_USER_ID"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
    webhook_url: ""  # legacy outbound-only fallback; leave empty for bot mode
```

Discord IDs must be quoted positive decimal strings. Protect the local config:

```bash
chmod 600 config/orca_auto.yaml
```

`bot_token` plus at least one `channel_ids` entry and a non-empty
`allowed_user_ids` list enables the gateway. `bot_token` plus
`default_channel_id` enables bot-authenticated notifications even without the
gateway. A webhook alone can send legacy notifications but cannot
receive commands or component interactions, so it does not enable the bot
service.

## 4. Install and verify the service

From the repository root:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
.venv/bin/orca_auto service restart
.venv/bin/orca_auto service status
```

For a foreground smoke test instead:

```bash
.venv/bin/orca_auto bot run --provider discord
```

After the gateway reports that it is ready, test in an allowed command channel:

```text
!help
!list
!cancel TARGET
```

Cancellation always requires a second button confirmation. Action IDs are
short-lived, single-use, and bound to the originating provider, channel, and
user.

## 5. Architecture and future notification controls

The Telegram and Discord adapters translate native events into the same
`IncomingCommand`/`IncomingAction` values. Shared application logic returns a
`BotReply` with provider-neutral `CardAction` rows; each adapter renders native
buttons. Scheduled notifications already use the same selected provider and
Discord bot identity through the REST notification adapter.

Notifications do not yet include action buttons. Adding them later should
extend the shared card/action application path rather than add Discord-only
domain logic. Because the queue worker and gateway are separate systemd
processes, notification-origin actions will also need a durable `ActionStore`
implementation; the current port has originator/operator audience policies but
its short-lived in-memory implementation intentionally covers gateway-generated
command cards only.

## Troubleshooting

- **Bot is absent from the server:** regenerate/open the OAuth2 bot invite and
  select the intended server. A token alone does not add a bot to a server.
- **Bot is online but ignores commands:** verify Message Content Intent,
  `channel_ids`, `allowed_user_ids`, and channel permission overrides.
- **Notifications do not arrive:** verify `default_channel_id`, **Send Messages**,
  and **Embed Links** in that channel.
- **Only the queue worker starts:** rerun `systemd install` after completing the
  bot token, channel, and operator-user configuration; webhook-only mode is
  intentionally worker-only.
- **Discord reports an invalid token or privileged-intent close:** reset the
  token if necessary and enable Message Content Intent before restarting.
