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
- `--priority N`: Queue scheduling priority (default: 10; lower values = higher priority, run earlier).
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
> **Note**: Clears terminal queue records and unlinks job-root terminal `job_state.json` metadata (resetting the duplicate submission barrier so subsequent submissions do not require `--force`). All generation subdirectories, calculation artifacts, and output files remain untouched on disk.

---

### `orca_auto service status` & `service restart`
Inspects background workers and controls systemd services safely.
```bash
orca_auto service status [--json]
orca_auto service restart [--force]
```
- `status`: Verifies that running worker processes match the installed systemd unit build.
- `restart`: Refuses to restart if active calculations are in flight to prevent calculation interruption. Use `--force` to override.

---

## 2. Queue Lifecycle States

| State | Description |
| :--- | :--- |
| `pending` | Job is durably recorded in the queue, awaiting worker admission and an available slot. (If admission is temporarily deferred due to transient host constraints like RAM scratch capacity, the job remains in `pending` with `metadata.admission_deferral_reason` set and display detail showing resource deferral). |
| `running` | Worker has claimed an execution slot and ORCA is running inside an isolated generation directory. |
| `completed` | ORCA calculation finished with normal termination (`ORCA TERMINATED NORMALLY`) and verified diagnostic checks. Does not guarantee convergence of every numerical property (e.g., unconverged SCF energy lines are omitted as null). |
| `failed` | Calculation terminated with an error, SCF convergence failure, or non-zero exit code (no blind retries). |
| `cancelled` | Calculation was explicitly aborted by the user. |

---

## 3. Directory Artifacts

Each calculation writes to a generation-isolated directory:
```text
water/
├── water.inp                  # Target input file
├── job_state.json             # Job-level state tracking active/terminal generations
└── 20260921-022823-b43c48b7/     # Generation-isolated execution directory
    ├── water.inp              # Staged input copy
    ├── water.out              # Raw ORCA standard output (stem matches input)
    ├── job_state.json         # Generation-level execution state
    ├── machine.json           # Structured observation artifact (v1 envelope schema)
    ├── job_report.html        # (Optional) Supporting Information and web summary report
    └── si_block.md            # (Optional) Supporting Information markdown block
```

---

## 4. Live Log Inspection

Inspect real-time worker scheduling and execution logs using systemd journal:
```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```
