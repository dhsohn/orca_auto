# systemd assets

**English** | [한국어](README.ko.md)

This directory is the single home for long-running orca_auto service assets.

## Included units

- `orca_auto-runtime@.target`
  - recommended combined runtime target for the default engine workers and selected messenger bot
- `orca_auto-engine-workers@.target`
  - default worker-only target for the ORCA queue worker
- `orca_auto-queue-worker@.service`
  - ORCA queue worker template
- `orca_auto-workflow-worker@.service`
  - explicit workflow supervisor plus internal xTB/CREST workers
- `orca_auto-bot@.service`
  - provider-neutral Telegram/Discord bot template

## Combined runtime target

Use `orca_auto-runtime@.target` when you want the default ORCA queue worker and
the selected Telegram or Discord bot to start together at boot.

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

### Transaction recovery

The installer stages each update in
`/etc/systemd/system/.orca_auto-install-transaction` (or under the selected
`--unit-dir`). A later invocation for the same `--user` and unit directory sees
that shared transaction even when it is run from a different checkout.

- `owner.json` binds an in-progress transaction to a boot ID, PID, and process
  start time. If that owner is still live, malformed, missing, or cannot be
  verified, the installer exits with status 1 without changing units or
  stopping services. Resolve the owner uncertainty before retrying.
- `manifest.json` means rollback/recovery data is still pending. Keep the whole
  transaction directory, including `backup/` and the manifest. Rerun with the
  same user and unit directory after the prior owner is known to be gone. The
  automatic path proceeds only when the recorded owner is verifiably stale,
  such as after a boot-ID change or PID reuse; an unobservable process remains
  fail-closed. A safely classified transaction restores the previous unit
  files, boot selection, and exact active component set. An ambiguous
  `restart_pending` phase is deliberately preserved and does not stop a
  possibly external start.
- `committed.json` means the new installation was committed but transaction
  cleanup failed. The new units remain authoritative and the installer exits
  with status 1 so the cleanup problem is visible; do not treat this as a
  rollback or replace the marker with an older manifest.

Before any manual cleanup, inspect the preserved JSON and verify the unit files,
enablement, and active states it describes. Do not delete a pending manifest or
its backups merely to make a rerun proceed.

If and only if `manifest.json` says `restart_pending`, an operator must inspect
the recorded target, `systemctl show ... --property=ActiveState`, and its journal
to decide whether the recorded restart command ran. Then rerun the installer
from the same repository, with the same user and unit directory, using exactly
one resolution:

```bash
orca_auto systemd install --user "<same-user>" --repo "<same-repo>" \
  --unit-dir "<same-unit-dir>" --resolve-pending-restart applied
# or, only when the restart command definitely did not run:
orca_auto systemd install --user "<same-user>" --repo "<same-repo>" \
  --unit-dir "<same-unit-dir>" --resolve-pending-restart not-applied
```

`applied` durably records that the restart ran, so rollback may stop a target
that was inactive before the install and then restore the exact snapshot.
`not-applied` records that no restart occurred, so recovery does not attribute a
new start to the installer and verifies the original active set. Choosing the
wrong value can stop or misclassify a live service. The option does not override
a live or unverifiable owner, and it fails when the transaction is absent or is
not in `restart_pending`; never edit or delete the manifest to bypass those
checks.

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

Use `orca_auto-engine-workers@.target` as the default worker-only runtime. It
pulls in the ORCA queue worker:

- `orca_auto-queue-worker@.service` runs
  `python -m orca_auto.cli queue worker --app orca`

Common assumptions:

- Repository path is `/home/<user>/orca_auto`
- Config path is `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python path is `/home/<user>/orca_auto/.venv/bin/python`
- The default target runs exactly one ORCA worker with its own systemd restart
  circuit.
- The worker uses the shared admission lifecycle while keeping its own retry and
  report behavior.
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
- the queue-worker and workflow-worker units use `Restart=on-failure`; the bot
  uses `Restart=always` because an unexpected clean provider return must not
  leave the gateway inactive. All three wait 30 seconds and permit at most
  three unit starts in five minutes;
- `orca_auto service restart` clears the bounded failure state before an
  operator-requested restart.

Install the default ORCA engine worker:

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
```

Maintain the default engine workers:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-engine-workers@$(whoami).target"
```

`scheduler.max_active_simulations` in `orca_auto.yaml` still caps the combined
number of active simulations across ORCA, internal xTB stages, and internal
CREST stages.
