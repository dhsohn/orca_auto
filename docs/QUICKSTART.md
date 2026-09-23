# Quickstart

**English** | [한국어](QUICKSTART.ko.md)

This guide walks you through configuring ORCA_auto, setting up the background worker service, submitting your first ORCA calculation, and inspecting the results.
If you have not installed the package yet, see the [Installation Guide](INSTALLATION.md).

---

## 1. Initialize Configuration

Create a configuration file (`orca_auto.yaml`) using the interactive wizard or default template:

```bash
# Create configuration in the default location or a specified path
orca_auto init --config ~/orca_auto.yaml
```

> **Key settings**:
> - Absolute path to your ORCA executable (`orca.executable`)
> - Root directory for calculation workspaces (`runs_root`)
> - Maximum concurrent simulations (`scheduler.max_active_simulations`)

---

## 2. Install and Start Background Services

Register and start the systemd worker service so calculations continue reliably even after the terminal session closes:

```bash
# Install and enable systemd units for the current user
orca_auto systemd install --user "$(id -un)" --config ~/orca_auto.yaml

# Check worker and runtime status
orca_auto service status --config ~/orca_auto.yaml
```

---

## 3. Prepare an Input Directory and Submit

Create a job directory below your configured `runs_root` and place your ORCA input file (`.inp`) along with any referenced coordinate files (`.xyz`, etc.). Resource limits (CPU cores `%pal` and memory `%maxcore`) are specified directly inside the `.inp` file:

```bash
# Submit the job directory to the queue (returns immediately upon enqueueing)
orca_auto run-dir ~/orca_runs/water --config ~/orca_auto.yaml
```

Once durably enqueued, the CLI returns immediately. The background worker evaluates host capacity and launches calculations in generation-isolated workspaces.

---

## 4. Monitor Queue and Logs

```bash
# List jobs in the queue (pending, running, completed)
orca_auto queue list --config ~/orca_auto.yaml

# Output structured JSON for automation
orca_auto queue list --config ~/orca_auto.yaml --json

# Follow live worker logs
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```

---

## 5. Cancel Jobs and Inspect Results

- **Cancel a job**: Safely cancel a pending or running calculation:
  ```bash
  orca_auto queue cancel <QUEUE_ID_OR_DIRECTORY> --config ~/orca_auto.yaml
  ```
- **Inspect results**: Upon completion, the job directory contains the raw ORCA output (`job.out`) and a structured observation artifact (`machine.json`) for downstream tools and automated reporting.
