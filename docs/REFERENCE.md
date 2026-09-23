# ORCA_auto Reference Guide

**English** | [한국어](REFERENCE.ko.md)

Detailed reference guide for ORCA_auto CLI commands, configuration schema, job lifecycles, and output artifacts.

---

## 1. CLI Command Reference

The unified CLI entrypoint is `orca_auto`. Use `--json` for automation and scripting.

| Command | Description | Key Options |
|---|---|---|
| `orca_auto init` | Interactively generate or update `config/orca_auto.yaml` | `--config <path>` |
| `orca_auto run-dir <path>` | Validate input files in a directory and submit to the queue | `--config <path>` |
| `orca_auto queue list` | Inspect job queue and simulation status | `--json`, `--engine <orca\|workflow>`, `--limit <N>`, `--refresh` |
| `orca_auto queue list clear` | Prune completed, failed, or cancelled entries from the queue | `--engine <orca\|workflow>` |
| `orca_auto queue cancel <target>` | Cancel a queued or actively running calculation | `--json` (`target`: queue_id, run_id, path alias) |
| `orca_auto scaffold conformer_search <path>` | Scaffold conformer screening workflow (extension required) | `--config <path>` |
| `orca_auto index prune` | Prune stale job location records whose files were removed | `--apply` (commit pruning), `--json` |
| `orca_auto service status` | Check status of systemd runtime targets and worker daemons | `--json` |
| `orca_auto service restart` | Safely restart worker daemons | `--force` (immediate restart, may stop active calculations) |
| `orca_auto systemd install` | Register and enable systemd units | `--user <name>`, `--repo <path>`, `--worker-only` |

---

## 2. Configuration Schema (`config/orca_auto.yaml`)

Configuration is resolved from `<repo_root>/config/orca_auto.yaml` or `~/.config/orca_auto/orca_auto.yaml`.

```yaml
runs_root: "/home/user/orca_runs"       # Top-level calculation directory root (required)

scheduler:
  max_active_simulations: 4             # Global limit for concurrent active simulations
  admission_root: "/home/user/orca_runs/.admission"

resources:
  max_cores_per_task: 8                 # Default CPU cores per task
  max_memory_gb_per_task: 32            # Default memory (GB) per task

orca:
  paths:
    orca_executable: "/opt/orca/orca"   # Absolute path to ORCA binary
  runtime:
    scratch_root: "/dev/shm/orca_auto"  # RAM scratch workspace (optional)
    scratch_min_free_gb: 8              # Minimum free memory required for scratch

workflow:
  paths:
    xtb_executable: "/opt/xtb/bin/xtb"      # Absolute path to xTB binary (optional)
    crest_executable: "/opt/crest/bin/crest" # Absolute path to CREST binary (optional)

messenger:
  provider: discord                     # Notification provider (discord)
  discord:
    bot_token: ""                       # Bot token
    default_channel_id: ""              # Target channel ID
```

---

## 3. Job Lifecycle and Status Classification

### Queue States
- `queued`: Enqueued and waiting for an available worker slot.
- `running`: Actively executing in an allocated worker slot.
- `completed`: Successfully finished and converged.
- `failed`: Terminated with errors or failed convergence.
- `cancelled`: Explicitly cancelled by user.

### Failure Classification
ORCA_auto parses output logs to pinpoint specific failure modes:
- `error_scf`: Electronic SCF failed to converge.
- `error_opt`: Geometry optimization did not converge within the cycle limit.
- `error_geometry`: Invalid coordinates (e.g., overlapping atoms or distance collapse).
- `error_memory` / `error_disk`: Out-of-memory or scratch disk space exhaustion.
- `error_termination`: Syntax abort or unclassified abnormal engine termination.

---

## 4. Output Artifacts

Within each job directory (`runs_root/<job_dir>/`), outputs are organized into generation subdirectories:

- `machine.json`: Machine-readable metadata (final energies, status, timestamps, receipt hashes).
- `job_state.json`: Internal lifecycle state and resumption checkpoints.
- `job_report.html`: Visual single-file report containing convergence plots, energy profiles, and vibrational frequencies.
- `si_block.md`: Markdown Supporting Information block with final energies, ZPE/thermal corrections, and Cartesian coordinates.
- `*.out` / `*.gbw`: Raw ORCA output files and binary wavefunction checkpoints.

---

## 5. Troubleshooting

```bash
# 1. Check worker daemon status
orca_auto service status

# 2. Inspect real-time worker logs
journalctl -u "orca_auto-queue-worker@$(whoami)" -f

# 3. Refresh queue view
orca_auto queue list --refresh

# 4. Safely restart workers
orca_auto service restart
```
