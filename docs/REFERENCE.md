# Command and Runtime Reference

**English** | [한국어](REFERENCE.ko.md)

This document provides a detailed reference for ORCA_auto 7.0 CLI commands, flags, lifecycle states, and output artifacts.
For formal runtime guarantees, refer to [Public Contracts (PUBLIC_CONTRACTS.md)](PUBLIC_CONTRACTS.md).

---

## 1. CLI Commands

### `orca_auto init`
Creates or updates the shared configuration file (`orca_auto.yaml`).
```bash
orca_auto init [--config PATH] [--force]
```
- `--config PATH`: Path for the configuration file (default: `~/orca_auto/config/orca_auto.yaml`).
- `--force`: Overwrite existing configuration if present.

---

### `orca_auto run-dir`
Validates an ORCA input directory and enqueues it atomically.
```bash
orca_auto run-dir <PATH> [--config PATH] [--force] [--priority N] [--json]
```
- `<PATH>`: Target directory containing an ORCA `.inp` file.
- `--force`: Force resubmission in a new generation even if an earlier attempt succeeded.
- `--priority N`: Queue scheduling priority (default: 0; higher values run earlier).
- `--json`: Output submission metadata in structured JSON format.

---

### `orca_auto queue list`
Inspects queued jobs, worker activity, and global active simulation counts.
```bash
orca_auto queue list [--config PATH] [--status STATUS] [--limit N] [--refresh] [--json]
```
- `--status STATUS`: Filter by state (`pending`, `running`, `completed`, `failed`, `cancelled`).
- `--limit N`: Limit the number of returned entries.
- `--refresh`: Scan the filesystem for unindexed calculation directories.
- `--json`: Structured JSON output for downstream automation and tooling.

---

### `orca_auto queue cancel`
Safely cancels a pending or running job.
```bash
orca_auto queue cancel <TARGET> [--config PATH] [--json]
```
- `<TARGET>`: Queue ID (`q_...`), Run ID (`run_...`), or job directory path.

---

### `orca_auto queue list clear`
Cleans up terminal entries (`completed`, `failed`, `cancelled`) from the queue view.
```bash
orca_auto queue list clear [--config PATH] [--json]
```
> **Note**: This only prunes queue listing records. All calculation artifacts, log files, and outputs remain intact on disk.

---

### `orca_auto service status` & `service restart`
Inspects background workers and controls systemd services safely.
```bash
orca_auto service status [--config PATH] [--json]
orca_auto service restart [--config PATH] [--force]
```
- `status`: Verifies that running worker processes match the installed systemd unit build.
- `restart`: Refuses to restart if active calculations are in flight to prevent calculation interruption. Use `--force` to override.

---

## 2. Queue Lifecycle States

| State | Description |
| :--- | :--- |
| `pending` | Job is durably recorded in the queue, awaiting worker admission and an available slot. |
| `running` | Worker has claimed an execution slot and ORCA is running inside an isolated generation directory. |
| `completed` | ORCA calculation finished cleanly with verified termination markers and energy convergence. |
| `failed` | Calculation terminated with an error, SCF convergence failure, or non-zero exit code (no blind retries). |
| `cancelled` | Calculation was explicitly aborted by the user. |
| `waiting for resources` | Job is temporarily deferred due to transient host constraints (e.g., RAM Scratch memory capacity). |

---

## 3. Directory Artifacts

Each calculation writes to a generation-isolated directory:
```text
water/
├── water.inp               # Original input file
├── job.out                 # Raw ORCA standard output
├── job_state.json          # Internal execution metadata and recovery tokens
├── machine.json            # Structured observation artifact (v1 envelope schema)
└── report.html             # (Optional) Supporting Information and web summary report
```

---

## 4. Live Log Inspection

Inspect real-time worker scheduling and execution logs using systemd journal:
```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```
