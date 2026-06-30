# systemd assets

**English** | [한국어](README.ko.md)

This directory is the single home for long-running orca_auto service assets.

## Included units

- `orca_auto-runtime@.target`
  - recommended combined runtime target for the unified queue worker and Telegram bot
- `orca_auto-queue-worker@.service`
  - recommended unified queue worker template
- `orca_auto-bot@.service`
  - unified Telegram bot template

## Combined runtime target

Use `orca_auto-runtime@.target` when you want the unified queue worker and the
unified Telegram bot to start together at boot.

It pulls in:

- `orca_auto-queue-worker@.service`
- `orca_auto-bot@.service`

Before enabling the combined runtime target:

- Set `telegram.bot_token` and `telegram.chat_id` in `orca_auto.yaml`
- Set `workflow.root` in `orca_auto.yaml` if you want workflow supervision too
- Restrict local config permissions with `chmod 600 config/orca_auto.yaml`

Install the combined runtime target:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

The installer renders the unit files with the repository path, writes them to
`/etc/systemd/system`, runs `systemctl daemon-reload`, and enables/starts the
right runtime for the current config. If Telegram is not configured yet, it
enables only the queue worker; run the same command again after setting
`telegram.bot_token` and `telegram.chat_id` to enable the full runtime target.

Monitor the combined runtime target:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

Maintain the combined runtime target:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## Engine queue workers

Use `orca_auto-queue-worker@.service` as the default worker service. It starts the unified worker supervisor through:

- `python -m orca_auto.cli queue worker`

Common assumptions:

- Repository path is `/home/<user>/orca_auto`
- Config path is `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python path is `/home/<user>/orca_auto/.venv/bin/python`
- The unified service runs the ORCA worker by default; ORCA uses the same
  shared admission lifecycle as internal engines, while keeping its ORCA
  retry/report/auto-organize behavior
- If `workflow.root` is configured, the same service also starts workflow supervision and the internal CREST/xTB workers

Install the unified engine worker:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

Use the worker-only service when you do not want the Telegram bot managed by
systemd, or when Telegram is not configured yet. The installer chooses that
mode automatically while Telegram settings are empty.

Monitor the unified engine worker:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

Maintain the unified engine worker:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-queue-worker@$(whoami)"
```

`scheduler.max_active_simulations` in `orca_auto.yaml` still caps the combined
number of active simulations across ORCA, internal xTB stages, and internal
CREST stages.
