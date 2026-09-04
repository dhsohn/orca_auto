# Public Contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

This document names the orca_auto surfaces that users, operators, and future
contributors may reasonably depend on. It is intentionally narrower than the
full implementation: internal modules, private helper functions, and runtime
plumbing may change when the documented behavior stays intact.

As of 1.0.0, every surface this document names is a committed contract. The
0.x releases carried a two-tier split — a small committed Stable Core plus an
accurate-but-movable Experimental remainder. Before the 1.0 tag every
Experimental surface was either promoted or removed, so the tiers are gone:
what is documented here is what the project commits to.

## Contract Rules

- Changes to any surface named here are deliberate, tested, documented, and
  called out in [CHANGELOG.md](../CHANGELOG.md). A change that breaks a
  documented behavior requires a major version.
- Additive JSON fields are allowed; consumers should ignore unknown fields.
- Human-oriented Markdown, HTML, and terminal formatting may change; use
  `--json` or the JSON artifacts for scripts.
- Internal worker entrypoints and Python helper modules are not a public API
  unless this document or [docs/REFERENCE.md](REFERENCE.md) says otherwise.
- Real ORCA behavior that cannot be proven in public CI is recorded as manual
  acceptance evidence, following [docs/VALIDATION.md](VALIDATION.md).

## Runtime Contract

Supported runtime assumptions:

- Python 3.11 or newer.
- Native Linux or WSL2.
- systemd 247 or newer on hosts that run the systemd units: `service status`
  reads unit start times with `systemctl show --timestamp=utc`, and an older
  systemd reports every git-backed worker as `undetermined`.
- Linux/POSIX paths for configured roots and executables.
- ORCA, xTB, and CREST executables, when configured, must be absolute Linux
  executable paths.
- The account running a chemistry engine owns and trusts its job directory and
  executable distribution for the lifetime of the job. For xTB/CREST this also
  includes the captured `PATH`/`LD_LIBRARY_PATH` and any `XTBPATH`/`XTBHOME`
  parameter roots. Executable bytes are content-identified, but shared-library
  and external-parameter contents are not copied into the queue generation.
  Untrusted processes under the same UID are therefore outside the isolation
  boundary.
- Executable content identity is not generally an engine-version compatibility
  check. ORCA and workflow xTB/CREST versions remain operator-qualified.

Unsupported path and process assumptions:

- Windows drive paths such as `C:\...` or `C:/...`.
- `/mnt/<drive>/...` executable paths.
- Relative executable paths in config.
- `.exe` engine binaries.
- Public CI that requires licensed chemistry binaries.

## Public CLI Contract

The public user/operator CLI is `orca_auto ...`.

Supported commands:

- `orca_auto init`
- `orca_auto run-dir <path>`
- `orca_auto scaffold ts_search <path>`
- `orca_auto scaffold conformer_search <path>`
- `orca_auto scaffold scan_ts <path>`
- `orca_auto queue list`
- `orca_auto queue list clear`
- `orca_auto queue cancel <target>`
- `orca_auto service status`
- `orca_auto service restart`
- `orca_auto systemd install --user <name> --repo <path>`

Behavior:

- `run-dir` is queue-first. New work is enqueued durably and executed later by a
  supervised worker.
- A successful new submission returns `status: queued`.
- Closing the submitting terminal after a successful queue submission is safe.
- A fully closed standalone ORCA job directory may be submitted again; the new
  submission creates a sibling visible generation. Active rows and incomplete
  terminal replay/fence state still block a successor for the same directory,
  and `--force` does not bypass that barrier.
- `queue cancel` accepts the visible activity id plus known aliases such as
  workflow id, queue id, run id, or path aliases.
- `queue list --json`, `queue cancel --json`, and `service status --json` are
  the script-friendly surfaces.
- Expected configuration, queue-store, index, and workflow-registry failures
  from `queue list`, `queue list clear`, and `queue cancel` are concise
  `error:`/`hint:` diagnostics on stderr. They return non-zero without a Python
  traceback or partial JSON on stdout. A downstream pipe closing during output
  is handled as an output condition after any durable clear/cancel action; it
  is not diagnosed as damaged configuration or state.
- `queue list --limit N` accepts only non-negative integers. `0` leaves the
  listing uncapped. `queue list clear` rejects every listing filter, including
  any non-zero `--limit`, before it mutates durable state.

Non-contract CLI surfaces:

- `orca_auto queue worker` and `python -m ...worker_child` are runtime plumbing.
  Users should normally manage long-running workers through `systemd`.
- Hidden `systemd install` flags exist for tests and maintenance; they are not
  the supported operator interface unless documented in the reference.

## Config Contract

Config discovery order:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

Supported configuration paths:

- `runs_root`
- `resources.max_cores_per_task`
- `resources.max_memory_gb_per_task`
- `scheduler.max_active_simulations`
- `scheduler.admission_root`
- `workflow.paths.xtb_executable`
- `workflow.paths.crest_executable`
- `messenger.provider` (`discord`)
- `messenger.discord.bot_token`
- `messenger.discord.default_channel_id`
- `messenger.discord.timeout_seconds`
- `messenger.discord.max_attempts`
- `messenger.discord.retry_backoff_seconds`
- `orca.runtime.default_max_retries`
- `orca.runtime.scratch_root`
- `orca.runtime.scratch_min_free_gb`
- `orca.paths.orca_executable`

Behavior:

- `runs_root` is the single runs root: standalone ORCA jobs and workflow
  workspaces both live under it.
- The shared admission directory defaults to `<runs_root>/.admission` unless
  `scheduler.admission_root` is set.
- `scheduler.max_active_simulations` caps active ORCA, internal xTB, and
  internal CREST jobs together.
- Only the configuration paths listed above are accepted. Unknown, misspelled,
  and removed keys fail configuration loading at the section where they appear.
  Explicit `scheduler`, `resources`, `workflow`, `workflow.paths`, `messenger`,
  `orca`, `orca.runtime`, and `orca.paths` sections must be mappings.
  `scheduler.admission_root` must be an absolute Linux path; explicit
  scheduler/resource limits must be positive integers; and explicit
  `orca.runtime.default_max_retries` must be a non-negative integer. A section
  or key may be omitted to use its documented default, but a configured
  execution-control key with a malformed value is rejected rather than
  defaulted.
- A YAML document with no node (empty, whitespace-only, or comments-only) is
  treated as an empty mapping. An explicit YAML null—including an otherwise
  empty `---` document—scalar, or sequence at the top level is rejected.
  Duplicate mapping keys are rejected at every nesting depth rather than
  resolved with last-key-wins.
- `orca.runtime.default_max_retries: 0` disables ORCA retries.
- A positive `default_max_retries` enables calculation-type retry policy, still
  capped by ORCA route type.
- `orca.runtime.scratch_root`, when present, must name a dedicated directory
  below `/dev/shm`; `scratch_min_free_gb` must be a positive integer. ORCA then
  executes one private tmpfs attempt at a time and publishes surviving regular
  files other than `*.tmp`/`*.tmp.*` as a journaled transaction to the
  inode-pinned durable visible generation. Runtime state artifact names cannot
  be published. Dependencies must be basename-relative and remain
  byte-identical; the selected working copy may receive only a missing final
  newline. Unresolved scratch workspaces fail closed. Launch requires current
  host available memory to cover the configured task-memory cap, free tmpfs,
  and `scratch_min_free_gb` host reserve. Completed-attempt metadata is recorded
  in `scratch_provenance`; committed output from an interrupted/exception path
  is recorded in `scratch_publications`, never in immutable execution-snapshot
  provenance.
  Workflow xTB/CREST use the same configured root and
  one-workspace admission. They keep immutable input snapshots durable, execute in tmpfs, and
  transactionally publish only their canonical result/evidence allowlists;
  omitted work trees and transient entries are recorded in
  `scratch_provenance`. CREST's native `--scratch` option remains unused.
  Root/workspace and generation directories are descriptor-pinned. A launch
  gate may `exec` ORCA only after the shared admission slot's PID/PGID process
  record is durable; EOF before release exits without starting ORCA.
  Queue, state, and process ownership remain durable. Unpublished scratch output
  is intentionally not a recovery contract across host or WSL shutdown.
- Outbound Discord delivery uses `messenger.provider: discord` plus non-empty
  `messenger.discord.bot_token` and `messenger.discord.default_channel_id`
  values. Notifications are one-way; there is no inbound command surface.
- After surrounding whitespace is trimmed, an explicit bot token must be a
  string containing only printable ASCII characters with no whitespace;
  `default_channel_id` must be a documented positive Discord ID. Explicit
  nulls, booleans, collections, control characters, non-ASCII text, and
  whitespace inside a token are rejected without echoing the token. Empty token
  and destination strings remain the intentional way to disable outbound
  delivery.
- Notification transport failure is advisory to queue submission: it produces a
  redacted warning and does not roll back or park an otherwise complete durable
  queue publication. Request construction, HTTP status handling, and response
  body reads all follow this rule; an unreadable success or rate-limit response
  is a bounded delivery failure, not a queue-publication failure.
- The terminal notification sent when a finished job's side effects are replayed
  follows the same rule: a delivery failure is logged with the redacted reason,
  the replay still completes and releases the job's admission slot, and the
  failed message is not retried later. Beyond the adapter's bounded attempts,
  terminal notification is best-effort.
- The Discord adapter bounds finite delivery timeouts to 0.1–120 seconds, integer
  total attempts to 1–10, and finite retry backoff to 0–120 seconds. Omission uses
  the documented defaults and finite values outside those ranges are clamped;
  explicitly configured booleans, non-numeric values, fractions for attempts,
  NaN, and infinities are rejected rather than defaulted.

## Queue And Activity Contract

The durable per-engine queue file is named `queue.json`. It is an implementation
file, but the queue lifecycle and visible activity fields are public behavior.

Queue entry fields that downstream code may rely on:

- `queue_id`
- `app_name`
- `task_id`
- `task_kind`
- `engine`
- `status`
- `priority`
- `enqueued_at`
- `started_at`
- `finished_at`
- `cancel_requested`
- `error`
- `metadata`

Queue statuses:

- `pending`
- `running`
- `completed`
- `failed`
- `cancelled`

Queue priorities are integers ordered from lowest numeric value to highest; `0`
and negative priorities are valid and are never treated as missing values.

`orca_auto queue list --json` returns:

- `count`
- `active_simulations`
- `activities`
- `sources`

Each activity item contains:

- `activity_id`
- `kind` (`job` or `workflow`)
- `engine` (`orca`, `xtb`, `crest`, or `workflow`)
- `status`
- `label`
- `source`
- `submitted_at`
- `updated_at`
- `cancel_target`
- `aliases`
- `metadata`

The `metadata` mapping is intentionally extensible. Scripts may use known keys
such as `queue_id`, `task_id`, `task_kind`, `run_id`, `workflow_id`,
`reaction_dir`, `job_dir`, `allowed_root`, `priority`, `template_name`,
`workspace_dir`, and `cancel_transitions_pending`, but should tolerate missing
or additional keys. `cancel_transitions_pending` appears on a workflow row while
an undrained cancel status transition is recorded on its workflow payload, and
it names why that row refuses a stale clear. The payload is the authority the
clear guard itself consults: once a worker drains the transitions the key is
gone from the row, even though its registry entry can still cache a count that
no reindex has refreshed. The payload consulted is the one in the workspace the
row names — payload summaries are matched to rows by workspace directory, never
by the `workflow_id` a payload persists, which a second (identity-quarantined)
workspace can also claim. The cached count is reported only for a row whose own
workspace payload could not be read at all. The plain
`queue list` table carries the same rows as a `cancel_pending:` note printed
under it, not as a column.

A terminal row
whose required same-generation terminal evidence cannot be reconstructed is
exposed as `repair_blocked` activity with `repair_blocked_reason` and
`queue_error` metadata instead of being retried indefinitely.

An ORCA row that is already terminal when a worker first observes it is treated
as closed history. Starting or restarting the worker does not regenerate that
row's state/report, replace its `run_id`, `finished_at`, or `error`, or resend its
terminal notification. Terminal side effects are replayed only when the durable
row carries the worker's valid incomplete-replay marker or the current worker
observed that exact row transition from `pending`/`running` to a terminal status.
Side-effect-bearing terminal writers store that marker atomically with the queue
transition; explicit administrative publication fences are excluded from replay.
While a marker is pending, cleanup preserves both its queue generation and run
state. Invalid or unsupported markers fail closed: they are logged and retained
as clear/forced-successor barriers rather than replayed. Replay and fence markers
are internal implementation state and must not be edited by clients.

xTB/CREST queue artifacts carry an internal immutable-generation fingerprint,
and new xTB/CREST/ORCA rows carry a submit-time execution snapshot. New
ORCA rows own one visible direct child named `YYYYMMDD-HHMMSS-<8-hex>/`, use
snapshot schema 2, and do not create an ORCA
`.orca_auto_input_snapshots/`, `.orca_auto_orca_executions/`, or nested
`.inputs/` tree. The bound selected `.inp` and dependencies preserve their
source basenames. Different referenced source paths with one basename always
fail submission closed, even when their bytes match. A dependency may share the
selected input stem when its basename differs and ORCA does not produce that
name: an SP `h2.inp` may refer
to `h2.xyz`, with both names preserved. For routes that write `<stem>.xyz`, a
sole main `* xyzfile` dependency may use that exact name: the bound `.inp`
inlines its coordinates and ORCA may mutate the visible XYZ after launch.
Same-stem auxiliary NEB Product/TS inputs remain unsupported. Frequency routes
reserve `<stem>.hess`; every route reserves `<stem>.out` and `<stem>.gbw`.
Submission also rejects the selected `.inp` basename and generation-owned
`job_state.json` and `machine.json` as dependency basenames. Output-base
overrides such as `%base` and NEB restart-GBW basename controls fail closed.

Only rows carrying the current execution snapshot, publication marker, and
identity fields are executable. Rows that do not satisfy that contract fail
closed. A pending row whose execution snapshot or job-directory identity is
invalid is fenced to `failed` by the worker's publication repair before
admission continues; a pending row that keeps an unrepairable publication
marker stays pending, pauses admission while it exists, and must be cancelled
with `queue cancel`. A terminal row is removed by `queue list clear`, and the
work is resubmitted.
Workflow-internal xTB/CREST snapshots use a unique namespace that is exclusively
reserved for the submission, rather than using the public task id alone as
snapshot ownership.
Generation directories are preceded by an internal durable intent under the
owning queue root. Workers reconcile only bounded, dead-owner intents against raw
queue rows and retire the intent before starting a reserved child; cleanup retains
the intent whenever generation removal is uncertain. These intent files are
implementation state and must not be edited by clients.
ORCA snapshots also reject ambiguous duplicate `%pal`/`nprocs`, `%maxcore`,
orbital-input, and route `PALn` directives before execution. Top-level `%moinp`
and `MOInp` inside `%scf` are one semantic orbital-input namespace. A route or
`%scf` block that requests `MORead` must name exactly one explicit,
snapshot-bound orbital input; implicit current-directory checkpoint lookup is
unsupported. External include/program hooks that are not explicitly
snapshot-bound are unsupported and fail closed.
Crash recovery seeds a same-stem runtime geometry only from the prior verified
generation. It must preserve the submitted atom-label sequence and contain the
declared number of atom rows with exactly three finite numeric coordinates and
no trailing rows. A valid seed does not require the original job-root geometry
still to exist. If that seed is absent or malformed, or if an immutable
dependency must be copied again, the current job-root file is accepted only when
its byte size and SHA-256 still match the original submission; changed, deleted,
or unverifiable dependencies fail recovery closed instead of changing the
calculation being resumed. A parseable seed that changes atom labels or order is
substitution evidence and fails closed rather than falling back. Recovery also
verifies and reuses the snapshotted ORCA executable path, byte size, and SHA-256;
changing the configured executable does not change an existing calculation.

New xTB/CREST terminal artifacts bind retained outputs to SHA-256 and byte-size
identities. Downstream readers verify the current file against that terminal
identity. Completed artifacts without a terminal identity are unsupported and
fail closed; they must be resubmitted. Readers do not backfill an identity from
the bytes observed later, and do not remap stale artifact paths by basename.

Workflow-internal xTB and CREST jobs use `job_state.json` as their internal
terminal state. They do not create an independent `machine.json`, and
adapters, indexing, repair, and workflow diagnostics do not read those files.
Report-only jobs are unsupported and must be resubmitted. This does not
change the separate ORCA report contract below.

## ORCA Job Artifact Contract

The submitted ORCA job root keeps the user inputs, coordination locks, and one
visible execution generation per submission. The generation contains:

- `<generation>/job_state.json` as private mutable/recovery state
- `<generation>/machine.json` as the only public machine metadata file
- `<generation>/job_report.html` when a report renderer applies
- `<generation>/si_block.md` for completed jobs ending on a stationary point
  (a copy-paste Supporting Information block: route, energies,
  thermochemistry, Nimag, coordinates) or for IRC routes (summary-only
  validation block, no coordinates). The block is withheld when the output
  yields no trustworthy final energy or geometry — including a final energy
  line annotated as not fully converged — because a partial SI block is worse
  than none

During a run the root additionally carries the live `job_state.json` (removed
by terminal cleanup once the run is cleared). Reports are published only into
a verified execution generation. A run whose generation cannot be verified,
including a submission rejected before generation binding, gets no report;
its live state and queue record retain the outcome. Readers resolve reports
through the verified generation only. Without a queue row, artifact lookup must
name the exact generation path; a reusable job-root path is not a latest-run
selector. An existing queue row without `task_id` and `run_id` is not allowed
to adopt adjacent state or reports. Queue lookup first requires the complete
canonical ORCA ownership tuple (`app_name`, `engine`, `task_kind`, `queue_id`,
and `task_id`); partial or foreign rows are ignored even for an exact queue-id
or reaction-path selector.

Unbound job-root report files are not runtime inputs. They may be archived or
removed by the operator, but must never be attached to a generation by path.
Once a terminal run's
root state has been cleaned, a from-scratch
job-locations index rebuild no longer rediscovers that run: generation
directories are deliberately excluded from production scans, and the rebuild
is upsert-only, so the live index keeps its record but a rebuild after losing
the index is lossy for cleaned runs.
Job-location matching and rebuilds, queue orphan/replay recovery, and terminal
record repair use the root state or durable queue/index record, never a root
report. A root report-only job neither locates a job nor terminalizes a queue
row. Workflow diagnostic fallback accepts only a direct visible-generation
report whose generation provenance and stage identity both verify.
The public ORCA machine reader likewise accepts only the canonical
`<visible-generation>/machine.json` whose directory owner, inode, artifact
receipts, bound input identity, operation identity, and payload provenance
verify. Its raw JSON loader and `job_state.json` reader are private.

Every new submission owns one visible
`YYYYMMDD-HHMMSS-<8-hex>/` direct child. The name shape is reserved: any
directory named as an ASCII date, time, and 8 lowercase hex digits in that
form is treated as an execution generation at every depth under `runs_root`,
excluded from production scans, and rejected as a `run-dir` submission
target — do not use this shape for your own directories. That directory contains the
bound `.inp` under the exact source basename, supported dependencies under their
exact source basenames, raw ORCA outputs, and the generation's
`job_state.json`, `machine.json`, and (when applicable)
`job_report.html` and `si_block.md`. Generation files retain the record for
the generation they describe. The existence of the root `run.lock`
file alone does not mean its advisory lock is currently owned.

`job_state.json` uses the normalized engine artifact shape below. It is an
orca_auto implementation detail, not a Hermes handoff contract:

- `schema_version`
- `engine`
- `job`
- `status`
- `input`
- `resources`
- `timestamps`
- `recovery`
- `process`
- `artifacts`
- `engine_payload`

Top-level expectations:

- `schema_version` is `1` for the current normalized artifact schema.
- `engine` is `orca` for ORCA job artifacts.
- `job.id` identifies the run when available.
- `job.dir` points at the job directory.
- `status.state` is the job state.
- `status.reason` is the final or current reason when available.
- For snapshot-bound rows, `input.primary_path` is the exact bound ORCA input in
  the visible generation, not the subsequently mutable source path. ORCA
  execution provenance retains the selected source path and bound content
  identities.
- `timestamps.started_at`, `timestamps.updated_at`, and
  `timestamps.finished_at` are UTC-style ISO text when available.
- `artifacts.last_out_path` points at the last ORCA output path when known.
- `engine_payload.run_id`, `engine_payload.max_retries`,
  `engine_payload.attempts`, and `engine_payload.final_result` carry the
  ORCA-specific run details.

The public `machine.json` uses `factory/machine-observation` version 1 and has
exactly nine top-level fields: `contract`, `producer`, `operation`, `lifecycle`,
`handoff`, `delivery`, `artifacts`, `lineage`, and `payload`. ORCA generations
use operation kind `chemistry/orca-run`; workflow roots use
`chemistry/workflow`. Both carry a `chemistry/results-bundle` version 1 payload.

An ORCA-run payload has `result_kind: "engine-run"`, `engine: "orca"`, a compact
`summary`, sanitized `results`, and `artifact_refs`. Artifact receipts use
generation-relative POSIX paths and bind the exact byte count and SHA-256. A
successful ORCA run is ready only when every required receipt, including the
bound input and final ORCA output, is available. Calculation outcome and
delivery remain separate: a successful calculation with a missing required
file is `succeeded / blocked / incomplete`.

Human HTML and SI files are written first. Terminal `machine.json` is written
last and is immutable. Consumers must reject hash or receipt mismatches rather
than reconstructing metadata from nearby files. Internal `job_state.json`, queue
rows, locks, and workflow state are never alternate public machine contracts.

A workflow observation records every verified ORCA `machine.json` that the
workflow consumed directly in `lineage.upstream`, including prerequisite
`relaxed_scan` stages and interaction-energy fan-out stages (metadata role
`interaction_*`) that are intentionally absent from the ranked ORCA results
table. The optional co-located HTML report is not lineage authority. Symlinked,
external, or otherwise unverified machine observations are omitted. Already
published terminal workflow observations are not rewritten or backfilled; this
applies to future publications. When SI regeneration is blocked at terminal
publication and a last known-good `workflow_si.md` exists, it stays pinned and
the observation carries the delivery code `orca_auto/si_publication_blocked`,
meaning the pinned SI may predate the final payload; when none was ever
published, delivery reports `incomplete` with
`orca_auto/required_artifact_unavailable` as usual.

`engine_payload.final_result`, when present, contains:

- `status`
- `analyzer_status`
- `reason`
- `completed_at`
- `last_out_path`
- optional `resumed`
- optional `skipped_execution`
- optional `runner_error`

ORCA run statuses:

- `created`
- `running`
- `retrying`
- `completed`
- `failed`

ORCA analyzer statuses:

- `completed`
- `error_scf`
- `error_scfgrad_abort`
- `error_multiplicity_impossible`
- `error_disk_io`
- `error_memory`
- `error_geometry`
- `geom_not_converged`
- `ts_not_found`
- `incomplete`
- `unknown_failure`

Reason strings are part of issue triage and report interpretation once they are
documented or tested. Important current examples include `normal_termination`,
`existing_out_completed`, `retry_limit_reached`, `interrupted_by_user`,
`worker_shutdown`, `crashed_recovery`, `runner_exception`, `cancel_requested`,
`rewrite_failed`, and `scants_recipes_exhausted`.

When the effective `max_retries` is zero, the first failed attempt is terminal
and its analyzer reason is preserved as the final reason. `retry_limit_reached`
is reserved for exhaustion of a positive retry budget.

## Workflow Contract

Workflow input manifests are named `flow.yaml`.

`flow.yaml` and internal engine YAML job manifests are single-link regular UTF-8
files limited to 1 MiB, 32 alias uses, 10,000 parsed and expanded object-graph
nodes, and 64 nesting levels. Cyclic/recursive alias or object graphs are
rejected before workflow materialization.

Workflow names and IDs must be single path segments and cannot contain `(` or
`)`. An existing workflow directory must not be renamed because its persisted
ID and artifact paths are tied to that directory; create a new workflow under
the new name instead.

Supported workflow templates:

- `reaction_ts_search`, scaffolded by `orca_auto scaffold ts_search`
- `conformer_screening`, scaffolded by `orca_auto scaffold conformer_search`
- `scan_ts_search`, scaffolded by `orca_auto scaffold scan_ts`

Manifest keys that users may rely on:

- `workflow_type`
- `crest_mode`
- `priority`
- `resources.max_cores`
- `resources.max_memory_gb`
- `orca.route_line`
- `orca.charge`
- `orca.multiplicity`
- `crest`
- `xtb`
- `endpoint_pairing`
- `max_crest_candidates`
- `max_xtb_stages`
- `max_orca_stages`
- `scan_coordinate`
- `barrier_threshold_kcal`
- `max_scan_extensions`
- `orca_optts_route_line`
- `boltzmann_temperature_k`
- `rmsd_dedup.enabled`
- `rmsd_dedup.rmsd_threshold_angstrom`
- `rmsd_dedup.energy_window_kcal`
- `rmsd_dedup.heavy_atoms_only`
- `interaction_energy.enabled`
- `interaction_energy.sp_route_line`
- `interaction_energy.max_fragments`
- `interaction_energy.priority`
- `interaction_energy.max_cores`
- `interaction_energy.max_memory_gb`
- `interaction_energy.fragments[].atom_indices`
- `interaction_energy.fragments[].charge`
- `interaction_energy.fragments[].multiplicity`
- `interaction_energy.fragments[].label`
- `allow_external_inputs`

ORCA routes are bound to durable workflow task roles. A
`reaction_ts_search` route and `scan_ts_search`'s
`orca_optts_route_line` must contain the exact active, unquoted `OptTS` token
and one supported active frequency token (`Freq`, `NumFreq`, or `AnFreq`),
without `ScanTS` or `NEB-TS`. A `conformer_screening` route and the relaxed-scan
`orca.route_line` must request a non-TS geometry optimization, and a relaxed
scan input must also carry a valid `%geom Scan` coordinate block. Route values
must be strings containing only route lines; comment-only and blank lines are
discarded, while quoted tokens, `!`/`%`/`*`/`$`-prefixed payload tokens, and any
other active ORCA input line are rejected instead of being rendered. Compact
leading syntax such as `!B3LYP` remains valid. Only active route tokens count:
tokens inside a closed `# ... #` inline comment and after an unmatched `#`
marker are ignored. Workflow creation,
restart rematerialization, pre-submission validation, and completed-stage
acceptance reject a route-role mismatch instead of publishing the result under
the wrong task role. Queue submission requires `reaction_dir` and `selected_inp`
to be present and equal in both durable payload copies, uses the direct
submitter's actual input-selection rule, and requires that selection to equal
the durable selected input. The validator then runs on the final rewritten
input bytes at the execution-snapshot boundary before those same bytes are
written and identity-bound.
Completed-stage acceptance requires the selected input named by the artifact
contract itself and does not substitute a pre-submission task-payload path.

`scan_coordinate` is one complete line using `B`, `A`, or `D` with respectively
two, three, or four distinct zero-based atom indices, two finite unequal
endpoints, and an integer point count of at least two. Every index must exist in
the stage's selected XYZ geometry. The input must contain exactly one closed
`%geom` block with one closed `Scan` sub-block and one active coordinate.
Trailing commands and multiple coordinates are rejected before a workspace is
created. Creation, dynamic scan extension, submission, and completed-result
acceptance reuse this contract. Canonical endpoints use shortest round-trip
float text, so valid precision is not silently rounded to eight decimal places.

Restart may change non-scientific controls, but once a primary ORCA stage has
completed it cannot change the durable route, charge, or multiplicity used by
that stage, and once a CREST or xTB stage has completed it cannot change the
workflow charge or multiplicity away from the electronic state that stage's
job manifest carried. An accepted electronic-state change is recorded in the
restart summary, the restart journal event and the command response; its
`previous` values are null when the workflow never recorded them. Report
aggregation verifies the selected inputs and omits relative
energy comparisons and numeric rankings if route, non-resource active input
directives, electronic-state, or ORCA version provenance is missing or mixed
across completed candidates. The selected inline or confined XYZ also must
prove the same ordered atom-label sequence; coordinates themselves remain
candidate-specific. Every identity-bound non-geometry dependency (for example,
point charges, orbital inputs, or NEB auxiliaries) must also have the same kind
and content identity; private snapshot pathnames do not affect comparison. The
HTML report, SI populations/refinements, and
interaction fan-out's RMSD representative selection use the same bound-input
scientific identity. `%pal`, `%maxcore`, and route `PALn` are resource-only and
do not split that identity. Only an
exact interaction child contract (ORCA single point, recognized role, unique
non-interaction ORCA parent, and valid fragment index when applicable) is
excluded from primary-stage restart and ranking checks; role metadata alone
cannot hide a primary stage.

`max_crest_candidates` is capped at 32 per reaction side. Endpoint pairing
keeps only the requested best pairs while evaluating this bounded Cartesian
space, rather than materializing and sorting every pair. Geometry-metric pairing
compares at most 256 effective atoms and loads each candidate ensemble only once
per selection call.

The `crest` and `xtb` engine-job mappings, `xtb.ts_guess_validation`,
`rmsd_dedup`, and `interaction_energy` use strict schemas: unknown
keys, malformed booleans, non-integral integer fields, non-string routes, and
multiline/control/non-printable route or label text are rejected. Workflow
admission rejects manifest shape, engine input-file paths, `endpoint_pairing`,
`rmsd_dedup`, and `interaction_energy`; an engine-job mapping's own key and
type schema is checked when that engine job is submitted, so an unknown
`xtb.ts_guess_validation` key surfaces at the first xTB stage rather than at
workflow admission. The engine `charge`/`uhf` conflict rule below is checked
earlier, when the workflow is created.
Fragment labels are at most 80 characters. An enabled interaction-energy block
requires 2–8 fragments; each multiplicity is an integer in `[1, 100]`, and
`sp_route_line` must describe a pure single-point calculation. Fragment indices
must be a static, gap-free, disjoint partition of every input atom.
For `reaction_ts_search`, `max_xtb_stages` and `max_orca_stages` are total hard
caps, including stages already attempted before restart. Endpoint-pairing mode
does not disable either cap. Workflow `orca.charge`/`orca.multiplicity` is the
authoritative electronic state; conflicting CREST/xTB `charge` or `uhf` values
are rejected. The exact selected xTB/CREST snapshot must use known elements in
the current GFN range (atomic numbers 1–86), leave a nonnegative electron count,
and have a UHF unpaired-electron count within that total and with matching
parity. A completed CREST stage must expose at least one strictly valid,
finite retained XYZ frame, and overlapping retained files cannot duplicate a
downstream geometry; distinct geometries present only in later valid retained
files remain candidates. Non-finite coordinates or xTB energies are not valid
workflow artifacts.

Local geometry admission is capped at 10,000 atoms. xTB Hessian jobs and ORCA
frequency/Hessian-producing inputs use the stricter 1,000-atom cap.

For trusted local CREST work, an explicit `mdlen` uses a default aggregate
`max_md_steps` budget of 10,000,000. If `mdlen` is omitted, admission evaluates
CREST's automatic-length worst case with a 14,000,000-step default budget;
under the standard non-quick trajectory multiplicity, GFN-FF and `gfn2//gfnff`
therefore require an explicit bounded `mdlen` or an explicit higher step budget
with its high-cost acknowledgement. Every local CREST job is also capped at
50,000,000,000 atom-steps.

Workflow runtime artifacts:

- `workflow.json` is the durable workflow payload.
- `workflow_report.html` is rewritten on workflow advances as a human-facing
  summary.
- `workflow_si.md` is rewritten on workflow advances when
  the workflow has ORCA stages: a paper-ready Supporting Information assembly
  (computational details, relative energies, per-structure blocks).
  A `conformer_screening` population set is emitted
  only after the workflow is terminal and complete and every route-classified
  minimum is converged and has a complete 3N vibrational spectrum with
  `Nimag = 0`, finite electronic/Gibbs energies, and a finite positive
  thermochemistry temperature. Any unfinished,
  failed, or unusable conformer omits the whole set with a note; a usable subset
  is never renormalized to 100%.
- Relative energies and populations use the same effective E/G convention.
  Single-point E is used only with complete, uniform exact-provenance coverage;
  composite G additionally requires complete thermochemical corrections at one
  exact optimization/frequency provenance. Exact provenance includes the
  executed method, basis, solvation, ORCA version, route, charge, and
  multiplicity. Missing optimization/frequency route or ORCA-version evidence
  omits populations; incomplete optional SP provenance disables that refinement.
  Parsed charge/multiplicity must also match the selected input. Mixed or partial
  refinement falls back consistently to the
  applicable optimization-level quantity and is noted. Population members
  within each `formula|charge|multiplicity` group must also share exact
  optimization/frequency provenance.
- Populations are normalized independently within each
  `formula|charge|multiplicity` group. This key is a stoichiometric proxy, not a
  connectivity identity: every retained minimum has statistical weight one, and
  no symmetry/degeneracy correction is applied. When `rmsd_dedup` is enabled,
  completeness and provenance are checked against the full pre-dedup ensemble
  before representatives are selected. The reported degeneracy is a workflow
  duplicate count and is not a statistical/symmetry weight.
  Markdown renders population as percent.
- `rmsd_dedup` compares all atoms by default and considers converged minima;
  a known nonzero `Nimag` excludes a candidate, while Opt-only candidates with
  no frequency result remain eligible. Candidates must have the same
  selected-atom element sequence, formula, charge, multiplicity, and exact optimization provenance
  (method, basis, solvation, ORCA version, and route). A merge requires both a
  proper-rotation RMSD and the maximum aligned-atom displacement to be below
  `rmsd_threshold_angstrom` (default 0.25), plus an effective-energy difference
  below `energy_window_kcal` (default 0.1). A complete uniform exact-provenance
  SP refinement supplies that energy; otherwise the optimization energy does.
  Nondegenerate pairs whose best unconstrained alignment prefers a global
  reflection are kept separate. This remains a geometric/energy heuristic:
  nearby distinct minima,
  especially local stereochemical variants, can still merge. Setting
  `heavy_atoms_only: true` increases that risk by ignoring H/D/T. Review
  merged groups before treating them as chemically identical.
- `interaction_energy` is available only for `conformer_screening`. It requires
  2–8 fragments whose indices form one disjoint, exhaustive partition of the
  optimized complex. Fragment charges must sum to the complex charge. Their
  multiplicities must also be able to couple to the complex multiplicity under
  the generalized angular-momentum spin-coupling manifold. For each fragment,
  `N_e = ΣZ − charge` must be nonnegative and `2S = multiplicity − 1` must not
  exceed `N_e` or differ from it in parity. `sp_route_line` is a
  pure single-point route; optimization, frequency, gradient, IRC, MD, NEB,
  GOAT, and scan job directives are rejected.
- Fan-out uses only valid terminal optimized minima and the RMSD representatives.
  A terminal partial-success ensemble may use its completed, converged subset
  after excluding known saddles. The representative energy convention is computed
  from that same eligible set, so an unusable/saddle stage cannot change the parent.
  When the public dedup report is off, the same all-atom
  default grouping still bounds fan-out while the SI structure table remains
  undeduplicated. The interaction generation fingerprint includes those RMSD
  grouping settings.
- Interaction ownership is fail-closed and config-bound. A child is quarantined
  from primary ORCA results, reused by fan-out, or retired by restart only when
  its canonical SHA-256 generation fingerprint matches the workflow's durable
  interaction configuration. Missing, malformed, or different fingerprints do
  not let arbitrary SP role metadata hide or retire a primary stage.
- A resolved interaction energy requires exactly one completed current-generation
  complex SP and one completed fragment SP at every expected index. The selected
  input and parsed output must agree on route and electronic state; executed
  method, basis, solvation, ORCA version, optimized complex geometry, indexed
  geometry subsets, and the shared energy convention must also agree. Missing,
  duplicate, running, stale-generation, mixed-level, wrong-state, wrong-geometry,
  or non-finite input omits ΔE_int rather than using a partial sum. Reportable
  results land in the `workflow_si.md` interaction-energy section. No separate
  Boys–Bernardi ghost-atom counterpoise calculation is run; that does not deny
  a method's inherent correction such as r2SCAN-3c gCP.
- A restart preserves the interaction SP route, per-fragment electronic state,
  interaction-specific resources, and generation fingerprint. Interaction and
  RMSD grouping settings are immutable after fan-out; reopening an original
  primary stage is also rejected while its interaction fan-out exists. Disabling
  the feature retires those persisted interaction stages. Before accepting an
  enabled config, restart reloads the copied durable input XYZ and revalidates
  its exhaustive partition and per-fragment electron states.
- SI publication is checkpointed in workflow and registry state with
  `si_publish_pending`, `si_publish_attempts`, `si_publish_blocked`, generation,
  and error metadata. A pending publication is retried on every worker cycle and
  blocks after the fifth failed writer attempt. Deterministic conflicts block
  immediately. Pre-writer workflow/registry/report checkpoint failures do not
  consume this writer budget; any successfully persisted pending marker remains
  immediately due for infrastructure reconciliation. Registry clear uses
  workflow-then-registry lock order and rechecks authoritative workflow
  identity/status; publication-pending, publication-blocked,
  final-child-sync-pending, undrained-cancel-transition, identity-quarantined,
  or authoritatively active records cannot be cleared as stale. An identity
  mismatch that has not yet been
  quarantined carries no cached registry marker: it is caught by that
  authoritative recheck, not by cached state, so a row already hidden by a
  cleared marker stays hidden until an operator acts. A quarantined payload
  keeps its observed durable ID as evidence while the registry keys the single row
  by the trusted workspace name and records the observed ID in metadata. After fixing
  the cause, an operator can re-arm a blocked publication with
  `orca_auto run-dir <workflow_dir> --force`, unless the workflow has already
  published its terminal observation. That observation is immutable and pins
  the bytes of `workflow_report.html`, `workflow_si.md` and itself: a workspace
  that has published one regenerates none of the three on any later advance.
  A restart of such a workspace still reopens its failed/cancelled stages and
  its registry row still follows the new status, but the published report, SI
  and observation keep describing the run they were published for. The restart
  records `pinned_by_terminal_observation` in its restart summary and response
  and the CLI prints it, the blocked publication stays blocked, and a
  re-advance retires any pending flag it finds under a published observation
  the same way. A fresh report, SI and observation require a new generation,
  not a restart.
- The population temperature is the parsed thermochemistry temperature. The
  optional `boltzmann_temperature_k` manifest key is a finite, strictly positive
  pin validated at admission and stored in the durable workflow request; it must
  agree with every parsed frequency-job temperature within 0.01 K and cannot
  create thermochemistry at a temperature the jobs did not use. SI reads the
  durable request value rather than a subsequently edited source `flow.yaml`.
  Missing, non-finite, non-positive, or inconsistent data cause populations to
  be omitted rather than fabricated at an assumed temperature.
- `workflow_registry.json` and `workflow_registry.journal.jsonl` support
  cross-workflow listing and semantic event history. Idle polling-cycle
  start/finish rows are not emitted. Bounded recent reads are newest-first by
  the registry-lock append/commit order, not by a later wall-clock sort, and do
  not scan the full journal. `workflow_worker_state.json` is an advisory worker
  snapshot coalesced on semantic changes and a bounded heartbeat; durable
  workflow/queue state and the worker singleton lock remain authoritative.
- xTB/CREST terminal output identities are verified before downstream parsing.
  A single output XYZ handed to a downstream stage has a 512 MiB
  materialization cap; larger output ensembles fail closed rather than being
  loaded without a bound.
- Internal engine queues and outputs live under workflow stage directories such
  as `<runs root>/<workflow_id>/01_crest`, `02_xtb`, and `03_orca`.
  `scan_ts_search` is ORCA-only and uses no engine root: its stages are
  workflow-ordered directories directly under the workspace (`01_scan`,
  `02_scan_maximum`, ...), and no `inputs/` copy of the source geometry is
  kept — the geometry is materialized straight into the first scan stage.

Workflow and stage statuses use the shared status vocabulary where applicable:

- `created`
- `planned`
- `pending`
- `queued`
- `submitted`
- `running`
- `retrying`
- `waiting_for_slot`
- `cancel_requested`
- `cancelled`
- `completed`
- `failed`
- `cancel_failed`
- `submission_failed`
- `unknown`

Workflow-specific reason strings currently used in public reports or triage
include `scan_profile_no_barrier`, `ts_candidates_exhausted`,
`reaction_ts_search_xtb_phase_failed`, `conformers_failed`,
`xtb_ts_guess_missing`, `xtb_ts_guess_geometry_invalid`, and
`xtb_ts_guess_geometry_unvalidated`.

A stage rejected before execution records `reason` (the submitter's reason, or
`queue_submission_failed` when it gives none) and, when the submitter wrote
anything to stderr or stdout, `submission_error_detail` in its stage metadata,
truncated to 1,000 characters. The candidates-exhausted workflow error names
that key as where to read each rejection. A successful resubmission clears both,
so a stage retried after `submission_failed` carries no stale failure text.

## Systemd Contract

Supported unit filenames:

- `systemd/orca_auto-runtime@.target`
- `systemd/orca_auto-engine-workers@.target`
- `systemd/orca_auto-queue-worker@.service`
- `systemd/orca_auto-workflow-worker@.service`

Supported operator commands:

- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto service status`
- `orca_auto service restart`

Behavior:

- Unit templates are loaded from the required `<repo>/systemd` directory named
  by `--repo`, including when the invoking `orca_auto` command is wheel-installed.
- The default `<runs_root>/.admission` directory may be absent at installation:
  the rendered unit grants its existing `runs_root` parent so the worker can
  create the nested directory. A separately configured
  `scheduler.admission_root` must already exist as a directory; otherwise
  installation fails before any unit is written or systemd command is run. A
  root the installing account cannot inspect (a permission error) is left to
  the service rather than treated as missing.
- Literal `%` characters in configured repository, configuration, admission,
  or runs paths are escaped when unit files are rendered; template-owned
  instance specifiers such as `%i` remain active. Paths containing quotes,
  backslashes, or dollar signs are rejected before any unit is written because
  those characters would change systemd tokenization or expansion.
- A full-runtime install enables the runtime target; a worker-only install
  enables the engine-worker target instead. The runtime target currently pulls
  in only the engine-worker target, so both modes start the same unit set.
- The engine-worker target starts the ORCA engine service. An unqualified
  interactive `queue worker` command remains ORCA-only.
  Configuring `runs_root` does not implicitly start workflow or its internal
  xTB/CREST workers.
- The workflow unit is installed but opt-in. Starting it explicitly runs the
  workflow supervisor and its internal xTB/CREST workers.
- `service status` reports the runtime and engine-worker targets, the default
  ORCA engine service, and the opt-in workflow worker. The opt-in worker
  is informational and is not required for worker-only or full-runtime health.
- `service status` also compares the installed distribution metadata against the
  source tree the process imports. When the interpreter running the command was
  installed as an editable install whose metadata froze at an earlier version,
  it reports `version_drift` with the `installed` and `source` versions and the
  `interpreter` it inspected, writes the mismatch and its recovery hint to
  stderr, and exits non-zero. The verdict covers that interpreter only: a host
  may hold several editable installs of one checkout, and the units run whichever
  interpreter their unit files name, so run the command with the interpreter you
  mean to check. `ok` continues to describe unit health alone, so a drifting
  deployment with healthy units reports `ok: true` and still exits non-zero. An
  install with no source `pyproject.toml`, such as a wheel install, has nothing
  to compare and reports `version_drift: null`.
- Each worker records its resolved `orca_auto` module source in its own process
  environment at startup. `service status` reads that import provenance from
  the active main process, binds it to the same PID/start ticks, and compares
  the unit start time with an independently captured, per-worker snapshot of
  that checkout's latest matching HEAD-reflog update, not the commit object's
  timestamp or the status command's checkout or the worker's current directory.
  Every HEAD reflog entry that names the current commit counts, including a
  same-commit checkout or reset: a forced checkout writes the same subject as
  a no-op one but restores the files, so the verdict errs toward stale.
  The imported package tree must also be clean relative to Git; uncommitted
  source changes make the verdict `undetermined`. The units import a
  checkout live but never reload, so
  a deploy that moves HEAD after a worker started leaves that process running a
  torn mix of cached pre-deploy modules and freshly imported post-deploy code.
  Such a worker is reported under `worker_staleness.stale` with its unit, PID,
  checkout, HEAD, and start/update evidence. An active git-backed worker whose
  process, checkout, or matching reflog evidence cannot be inspected is
  reported under `worker_staleness.undetermined` rather than assumed fresh.
  Either finding writes the affected unit and a `service restart` hint to
  stderr and exits non-zero; `ok` continues to describe unit health alone.
  Active non-git workers have no checkout evidence to compare: an all-wheel set
  reports `worker_staleness: null`, while a mixed set lists them under additive
  `worker_staleness.uncompared` without masking verdicts for git-backed workers.
  Inactive units are not judged.
- `service restart` clears the start-limit failure state of every worker service
  it is about to restart. It restarts the runtime target when enabled, otherwise
  the engine-worker target, and then restarts the worker services themselves:
  the ORCA engine service always, and the workflow worker whenever that opt-in
  unit is running, starting up, or failed. A workflow worker that is stopped or
  stopping stays that way; supervision is never opted into on the operator's
  behalf. If the workflow worker's state cannot be read, the command changes
  nothing and exits non-zero.
- Restarting a worker service stops its engine process, so `service restart`
  ends in-flight ORCA work. Run it in an idle window.
- Both service commands address the units of the account that invoked them:
  `SUDO_USER` when it is set and the process is root, otherwise the current
  account.
- A clean engine-worker supervisor exit remains stopped. Each child supervisor
  opens a bounded restart circuit, and systemd applies a bounded delayed restart.

## Non-Contracts

These are outside this document — not documented behavior at all, and never safe
to depend on:

- Private Python functions and modules, including helper modules under
  `src/orca_auto`.
- Internal worker child command lines.
- Exact terminal table widths, colors, icons, and wrapping.
- Exact HTML or Markdown report layout.
- Cache directories and local tool artifacts.
- Raw ORCA, xTB, or CREST output formatting.
- Any private scheduler, MPI, module-system, or workstation policy.

When in doubt, document a desired behavior here before writing external scripts
that depend on it.
