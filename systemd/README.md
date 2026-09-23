# systemd Services

**English** | [한국어](README.ko.md)

This directory contains systemd unit templates and operational guidance for running ORCA_auto background workers.

## Unit Overview

- `orca_auto-runtime@.target`
  - Recommended top-level runtime target that manages background engine workers.
- `orca_auto-engine-workers@.target`
  - Worker target for ORCA queue execution.
- `orca_auto-queue-worker@.service`
  - Template service for the ORCA queue worker (`python -m orca_auto.cli queue worker --app orca`).
- `orca_auto-workflow-worker@.service`
  - Worker service for conformer screening and multi-stage workflows (xTB/CREST).

## Service Installation

From the repository root:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
```

This renders unit files to `/etc/systemd/system`, reloads systemd, and enables/starts `orca_auto-runtime@<user>.target`.

If you update the repository or edit unit templates, re-run the same command to apply changes.

### Enabling the Workflow Worker

To process multi-stage workflows such as conformer screening, install the `orca_auto_workflows` extension in your Python environment and start the workflow service:

```bash
sudo systemctl start "orca_auto-workflow-worker@$(whoami)"
```

## Monitoring and Maintenance

### Status and Logs

```bash
# Check status across all targets and workers
orca_auto service status

# Follow ORCA queue worker logs
journalctl -u "orca_auto-queue-worker@$(whoami)" -f

# Follow workflow worker logs
journalctl -u "orca_auto-workflow-worker@$(whoami)" -f
```

### Restart and Stop

```bash
# Safely restart workers (refused while calculations are active to prevent disruption)
orca_auto service restart

# Force restart immediately if required
orca_auto service restart --force

# Stop runtime services
sudo systemctl stop "orca_auto-runtime@$(whoami).target"
```

## Worker Policies

- **Automatic Restart**: Units use `Restart=on-failure` with a 30-second backoff (up to 3 restart attempts within 5 minutes).
- **Concurrency Guard**: `scheduler.max_active_simulations` in `config/orca_auto.yaml` limits total concurrent calculations across engines.
- **Safe Restart**: `orca_auto service restart` checks for running jobs and prompts or requires `--force` if jobs are actively computing.
