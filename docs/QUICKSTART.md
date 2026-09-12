# ORCA_auto Quickstart

**English** | [한국어](QUICKSTART.ko.md)

This guide is the shortest path from a fresh checkout to the supervised
ORCA_auto engine workers.

For release wheels and the Core / Core + Workflows choice, see
[package installation](INSTALLATION.md). The steps below use a source checkout.

## 1) Install

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

The bootstrap script creates `.venv`, installs the ORCA_auto core, and creates
`config/orca_auto.yaml` from the example template when needed.
Core-only is the default for a fresh environment. Reusing `.venv` does not
uninstall an already installed workflows extension; use a fresh environment
when you need a genuinely core-only profile.

Since 5.0.0, workflows are an optional
same-version distribution, not part of a default core installation. To include
them, use `bash scripts/bootstrap_wsl.sh --with-workflows` instead, or activate
the environment and run:

```bash
python -m pip install -e . -e ./extensions/workflows
```

The current development extension supports conformer screening only, with
CREST generation and ORCA refinement. For an
existing installation, use a fresh environment and the cutover guidance in
[RELEASE.md](RELEASE.md), including its TS-workflow removal warning. Do not
replace a running worker's source or environment.

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
The installer still reads `systemd/` from the checkout supplied by `--repo`;
installing either wheel alone does not deploy services. For a workflow submission,
install the matching workflows extension in the worker's environment, then
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
code the workers import, but only in an idle window. By default it refuses when
calculations are active/reserved or safety cannot be verified. Resolve the
diagnostic and retry; `orca_auto service restart --force` deliberately skips
this protection and can interrupt calculations. It does not wait for completion.
See the [Systemd Contract](PUBLIC_CONTRACTS.md#systemd-contract) for guard limits.

## 5) Submit Work

Place an ORCA `.inp` in a job directory under the configured `runs_root`, then submit:

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
