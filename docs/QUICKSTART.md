# orca_auto Quickstart

**English** | [한국어](QUICKSTART.ko.md)

This guide is the shortest path from a fresh checkout to the supervised
orca_auto engine workers.

## 1) Install

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

The bootstrap script creates `.venv`, installs orca_auto, and creates
`config/orca_auto.yaml` from the example template when needed.

## 2) Configure

```bash
orca_auto init
```

Use absolute Linux paths for ORCA, xTB, CREST, and run directories. If you want
Discord notifications, set `messenger.discord.bot_token` and
`messenger.discord.default_channel_id` during init or edit `config/orca_auto.yaml` afterward.

## 3) Install The Runtime Service

```bash
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

This enables the runtime target, which starts the ORCA engine service.
For a workflow submission,
start the opt-in workflow unit before or after queueing it:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
```

## 4) Check Or Restart Services

```bash
orca_auto service status
orca_auto service restart
```

`service status` shows the runtime and engine-worker targets, the default ORCA
engine service, and the opt-in workflow service.
`service restart` restarts the runtime target and then the worker services
themselves, including the workflow worker when it is already running — a target
restart on its own leaves their processes up. Run it after a deploy that touches
code the workers import, but only in an idle window: restarting a worker stops
the ORCA process it is supervising.

## 5) Submit Work

```bash
orca_auto run-dir '/home/user/orca_runs/sample_rxn'
```

`run-dir` queues work durably. Closing the terminal after a successful queue
submission is safe because the systemd worker performs the actual execution.
For ORCA, the worker executes the queued entry by queue id; the job's
`reaction_dir` remains recorded in the queue and reports, but it is not the
worker-child command identity.

## 6) Watch The Queue

```bash
orca_auto queue list
orca_auto queue list --engine orca
orca_auto queue cancel <target>
```

Use `orca_auto queue list clear` when you want to prune completed, failed, and
cancelled entries from the unified activity list.

## Troubleshooting

```bash
orca_auto service status
orca_auto service restart
orca_auto queue list --refresh
```

If a service still does not behave as expected, use the deeper systemd commands
in `systemd/README.md`.
