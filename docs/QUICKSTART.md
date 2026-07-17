# orca_auto Quickstart

**English** | [한국어](QUICKSTART.ko.md)

This guide is the shortest path from a fresh checkout to a supervised
orca_auto queue worker.

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
Telegram notifications, set `messenger.telegram.bot_token` and
`messenger.telegram.chat_id` during init or edit `config/orca_auto.yaml` afterward.

## 3) Install The Runtime Service

```bash
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

If the selected Telegram or Discord provider has complete interactive bot settings,
orca_auto enables the full runtime target. Otherwise it enables only the queue worker.

## 4) Check Or Restart Services

```bash
orca_auto service status
orca_auto service restart
```

`service status` shows the runtime target, queue worker, and selected messenger bot.
`service restart` restarts the full runtime target when it is enabled; otherwise
it restarts the queue worker.

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
