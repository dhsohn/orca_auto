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
- A config that does not load (`invalid_config`) or a corrupt `queue.json` (`queue_store_corrupt`) is reported as one `error:` line with exit 1, also with `--log-file`.

---

### `orca_auto queue list`
Inspects queued jobs, worker activity, and global active simulation counts.
```bash
orca_auto queue list [--config PATH] [--status STATUS] [--limit N] [--refresh] [--json]
```
- `--status STATUS`: Filter by state (`pending`, `running`, `completed`, `failed`, `cancelled`).
- `--limit N`: Limit the number of returned entries.
- `--refresh`: Scan the filesystem for unindexed calculation directories and record them in `job_locations.json` (the same rebuild as `index rebuild`).
- `--json`: Structured JSON output for downstream automation and tooling.
- Each row carries `worker_log` (`<runs_root>/logs/<queue_id>.log`); the text view lists it under the table for running and failed rows.
- Exits 1 without a discoverable config or an existing `runs_root`, creating nothing. A corrupt `admission_slots.json` is reported as an `admission_blockers` entry (scope `admission_store`, queue id `*`) while `active_simulations` falls back to the listing's count.

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
> **Note**: Clears terminal queue records and unlinks job-root terminal `job_state.json` metadata (resetting the duplicate submission barrier so subsequent submissions do not require `--force`). All generation subdirectories, calculation artifacts, and output files remain untouched on disk. The worker log and publication lock file of every cleared row are removed; `--json` reports the log count as `removed_worker_logs`.

A terminal row with unfinished publication retains its replay marker and is excluded from clearing and force resubmission. Its execution slot may already be free; the worker retries index publication and marker removal without rerunning ORCA.

---

### `orca_auto index rebuild`
Re-derives `job_locations.json` from the run states on disk; `index prune` owns removal.
```bash
orca_auto index rebuild [--config PATH] [--dry-run] [--json]
```
- Walks every `job_state.json` under `runs_root` (`report.json` outranks the state for identity) and adds or updates rows by job id; rows are never removed. A state that names neither a job id nor a run id is reported as skipped.
- A job id found in several directories is never chosen silently: the row keeps the directory it already points at while that directory still holds the job's state, a finished (`completed`/`failed`/`cancelled`) row is never turned back into a running one, and otherwise a terminal state beats a non-terminal one, then the newest `job_state.json`. Each such case is printed as a `conflict:` line naming the kept and ignored directories.
- `--dry-run`: Report the rows that would be added or updated without writing the index.
- `--json`: Emits `index_path`, `scanned`, `total`, `added_count`, `updated_count`, `unchanged_count`, `skipped_count`, `applied`, the `added`, `updated` and `skipped` lists and the `conflicts` list (`job_id`, `kept_path`, `ignored_paths`).
- Exit code is 0 also when nothing changes; 1 when `runs_root` is unconfigured, the config is damaged, `runs_root` is missing, the index is corrupt or an OS error occurs (nothing is written).

---

### `orca_auto scratch list` & `scratch clear`
Inspects and clears RAM-scratch workspaces under `orca.runtime.scratch_root`. One stale, unverifiable or invalid-manifest workspace blocks every later scratch launch until it is removed.
```bash
orca_auto scratch list [--config PATH] [--json]
orca_auto scratch clear NAME [--config PATH] [--json]
orca_auto scratch clear --all-stale [--config PATH] [--json]
```
- `list`: Prints each workspace with its state (`live`, `stale`, `unverifiable`, `invalid-manifest`, `unsafe`, `tombstone`), owner PID and size, and names the workspaces that block new launches. Exits 0 even when blockers exist; exits 1 when scratch is not configured or the root is unreadable.
- `clear NAME`: Removes the named workspace as printed by `scratch list` (`attempt-...`). Live workspaces are refused.
- `--all-stale`: Removes every `stale`, `unverifiable` or `invalid-manifest` workspace. Exactly one of `NAME` or `--all-stale` is required.
- Publication temp files of the durable generation are cleaned only when the manifest was valid and the generation lies under the configured `runs_root`; otherwise the path is left alone and the reason is printed as a `note:` (`durable_note` in `--json`).
- Exit code is 1 when any target was refused; `--all-stale` with nothing to remove exits 0 with `removed_count` 0.

---

### `orca_auto service status` & `service restart`
Inspects background workers and controls systemd services safely.
```bash
orca_auto service status [--json]
orca_auto service restart [--force]
```
- `status`: Verifies that running worker processes match the checkout HEAD or the installed runtime build. Exits 1 (`ok: false` under `--json`) when a unit is unhealthy or a worker is stale or undetermined.
- `restart`: Refuses to restart if active calculations are in flight to prevent calculation interruption. Use `--force` to override. A failed `sudo`/`systemctl` step exits 1 with an `error:` line naming the command.

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
├── job_state.json             # Current execution and parent notification bookkeeping
└── 20260921-022823-b43c48b7/     # Generation-isolated execution directory
    ├── water.inp              # Staged input copy
    ├── water.out              # Raw ORCA standard output (stem matches input)
    ├── job_state.json         # Execution evidence; updates only when execution facts change
    ├── machine.json           # Structured observation artifact (v1 envelope schema)
    ├── execution_provenance.json # Captured submission/execution identities, when recorded
    ├── job_report.html        # (Optional) Supporting Information and web summary report
    └── si_block.md            # (Optional) Supporting Information markdown block
```

---

## 4. Live Log Inspection

Inspect real-time worker scheduling and execution logs using systemd journal:
```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```
- The journal carries the worker's INFO lifecycle lines (`Queue worker started`/`stopped`, orphan reconciliation, intent sweeps) plus its warnings and errors; each child's INFO lines go to its own `worker_log` file (`<runs_root>/logs/<queue_id>.log`, shown by `queue list`).
- A failed startup reconciliation is one `Queue worker startup failed: <reason>` line; the worker removes its pid file and exits 1, and the supervisor restarts it up to its cap.
- When `max_concurrent` × `resources.max_cores_per_task` exceeds the CPUs the worker may use (`sched_getaffinity`), the worker logs one WARNING naming both numbers at start. It does not refuse to start and there is no option to silence it.
