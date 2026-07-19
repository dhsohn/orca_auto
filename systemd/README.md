# systemd assets

**English** | [한국어](README.ko.md)

This directory is the single home for long-running orca_auto service assets.

## Included units

- `orca_auto-runtime@.target`
  - recommended combined runtime target for the queue worker and selected messenger bot
- `orca_auto-queue-worker@.service`
  - default ORCA-only queue worker template
- `orca_auto-workflow-worker@.service`
  - explicit workflow supervisor plus internal xTB/CREST workers
- `orca_auto-bot@.service`
  - provider-neutral Telegram/Discord bot template

## Combined runtime target

Use `orca_auto-runtime@.target` when you want the ORCA queue worker and the
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

By default, both `orca_auto systemd install` and `orca_auto service restart`
replace the supervised build through a fail-closed drain gate. They snapshot
the managed unit states, stop only units that are active, and verify that all
four are non-running before
writing any unit file. Only then does install write the units, reload systemd,
disable the opposite boot mode, enable the selected mode, and start and verify
the selected runtime. Full mode requires the runtime target, queue worker, and
bot to become active; worker-only mode requires the queue worker. The workflow
unit is restarted and verified only when it was active before the drain. A
snapshot, stop, or non-running check failure aborts before any unit write. A fresh
install with no loaded units proceeds without a stop/reset command. A later
write, reload, boot-selection, start, or restore failure never restarts the old
processes; the command stops any partial new graph and leaves all managed units
stopped for an explicit repair and rerun.

Every non-dry-run install apply, `service restart`, and direct runtime
replacement for the same target user shares one EUID-independent,
non-persistent Linux abstract `AF_UNIX` socket lock within the same Linux
network namespace. The versioned target-user hash is held before restart mode
queries or drain and released by closing the socket only after start/restore
completes, so concurrent commands cannot observe an old mode and later start it
over a newer selection. Nested direct replacement reuses the same-thread
socket; other threads, processes, and caller EUIDs in that network namespace
serialize. A lock timeout aborts with exit status 1 before systemd mutation.
Dry-run and plan construction do not acquire the lock. Restart selection uses the
runtime target: exact active or enabled selects full mode, otherwise exact inactive/failed
plus disabled selects worker-only. Matching text with any other exit status fails closed.

All supported mutation callers—including WSL/native host shells and the
provided systemd units—must run in the same Linux network namespace when they
control the same host systemd. Controlling host systemd from a container or
separate network namespace is unsupported. Abstract socket names are
permissionless, so this feature requires a trusted-local-user or single-user
administrative boundary: an untrusted local user can pre-bind the name and
cause a fail-closed availability denial. The timeout occurs before mutation, so
such preemption cannot create a split-build graph or data damage. No file-lock
fallback is provided for this limitation.

`systemd install --no-start` changes only the boot selection, while
`--no-enable` only writes the units and reloads systemd; neither stops or starts
services. These maintenance modes are offline-only: before writing any unit
file, the installer requires the runtime target, queue worker, bot, and workflow
worker to each report a known non-running state (`inactive`, `failed`, or absent).
An active, transitional, or unqueryable unit aborts before any write. Boot-mode changes disable
the opposite mode before enabling the selected mode, so a later enable failure
cannot leave both modes enabled. `--no-start` validates the complete runtime
configuration before changing the boot selection. `--no-enable` may stage units
without a complete config because it does not select a boot mode.

The gate covers systemd units only. Before installing or restarting a changed
build, stop and drain every old-build foreground/manual `orca_auto` process as
well: queue and workflow workers, the bot, direct CLI commands, maintenance
commands, and upload handling. Verify that no old calculation or process
ownership remains before loading the new build.

An in-place checkout update happens before the new CLI can run, so it needs an
earlier drain than the install command itself. For an in-place deployment:

1. Using the old checkout, record whether the workflow unit is active.
2. Stop the runtime target, queue worker, bot, and workflow worker, and require
   every unit to report exactly `inactive`.
3. Only after that verification, update the checkout or installed package.
4. Run `orca_auto systemd install` from the new build.
5. If the workflow unit was active in step 1, start and verify it explicitly;
   the new installer sees the already stopped unit and cannot infer that prior
   state.

Alternatively, stage the new build in a separate immutable release directory
and run its installer while the old release is still intact; the pre-write
drain then preserves and restores the workflow snapshot automatically. Never
sync new code into a checkout still used by an old supervised process.

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

Use `orca_auto-queue-worker@.service` as the default worker service. It starts only the ORCA worker through:

- `python -m orca_auto.cli queue worker --app orca`

Common assumptions:

- Repository path is `/home/<user>/orca_auto`
- Config path is `/home/<user>/orca_auto/config/orca_auto.yaml`
- Python path is `/home/<user>/orca_auto/.venv/bin/python`
- The default service runs only the ORCA worker; ORCA uses the same
  shared admission lifecycle as internal engines, while keeping its ORCA
  retry/report behavior
- A configured `runs_root` never implicitly starts workflow, xTB, CREST, or xTB-MD workers

Start workflow supervision and its internal xTB/CREST workers only when needed:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
journalctl -u "orca_auto-workflow-worker@$(whoami)" -f
```

The workflow unit runs `queue worker --app workflow`, which explicitly expands
to the workflow supervisor and its xTB/CREST engine workers. It is installed but
is not pulled in by the default runtime target. Standalone xTB-MD likewise
requires an explicit `queue worker --app xtb_md` process.

Worker safety policy:

- supervised workers run in separate process sessions and initial starts are
  staggered by two seconds;
- a worker that exits three times within five minutes stops its
  supervisor instead of entering an unbounded child restart loop;
- engine workers' idle full-state reconciliation runs at most once per minute,
  independently of the short queue poll;
- the systemd unit uses `Restart=on-failure` with a 30-second delay and permits
  at most three unit starts in five minutes.

Install the ORCA engine worker:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

Use the worker-only service when you do not want an interactive bot managed by
systemd, or when the selected provider is incomplete. The installer chooses that mode
automatically when the bot is not fully configured.

Monitor the ORCA engine worker:

```bash
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

Maintain the ORCA engine worker:

```bash
orca_auto service restart
sudo systemctl stop "orca_auto-queue-worker@$(whoami)"
```

`scheduler.max_active_simulations` in `orca_auto.yaml` still caps the combined
number of active simulations across ORCA, internal xTB stages, and internal
CREST stages.
