# systemd assets

**English** | [한국어](README.ko.md)

This directory is the single home for long-running orca_auto service assets.

## Included units

- `orca_auto-runtime@.target`
  - recommended combined runtime target for the queue worker and selected messenger bot
- `orca_auto-queue-worker@.service`
  - recommended unified queue worker template
- `orca_auto-bot@.service`
  - provider-neutral Telegram/Discord bot template

## Combined runtime target

Use `orca_auto-runtime@.target` when you want the unified queue worker and the
selected Telegram or Discord bot to start together at boot.

It pulls in:

- `orca_auto-queue-worker@.service`
- `orca_auto-bot@.service`

Before enabling the combined runtime target:

- Complete the selected provider's interactive credentials in `orca_auto.yaml`
- Restrict local config permissions with `chmod 600 config/orca_auto.yaml`

Install the combined runtime target:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

The installer renders the unit files with the repository path, writes them to
`/etc/systemd/system`, runs `systemctl daemon-reload`, and enables/starts the
right runtime for the current config. Telegram needs a token and chat ID. Discord needs
a separate bot token, at least one inbound command channel, and an operator user ID. When
the bot is not fully configured, the installer selects the queue worker only. Rerun the
command after completing bot configuration to enable the full runtime target.

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
  retry/report behavior
- The same service also starts workflow supervision and the internal CREST/xTB workers under the shared `runs_root`

Worker safety policy:

- supervised workers run in separate process sessions and initial starts are
  staggered by two seconds;
- a worker that exits three times within five minutes stops the unified
  supervisor instead of entering an unbounded child restart loop;
- engine workers' idle full-state reconciliation runs at most once per minute,
  independently of the short queue poll;
- the systemd unit uses `Restart=on-failure` with a 30-second delay and permits
  at most three unit starts in five minutes.

Install the unified engine worker:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

Use the worker-only service when you do not want an interactive bot managed by
systemd, or when the selected provider is incomplete. The installer chooses that mode
automatically when the bot is not fully configured.

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
