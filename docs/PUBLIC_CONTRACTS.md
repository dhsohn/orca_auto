# Public Contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

This document defines the stable public contracts, runtime behaviors, configuration rules, and artifact schemas for ORCA_auto.
ORCA_auto operates on Linux and WSL2 with Python 3.11+ and systemd supervision, using absolute Linux paths.

---

## 1. Public CLI Commands

| Command | Behavior and Guarantees |
| :--- | :--- |
| `init` | Creates or updates the shared configuration (`orca_auto.yaml`). Custom paths are specified via `--config`. |
| `run-dir PATH` | Validates and durably enqueues an ORCA input directory, returning immediately upon acceptance. A config that does not load (`invalid_config`) or a corrupt `queue.json` (`queue_store_corrupt`) is one `error:` line with exit 1. |
| `queue list` | Queries queued/active jobs and the global active simulation count. Supports `--json` for automation. Exits 1 without a discoverable config or an existing `runs_root`, creating nothing. A corrupt `admission_slots.json` is reported as an `admission_blockers` entry (scope `admission_store`) while `active_simulations` falls back to the listing's count; each row carries `worker_log`. |
| `queue list clear` | Clears terminal queue records and unlinks job-root run states while preserving generation artifacts on disk; also removes each cleared row's worker log and publication lock file. Exits 1 without a discoverable config or an existing `runs_root`. |
| `queue cancel TARGET` | Cancels a job by queue ID, run ID, or unambiguous directory path alias. Exits 1 without a discoverable config or an existing `runs_root`. |
| `index prune` | Previews indexed rows whose disk paths no longer exist. Removes them only when `--apply` is passed. |
| `index rebuild` | Re-derives `job_locations.json` rows from every `job_state.json` under `runs_root`, adding or updating rows by job id and never removing one. `--dry-run` reports without writing. |
| `systemd install` | Installs systemd unit templates for the specified user and repository or prepared runtime root (`--repo`). A config that exists but does not load exits 1 and writes no units; `TimeoutStopSec` is rendered from `scheduler.max_active_simulations`. |
| `service status` | Inspects systemd units and verifies worker process freshness against the checkout HEAD or the installed runtime build. Exits 1 (`ok: false` under `--json`) when a unit is unhealthy or a worker is stale or undetermined. |
| `service restart` | Refuses restart if active calculations or reservations exist, preventing accidental data loss. Use `--force` to bypass. A failed `sudo`/`systemctl` step exits 1 with an `error:` line naming the command. |
| `scratch list` | Lists RAM-scratch workspaces under `orca.runtime.scratch_root` and whether any non-live workspace blocks new scratch launches. Exits 0 even when blockers exist; supports `--json`. |
| `scratch clear NAME` / `--all-stale` | Removes non-live (`stale`, `unverifiable`, `invalid-manifest`) scratch workspaces. Live workspaces are refused; exits 1 when a target was refused, while `--all-stale` with nothing to remove exits 0. Publication temp files of the durable generation are cleaned only when the manifest was valid and the generation lies under `runs_root`; otherwise the path is left alone and named in `durable_note`. |

### JSON output and exit codes
- Every `--json` document carries `ok`, `true` exactly when the command exits 0. A failed command prints `{"ok": false, "error": "<message>"}` on stdout and still writes the `error:` line to stderr.
- Exit code 0 means success or nothing to do; 1 means the command was refused, failed or was invalid; 2 is an argparse usage error. Raw exit codes of `sudo`/`systemctl` are never passed through.

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
3. User home default (`~/orca_auto/config/orca_auto.yaml`)

A source checkout is not probed.

> **Validation Policy**:
> Invalid mappings, explicit nulls, unrecognized keys, and retired workflow configuration sections are rejected before default values are applied. See [config/orca_auto.yaml.example](../config/orca_auto.yaml.example) for accepted settings.

---

## 3. Execution & Recovery Guarantees

1. **Atomic Submission**: A successful submission (`status: queued`) guarantees that the input snapshot is created and the job is permanently recorded on disk.
2. **Generation Isolation**: Calculations run within versioned, generation-isolated directories to prevent state contamination across repeated attempts.
3. **Explicit Failure Handling**: Failed runs record clear diagnostic exit reasons without attempting automatic retries.
4. **Capacity Deferral**: When RAM Scratch is enabled, temporary host memory constraints defer launching (job remains in `pending` with `metadata.admission_deferral_reason` set) rather than failing the calculation.
5. **Publication Failure Isolation**: A queued location-index publication that is busy or fails keeps that submission pending while unrelated eligible jobs may use available capacity. The worker retries publication on subsequent admission passes; persisted failure details remain visible through `queue list` and its `admission_blockers`. Those publication blockers identify individual queue rows. Path/generation checks still apply, and an unreadable queue stops admission.

6. **State Ownership**: Job-root `job_state.json` serves current execution control and parent notification bookkeeping. Generation `job_state.json` records execution evidence for result verification. The state writer saves changed generation facts before refreshing root; notification-only and identical-state saves preserve generation bytes and timestamps. Historical notification fields stay readable. If root refresh fails, the saved generation remains and the error is reported; retrying the same execution does not rewrite that evidence.

7. **Terminal Completion Ownership**: The parent confirms child/engine termination and prepares the actual terminal run evidence before returning execution capacity. A zero exit code still requires a matching terminal state. Index publication and replay-marker removal can retry without an execution slot, including after worker restart. The durable marker fences subsequent submissions in the same directory until publication completes; unrelated eligible jobs may proceed. State preparation and slot-release failures retain supervised retry ownership. Notification delivery remains best effort.

8. **Advisory Notification Ownership**: The parent claims queued notifications from a newly submitted durable row after queued publication completes; the child dispatches its captured start event after saving attempt state. Bounded background delivery does not hold publication completion or runner launch. Claim/send failures and process exit can lose advisory messages. Historical rows are not backfilled, and notification delivery never changes execution evidence.

9. **Terminal Publication Visibility**: A terminal replay marker preserves the row's execution status and adds `result publication pending` detail. Metadata identifies `publication_blocked_scope=orca_terminal_publication` and `publication_owner=orca_queue_worker`, with reason and next action. These directory-specific fences also appear in `admission_blockers` across status filters and pagination; they do not imply an occupied execution slot.

---

## 4. Machine Observation (`machine.json`) Schema

Upon completion, each job publishes a structured `machine.json` artifact in its generation directory for downstream tools (such as Chemvas and LLMdocx):

- **Envelope Schema**: Conforms to the standard `factory/machine-observation` v1 contract.
- **Operation & Payload**: Emits `chemistry/orca-run` with a `chemistry/results-bundle` v1 payload.
- **Input Provenance**: When submission source identities are recorded, `payload.data.results.execution_provenance_artifact` references the required `execution-provenance` artifact (`execution_provenance.json`, `application/json`). It preserves the captured original input/dependency identities, bound input and materialized-copy identities, resolved resource request, executable identity and any crash-recovery origin. `artifacts.input` refers to the execution `.inp`, which can differ from the original after resource normalization and reference rewriting. The provenance file records identities, not an archive of original file contents. Its filename is reserved; referenced input files with that basename are rejected before execution. Readers verify its receipt and agreement with generation state without reopening source paths. Historical reports lacking this evidence remain readable and are not backfilled; terminal publication and replay do not rewrite it.
- **Verification**: Completion (`completed`) verifies normal termination (`ORCA TERMINATED NORMALLY`) without detected fatal crash markers (and for TS calculations, satisfies mode-specific stationary point criteria). It does not guarantee that every numerical property converged; for example, if the final single-point energy line is annotated `SCF not fully converged!`, energy fields are omitted (`null`) rather than populated with unverified numbers. Extracted chemical properties (energies, stationary points, electronic states) reflect verified evidence without synthetic defaults.
- **Scope Boundary**: ORCA_auto supervises process lifecycle and structures output artifacts; scientific acceptance and chemical validity remain the researcher's responsibility.

---

## 5. Version 7.0 Retirement & Migration

- **Workflows Retired**: Conformer search orchestration, scaffolds, and internal xTB/CREST engines have been removed in version 7.0 to focus entirely on standalone ORCA execution.
- **Historical Data Safety**: Existing 6.x workflow directories remain read-only and will not be overwritten by 7.0 workers.
- Refer to the [7.0 Upgrade Guide](RELEASE.md#upgrading-to-70) for operational transition steps.
