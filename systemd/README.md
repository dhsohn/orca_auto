# systemd assets

**English** | [한국어](README.ko.md)

This directory is the single home for long-running orca_auto service assets.

## Included units

- `orca_auto-runtime@.target`
  - recommended combined runtime target for the default engine workers and selected messenger bot
- `orca_auto-engine-workers@.target`
  - default worker-only target for independent ORCA and standalone xTB-MD services
- `orca_auto-queue-worker@.service`
  - ORCA queue worker template
- `orca_auto-xtb-md-worker@.service`
  - standalone xTB-MD queue worker template
- `orca_auto-workflow-worker@.service`
  - explicit workflow supervisor plus internal xTB/CREST workers
- `orca_auto-bot@.service`
  - provider-neutral Telegram/Discord bot template

## Combined runtime target

Use `orca_auto-runtime@.target` when you want the default ORCA/standalone
xTB-MD queue workers and the selected Telegram or Discord bot to start together
at boot.

It pulls in:

- `orca_auto-engine-workers@.target`
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
the bot is not fully configured, the installer selects the bot-free engine-worker target. Rerun the
command after completing bot configuration to enable the full runtime target.

After updating the checkout or editing any unit template in this directory,
rerun the installer with the same `--user` and `--repo` values. The installed
units are rendered copies under `/etc/systemd/system`; `systemctl daemon-reload`
alone does not copy template changes or install newly added units.

Monitor the combined runtime target:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-xtb-md-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

Maintain the combined runtime target:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## Engine queue workers

Use `orca_auto-engine-workers@.target` as the default worker-only runtime. It
pulls in two independent services:

- `orca_auto-queue-worker@.service` runs
  `python -m orca_auto.cli queue worker --app orca`
- `orca_auto-xtb-md-worker@.service` runs
  `python -m orca_auto.cli queue worker --app xtb_md`

Common assumptions:

- Repository path is `/home/<user>/orca_auto`
- Config path is `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python path is `/home/<user>/orca_auto/.venv/bin/python`
- The default target runs exactly one ORCA worker and one standalone xTB-MD
  worker. Each has its own systemd restart circuit, so a failed xTB-MD service
  cannot stop the ORCA service, and vice versa.
- Both workers use the shared admission lifecycle while keeping their own retry
  and report behavior.
- A configured `runs_root` never implicitly starts workflow, internal xTB, or
  CREST workers.

Start workflow supervision and its internal xTB/CREST workers only when needed:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
journalctl -u "orca_auto-workflow-worker@$(whoami)" -f
```

The workflow unit runs `queue worker --app workflow`, which explicitly expands
to the workflow supervisor and its xTB/CREST engine workers. It is installed but
is not pulled in by the default runtime target.

Worker safety policy:

- each engine service owns one worker supervisor and its child process session;
- a worker that exits three times within five minutes stops its
  supervisor instead of entering an unbounded child restart loop;
- engine workers' idle full-state reconciliation runs at most once per minute,
  independently of the short queue poll;
- the queue-worker, workflow-worker, and bot units use `Restart=on-failure`
  with a 30-second delay and permit at most three unit starts in five minutes;
- `orca_auto service restart` clears the bounded failure state before an
  operator-requested restart.

Install the default ORCA and standalone xTB-MD engine workers:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

Use the worker-only target when you do not want an interactive bot managed by
systemd, or when the selected provider is incomplete. The installer chooses that mode
automatically when the bot is not fully configured.

Monitor the default engine workers:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-xtb-md-worker@$(whoami)" -f
```

Maintain the default engine workers:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-engine-workers@$(whoami).target"
```

`scheduler.max_active_simulations` in `orca_auto.yaml` still caps the combined
number of active simulations across ORCA, standalone xTB-MD, internal xTB
stages, and internal CREST stages.
