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
| `queue list` | Queries queued/active jobs and the global active simulation count. Supports `--json` for automation. Lists the rows of `queue.json`, each with its own directory's root `job_state.json`; a run state without a queue row is not listed ([ADR 0010](adr/0010-queue-commands-read-queue-rows.md)). Exits 1 without a discoverable config or an existing `runs_root`, creating nothing. A corrupt `admission_slots.json` is reported as an `admission_blockers` entry (scope `admission_store`) while `active_simulations` falls back to the listing's count; each row carries `worker_log`. |
| `queue list clear` | Clears terminal queue records and unlinks job-root run states while preserving generation artifacts on disk; also removes each cleared row's worker log and publication lock file. Exits 1 without a discoverable config or an existing `runs_root`. |
| `queue cancel TARGET` | Cancels a job by queue ID, run ID, or unambiguous directory path alias. A directory path or name resolves to that directory's active generation, or to its newest finished row when none is active; a name shared by different directories, or two active generations, is ambiguous. A queue ID or run ID wins over a directory name, and a name that is an existing directory in the working directory without a queue row of its own is ambiguous too. Under `--json`, `result` is `{status, reason, queue_id, job_id, reaction_dir}`; a failure exits 1 with `reason` `target_not_found`, `ambiguous`, `already_terminal` or `cancel_failed`, and the row fields stay empty when no single row was named. Exits 1 without a discoverable config or an existing `runs_root`. |
| `index prune` | Previews indexed rows whose disk paths no longer exist. Removes them only when `--apply` is passed. |
| `index rebuild` | Re-derives `job_locations.json` rows from every `job_state.json` under `runs_root`, adding or updating rows by job id and never removing one. `--dry-run` reports without writing. |
| `systemd install` | Installs systemd unit templates for the specified user and repository or prepared runtime root (`--repo`). A config that exists but does not load exits 1 and writes no units; `TimeoutStopSec` is rendered from `scheduler.max_active_simulations` and `ReadWritePaths` names only `runs_root`. A failed `sudo`/`systemctl` step exits 1 with an `error:` line naming the command. |
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
- Files named by recognized ORCA file keywords (coordinate files, `%moinp`, `%pointcharges`, Hessian inputs including ESD `GSHessian`/`ESHessian`, NEB endpoint and restart paths) are bound into the generation. Any other quoted keyword value that looks like a file path (absolute, starting with `~`, `./` or `../` relative with either slash, or with a filename extension) is rejected at submission with `Unsupported ORCA file reference`. File names ORCA writes (`%plots` file arguments, a `%md` `Filename`) must be plain basenames.
- On NEB-family routes, a referenced file whose basename matches a file ORCA writes for the input stem (for example `<stem>_MEP.allxyz`, and `<stem>_reactant*`/`<stem>_product*` when `%neb` enables end-point preoptimization or contains `Monitor_Internals`) is rejected at submission; rename the restart or endpoint file before resubmitting.
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
> Invalid mappings, explicit nulls and unrecognized keys (including a `workflow` section) are rejected before default values are applied. See [config/orca_auto.yaml.example](../config/orca_auto.yaml.example) for accepted settings.

Admission state always lives in `<runs_root>/.admission`, and its limit is `scheduler.max_active_simulations`. The removed `scheduler.admission_root` key is rejected with a hint to delete it ([ADR 0007](adr/0007-one-admission-store-under-runs-root.md)).

---

## 3. Execution & Recovery Guarantees

1. **Atomic Submission**: A successful submission (`status: queued`) guarantees that the input snapshot is created and the job is permanently recorded on disk.
2. **Generation Isolation**: Calculations run within versioned, generation-isolated directories to prevent state contamination across repeated attempts.
3. **Explicit Failure Handling**: Failed runs record clear diagnostic exit reasons without attempting automatic retries.
4. **Capacity Deferral**: When RAM Scratch is enabled, temporary host memory constraints defer launching (job remains in `pending` with `metadata.admission_deferral_reason` set) rather than failing the calculation.
5. **Publication Failure Isolation**: A queued location-index publication that is busy or fails keeps that submission pending while unrelated eligible jobs may use available capacity. The worker retries publication on subsequent admission passes; persisted failure details remain visible through `queue list` and its `admission_blockers`. Those publication blockers identify individual queue rows. Path/generation checks still apply, and an unreadable queue stops admission.

6. **State Ownership**: Job-root `job_state.json` serves current execution control and parent notification bookkeeping. Generation `job_state.json` records execution evidence for result verification. The state writer saves changed generation facts before refreshing root; notification-only and identical-state saves preserve generation bytes and timestamps. Historical notification fields stay readable. If root refresh fails, the saved generation remains and the error is reported; retrying the same execution does not rewrite that evidence. The `queue_generation` recorded in `job_state.json` is an opaque digest of the queue generation identity, comparable only within one major version.

7. **Terminal Completion Ownership**: The parent confirms child/engine termination and prepares the actual terminal run evidence before returning execution capacity. A zero exit code still requires a matching terminal state. Index publication and replay-marker removal can retry without an execution slot, including after worker restart. The durable marker fences subsequent submissions in the same directory until publication completes; unrelated eligible jobs may proceed. State preparation and slot-release failures retain supervised retry ownership. Notification delivery remains best effort.

8. **Advisory Notification Ownership**: The parent claims queued notifications from a newly submitted durable row after queued publication completes; the child dispatches its captured start event after saving attempt state. Bounded background delivery does not hold publication completion or runner launch. Claim/send failures and process exit can lose advisory messages. Historical rows are not backfilled, and notification delivery never changes execution evidence.

9. **Terminal Publication Visibility**: A terminal replay marker preserves the row's execution status and adds `result publication pending` detail. Metadata identifies `publication_blocked_scope=orca_terminal_publication` and `publication_owner=orca_queue_worker`, with reason and next action. These directory-specific fences also appear in `admission_blockers` across status filters and pagination; they do not imply an occupied execution slot.

---

## 4. Machine Observation (`machine.json`) Schema

Upon completion, each job publishes a structured `machine.json` artifact in its generation directory for downstream tools (such as Chemvas and LLMdocx):

- **Envelope Schema**: Conforms to the standard `factory/machine-observation` v1 contract.
- **Operation & Payload**: Emits `chemistry/orca-run` with a `chemistry/results-bundle` v1 payload.
- **Input Provenance**: When submission source identities are recorded, `payload.data.results.execution_provenance_artifact` references the required `execution-provenance` artifact (`execution_provenance.json`, `application/json`). It preserves the captured original input/dependency identities, bound input and materialized-copy identities, resolved resource request, executable identity and any crash-recovery origin. `artifacts.input` refers to the execution `.inp`, which can differ from the original after resource normalization and reference rewriting. The provenance file records identities, not an archive of original file contents. Its filename is reserved; referenced input files with that basename are rejected before execution. Any reader can verify its receipt without reopening source paths; the release smoke checks agreement with generation state. Historical reports lacking this evidence remain readable and are not backfilled; terminal publication and replay do not rewrite it.
- **Verification**: Completion (`completed`) verifies normal termination (`ORCA TERMINATED NORMALLY`) without detected fatal crash markers (and for TS calculations, satisfies mode-specific stationary point criteria). It does not guarantee that every numerical property converged; for example, if the final single-point energy line is annotated `SCF not fully converged!`, energy fields are omitted (`null`) rather than populated with unverified numbers. Extracted chemical properties (energies, stationary points, electronic states) reflect verified evidence without synthetic defaults.
- **Scope Boundary**: ORCA_auto supervises process lifecycle and structures output artifacts; scientific acceptance and chemical validity remain the researcher's responsibility.

---

## 5. No Workflow Support

- **Standalone ORCA only**: Conformer search orchestration, scaffolds and the internal xTB/CREST engines were removed in 7.0 ([7.0 Upgrade Guide](RELEASE.md#upgrading-to-70)).
- **Leftover workflow files have no meaning**: A directory that holds `flow.yaml` or `workflow.json`, or lies under one, is an ordinary directory for `run-dir`, the worker, `queue cancel`, `queue list clear`, cleanup and `index rebuild`. A queue row's `workflow_id` metadata is ignored, and an `admission_slots.json` row carrying `workflow_id` is rejected as corrupt ([ADR 0005](adr/0005-remove-retired-workflow-support.md)).
