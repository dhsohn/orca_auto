# ORCA_auto Quickstart

**English** | [한국어](QUICKSTART.ko.md)

This guide walks you through setting up ORCA_auto workers from source and submitting your first calculation.

For release wheels and installation profiles, see [package installation](INSTALLATION.md). The steps below use a source checkout.

## 1) Install

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

The bootstrap script creates `.venv`, installs the ORCA_auto core, and sets up `config/orca_auto.yaml` from the example template.

To include the conformer screening workflow extension:

```bash
# Using the bootstrap script
bash scripts/bootstrap_wsl.sh --with-workflows

# Or install manually in an active venv
python -m pip install -e . -e ./extensions/workflows
```

## 2) Configure

```bash
orca_auto init
```

Use absolute Linux paths for ORCA, xTB, CREST, and run directories. If you want Discord notifications, enter the bot token and channel ID during init or edit `config/orca_auto.yaml` afterward.

## 3) Install Systemd Runtime Services

```bash
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

This command enables the systemd runtime target and starts the ORCA engine worker service.
(The installer reads the unit templates from `systemd/` in the specified repository.)

To run workflow jobs (conformer screening), start the workflow worker service as well:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
```

## 4) Check or Restart Services

```bash
orca_auto service status
orca_auto service restart
```

- `service status`: Shows the state of runtime targets, ORCA engine workers, and workflow workers.
- `service restart`: Restarts the runtime target and worker services safely. By default, restart is refused if calculations are active to prevent data loss. (Pass `--force` if you need to force an immediate restart.)

## 5) Submit Work

Place an ORCA input file (`.inp`) in a job directory under the configured `runs_root`, then submit:

```bash
orca_auto run-dir '/home/user/orca_runs/sample_rxn'
```

`run-dir` queues work safely. Once submitted, you can safely close the terminal; the systemd worker handles execution in the background.

## 6) Watch the Queue

```bash
# View all queued and recent jobs
orca_auto queue list

# View ORCA jobs only
orca_auto queue list --engine orca

# Cancel a job
orca_auto queue cancel <target>

# Clear completed, failed, or cancelled entries
orca_auto queue list clear
```

## Troubleshooting

```bash
orca_auto service status
orca_auto service restart
orca_auto queue list --refresh
```

For advanced service management and logs, see [systemd service documentation](../systemd/README.md).
