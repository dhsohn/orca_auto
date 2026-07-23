# Discord Setup

orca_auto sends one-way outbound notifications to a Discord channel using a bot
token. The bot only posts messages; it does not read channel messages or accept
interactive commands. Create a dedicated application for orca_auto; do not reuse
the `ollama_bot` token.

## 1. Create and invite the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, add a bot, and copy its bot token.
2. On **OAuth2 → URL Generator**, select the `bot` scope and grant the minimum
   permissions needed in the notification channel:
   **View Channel**, **Send Messages**, and **Embed Links**. Open the generated
   URL and add the bot to the server.
3. Check channel-level permission overrides too. The bot must have those
   permissions in the notification channel.

No privileged gateway intents are required. orca_auto only posts messages
through the authenticated REST API, so the bot never needs Message Content
Intent or message-read permissions.

Treat the bot token like a password. Store it only in the local config, keep
that file out of Git, and never paste the token into an issue, PR, or chat.

## 2. Copy the channel ID

Enable **User Settings → Advanced → Developer Mode** in Discord. Then use
**Copy Channel ID** on the notification channel. Discord's
[ID guide](https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID)
shows where the control is.

- `default_channel_id`: destination for scheduled and worker notifications.

## 3. Configure orca_auto

Edit the active `orca_auto.yaml` (normally `config/orca_auto.yaml`):

You can also rerun `orca_auto init`; when it asks whether to keep the existing
messenger settings, answer **No** and enter the Discord bot values it prompts
for. The provider is always `discord`, so there is no provider prompt.

```yaml
messenger:
  provider: discord
  discord:
    bot_token: "YOUR_ORCA_AUTO_BOT_TOKEN"
    default_channel_id: "NOTIFICATION_CHANNEL_ID"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
```

Discord IDs must be quoted positive decimal strings. Protect the local config:

```bash
chmod 600 config/orca_auto.yaml
```

`bot_token` plus `default_channel_id` enables bot-authenticated outbound
notifications. Leaving either empty disables delivery.

## 4. Install and verify

From the repository root:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
.venv/bin/orca_auto service restart
.venv/bin/orca_auto service status
```

orca_auto posts a notification when a run reaches a terminal state. To confirm
delivery without waiting for a job, run a single scan:

```bash
.venv/bin/orca_auto scan-notify
```

Then check the notification channel for the message card.

## Troubleshooting

- **Bot is absent from the server:** regenerate/open the OAuth2 bot invite and
  select the intended server. A token alone does not add a bot to a server.
- **Notifications do not arrive:** verify `bot_token`, `default_channel_id`, and
  **Send Messages** and **Embed Links** in that channel.
- **Discord reports an invalid token:** reset the token if necessary and restart
  the service.
