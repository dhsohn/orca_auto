# systemd assets

**English** | [한국어](README.ko.md)

This directory is the single home for long-running orca_auto service assets.

## Included units

- `orca_auto-runtime@.target`
  - recommended runtime target that supervises the default engine workers
- `orca_auto-engine-workers@.target`
  - default worker-only target for the ORCA queue worker
- `orca_auto-queue-worker@.service`
  - ORCA queue worker template
- `orca_auto-workflow-worker@.service`
  - explicit workflow supervisor plus internal xTB/CREST workers

## Runtime target

Use `orca_auto-runtime@.target` as the recommended runtime target. It supervises
the default ORCA queue worker at boot. Outbound notifications are delivered
fire-and-forget by the engine workers themselves, so there is no separate bot
service.

It pulls in:

- `orca_auto-engine-workers@.target`

Before enabling the runtime target:

- Restrict local config permissions with `chmod 600 config/orca_auto.yaml`
  and `chmod 700 config` (a world-writable directory lets any local account
  replace the file, and with it the configured engine executables)

Install the runtime target:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

The installer renders the unit files with the repository path, writes them to
`/etc/systemd/system`, runs `systemctl daemon-reload`, and enables/starts the
runtime target (or the engine-worker target with `--worker-only`). Literal `%`
in rendered data paths is escaped; paths containing quotes, backslashes, or
dollar signs are rejected before any unit is written.

After updating the checkout or editing any unit template in this directory,
rerun the installer with the same `--user` and `--repo` values. The installed
units are rendered copies under `/etc/systemd/system`; `systemctl daemon-reload`
alone does not copy template changes or install newly added units. The
installer's target restart does not restart workers that are already running;
run `orca_auto service restart` in an idle window afterwards so they import
the updated checkout (`orca_auto service status` reports them as stale until
then).

### Failed installs

The installer writes each rendered unit file into place and then runs the
`systemctl` transition commands in order. If a command fails, the installer
stops with that command's exit status; the new unit files are already in place
and no rollback is attempted. Fix the reported failure and rerun the installer
with the same `--user` and `--repo` values — every step is idempotent.

Monitor the runtime target:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

Maintain the runtime target:

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
- the queue-worker and workflow-worker units use `Restart=on-failure`; both wait
  30 seconds and permit at most three unit starts in five minutes;
- `orca_auto service restart` clears the bounded failure state and then
  restarts the worker services themselves, so it ends in-flight ORCA work:
  run it in an idle window. A worker whose restart fails is left stopped
  rather than running stale code, and an unreadable workflow-worker state
  changes nothing and exits non-zero.
- supervised workers record their resolved package import source during the
  startup exec. `service status` binds that evidence to PID/start ticks and
  checks a fresh Git HEAD reflog and imported-package cleanliness snapshot per
  worker; process cwd is not source evidence. Workers from an older release or
  a package tree with uncommitted source changes report `undetermined`.

Install the default ORCA engine worker:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

Use the worker-only target with `--worker-only` to enable
`orca_auto-engine-workers@.target` directly instead of the runtime target.

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
