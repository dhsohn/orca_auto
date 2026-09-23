# Public Contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

This document defines the stable public contracts, runtime behaviors, configuration rules, and artifact schemas for ORCA_auto 7.0.
ORCA_auto operates on Linux and WSL2 with Python 3.11+ and systemd supervision, using absolute Linux paths.

---

## 1. Public CLI Commands

| Command | Behavior and Guarantees |
| :--- | :--- |
| `init` | Creates or updates the shared configuration (`orca_auto.yaml`). Custom paths are specified via `--config`. |
| `run-dir PATH` | Validates and durably enqueues an ORCA input directory, returning immediately upon acceptance. |
| `queue list` | Queries queued/active jobs and the global active simulation count. Supports `--json` for automation. |
| `queue list clear` | Clears terminal (completed, failed, cancelled) queue entries while preserving calculation artifacts on disk. |
| `queue cancel TARGET` | Cancels a job by queue ID, run ID, or unambiguous directory path alias. |
| `index prune` | Previews indexed rows whose disk paths no longer exist. Removes them only when `--apply` is passed. |
| `systemd install` | Installs systemd unit templates for the specified user and runtime. |
| `service status` | Inspects systemd units and verifies worker process freshness against the installed build. Returns non-zero on drift. |
| `service restart` | Refuses restart if active calculations or reservations exist, preventing accidental data loss. Use `--force` to bypass. |

### `run-dir` Behavior
- Automatically detects the most recently modified eligible `.inp` file in the target directory (ties broken alphabetically by filename).
- Binds inputs, referenced coordinate files, and the verified ORCA executable into a fresh execution generation.
- Re-submitting an active calculation directory is rejected to prevent duplicate execution.
- Passing `--force` triggers a new execution generation even if an earlier attempt succeeded.
- Computational resources strictly honor `%pal` and `%maxcore` directives inside the `.inp` file; configuration values fill in missing defaults.

---

## 2. Configuration Precedence & Validation

Configuration files are resolved in the following priority order:
1. Explicit CLI argument (`--config PATH`)
2. Environment variable `ORCA_AUTO_CONFIG`
3. Checkout configuration (`config/orca_auto.yaml`)
4. User home default (`~/orca_auto/config/orca_auto.yaml`)

> **Validation Policy**:
> Invalid mappings, explicit nulls, unrecognized keys, and retired workflow configuration sections are rejected before default values are applied. See [config/orca_auto.yaml.example](../config/orca_auto.yaml.example) for accepted settings.

---

## 3. Execution & Recovery Guarantees

1. **Atomic Submission**: A successful submission (`status: queued`) guarantees that the input snapshot is created and the job is permanently recorded on disk.
2. **Generation Isolation**: Calculations run within versioned, generation-isolated directories to prevent state contamination across repeated attempts.
3. **Explicit Failure Handling**: Failed runs record clear diagnostic exit reasons without attempting automatic, blind retries.
4. **Capacity Deferral**: When RAM Scratch is enabled, temporary host memory constraints defer launching (`waiting for resources`) rather than failing the calculation.

---

## 4. Machine Observation (`machine.json`) Schema

Upon completion, each job publishes a structured `machine.json` artifact for downstream tools (such as Chemvas and LLMdocx):

- **Envelope Schema**: Conforms to the standard `factory/machine-observation` v1 contract.
- **Operation & Payload**: Emits `chemistry/orca-run` with a `chemistry/results-bundle` v1 payload.
- **Verification**: Completion is verified through ORCA normal termination markers and output diagnostic scanning rather than process exit codes alone. Extracted chemical properties (energies, stationary points, electronic states) reflect verified evidence without synthetic defaults.
- **Scope Boundary**: ORCA_auto supervises process lifecycle and structures output artifacts; scientific acceptance and chemical validity remain the researcher's responsibility.

---

## 5. Version 7.0 Retirement & Migration

- **Workflows Retired**: Conformer search orchestration, scaffolds, and internal xTB/CREST engines have been removed in version 7.0 to focus entirely on standalone ORCA execution.
- **Historical Data Safety**: Existing 6.x workflow directories are retained intact as read-only workspaces and cannot be overwritten by 7.0 workers.
- Refer to the [7.0 Upgrade Guide](RELEASE.md#upgrading-to-70) for operational transition steps.
