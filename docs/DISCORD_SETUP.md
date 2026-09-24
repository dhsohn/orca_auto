# Discord Setup

**English** | [한국어](DISCORD_SETUP.ko.md)

ORCA_auto can send notifications to a Discord channel when jobs are queued and completed. The bot operates in outbound-only mode.

## 1. Create and invite the bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, add a bot, and copy its bot token.
2. Under **OAuth2 → URL Generator**, select the `bot` scope and grant the following permissions:
   - **View Channel**
   - **Send Messages**
   - **Embed Links**
3. Open the generated URL in your browser and invite the bot to your server.
4. Verify channel permission overrides to ensure the bot can send messages in the intended notification channel.

> **Security Note**: Treat your bot token like a password. Store it only in your local configuration file and never commit it to Git.

## 2. Copy the channel ID

In Discord, enable **User Settings → Advanced → Developer Mode**, right-click the notification channel, and select **Copy Channel ID**.

## 3. Configure ORCA_auto

Edit `~/orca_auto/config/orca_auto.yaml` (or the file passed with `--config` / `ORCA_AUTO_CONFIG`):

```yaml
messenger:
  provider: discord
  discord:
    bot_token: "YOUR_DISCORD_BOT_TOKEN"
    default_channel_id: "YOUR_CHANNEL_ID"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
```

Restrict file permissions for safety:

```bash
chmod 600 ~/orca_auto/config/orca_auto.yaml
```

You can also configure this interactively by running `orca_auto init`.

## 4. Apply changes and test

Restart the worker service to apply the configuration:

```bash
orca_auto service restart
orca_auto service status
```

Test notification delivery by submitting a job:

```bash
orca_auto run-dir <job_path>
```

A notification card will be sent when the job is queued, followed by a summary card upon completion.

## Troubleshooting

- **No notifications received:** Ensure the bot is added to your server and has `Send Messages` and `Embed Links` permissions in the target channel.
- **Invalid token error:** Regenerate the token in the Developer Portal, update `~/orca_auto/config/orca_auto.yaml`, and restart the service.
