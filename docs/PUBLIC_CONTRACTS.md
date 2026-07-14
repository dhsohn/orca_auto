# Public Contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

This document names the orca_auto surfaces that users, operators, and future
contributors may reasonably depend on. It is intentionally narrower than the
full implementation: internal modules, private helper functions, and runtime
plumbing may change when the documented behavior stays intact.

orca_auto is still in the 0.x series. Breaking changes can happen, but changes
to the contracts below should be deliberate, tested, documented, and called out
in [CHANGELOG.md](../CHANGELOG.md).

## Compatibility Level

- Existing documented commands, config keys, artifact names, and status strings
  should not be renamed or removed casually.
- Additive JSON fields are allowed. Consumers should ignore unknown fields.
- Human-oriented Markdown, HTML, and terminal table formatting may change
  without a migration path; use `--json` or JSON artifacts for scripts.
- Internal worker entrypoints and Python helper modules are not a stable public
  API unless this document or [docs/REFERENCE.md](REFERENCE.md) says otherwise.
- Real ORCA behavior that cannot be proven in public CI should be recorded as
  manual acceptance evidence, following [docs/VALIDATION.md](VALIDATION.md).

## Runtime Contract

Supported runtime assumptions:

- Python 3.11 or newer.
- Native Linux or WSL2.
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
  check. ORCA and workflow xTB/CREST versions remain operator-qualified. The
  standalone xTB-MD adapter is the exception: it probes and currently accepts
  exactly stable xTB 6.7.1 before queueing.

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
- `orca_auto scan-notify`
- `orca_auto smoke`

Stable behavior:

- `run-dir` is queue-first. New work is enqueued durably and executed later by a
  supervised worker.
- A successful new submission returns `status: queued`.
- Closing the submitting terminal after a successful queue submission is safe.
- `queue cancel` accepts the visible activity id plus known aliases such as
  workflow id, queue id, run id, or path aliases.
- `queue list --json`, `queue cancel --json`, and `service status --json` are
  the script-friendly surfaces.
- `queue list --watch` is human-oriented and does not support `--json`.
- `smoke` is a source-checkout developer command. With no options it runs the
  fake profile and uses the discovered shared config's `runs_root`; missing
  repository tests, Git metadata, or `runs_root` fails closed.

Non-contract CLI surfaces:

- `orca_auto queue worker` and `python -m ...worker_child` are runtime plumbing.
  Users should normally manage long-running workers through `systemd`.
- Hidden `systemd install` flags exist for tests and maintenance; they are not
  the supported operator interface unless documented in the reference.
- `python -m orca_auto.smoke` and `scripts/smoke.sh` are maintenance
  entrypoints; the shell wrapper pins its checkout, while user-facing
  documentation uses `orca_auto smoke`.

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
- `scheduler.max_active_xtb_md`
- `scheduler.admission_root`
- `workflow.paths.xtb_executable`
- `workflow.paths.crest_executable`
- `messenger.provider` (`telegram` or `discord`)
- `messenger.telegram.bot_token`
- `messenger.telegram.chat_id`
- `messenger.telegram.allowed_user_ids`
- `messenger.telegram.timeout_seconds`
- `messenger.telegram.max_attempts`
- `messenger.telegram.retry_backoff_seconds`
- `messenger.discord.bot_token`
- `messenger.discord.channel_ids`
- `messenger.discord.default_channel_id`
- `messenger.discord.allowed_user_ids`
- `messenger.discord.timeout_seconds`
- `messenger.discord.max_attempts`
- `messenger.discord.retry_backoff_seconds`
- `orca.runtime.default_max_retries`
- `orca.paths.orca_executable`

Stable behavior:

- `runs_root` is the single runs root: standalone ORCA/xTB-MD jobs and workflow
  workspaces both live under it.
- The shared admission directory defaults to `<runs_root>/.admission` unless
  `scheduler.admission_root` is set.
- `scheduler.max_active_simulations` caps active ORCA, standalone xTB-MD,
  internal xTB, and internal CREST jobs together.
- `scheduler.max_active_xtb_md` is a positive standalone xTB-MD subcap and
  defaults to `1` when omitted.
- Explicit `scheduler`, `resources`, `workflow`, and `workflow.paths` sections
  must be mappings. `scheduler.admission_root` must be an absolute Linux path,
  and explicit scheduler/resource limits must be positive integers; malformed
  execution controls are rejected rather than defaulted.
- `orca.runtime.default_max_retries: 0` disables ORCA retries.
- A positive `default_max_retries` enables calculation-type retry policy, still
  capped by ORCA route type.
- Outbound Telegram delivery requires `messenger.provider: telegram` and non-empty
  `messenger.telegram.bot_token` and `messenger.telegram.chat_id` values.
- Canonical Discord delivery uses `messenger.discord.bot_token` plus
  `default_channel_id`; `channel_ids` authorizes inbound channels. The interactive gateway
  additionally requires a non-empty `allowed_user_ids` operator allowlist.
- Both adapters bound delivery timeouts to 0.1–120 seconds, total attempts to 1–10,
  and configured retry backoff to 0–120 seconds.

Migration note:

- During the current compatibility window, readers accept both the legacy top-level
  `telegram:` block and canonical `messenger.telegram`. If both are present, the nested
  `messenger.telegram` values take precedence.
- New configuration, generated examples, and tooling write `messenger.telegram`; do not add
  new top-level `telegram:` blocks. Discord has no legacy alias: use the nested
  `messenger.discord` bot fields.

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
- `engine` (`orca`, `xtb_md`, `xtb`, `crest`, or `workflow`)
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
`reaction_dir`, `job_dir`, `allowed_root`, `priority`, `template_name`, and
`workspace_dir`, but should tolerate missing or additional keys. A terminal row
whose same-generation state/report pair cannot be reconstructed is exposed as
`repair_blocked` activity with `repair_blocked_reason` and `queue_error`
metadata instead of being retried indefinitely.

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

xTB-MD/xTB/CREST queue artifacts carry an internal immutable-generation fingerprint,
and new xTB-MD/xTB/CREST/ORCA rows carry a submit-time execution snapshot. Before
upgrading from a build without these fields, deployments must either drain those
rows under the old build or cancel/clear and resubmit them under the new build.
In-place adoption of pre-snapshot queue rows is not supported; unverifiable
artifacts fail closed instead of being attached to a newer generation.
xTB-MD/xTB/CREST snapshots use a unique namespace that is exclusively reserved for the
submission, rather than using the public task id alone as snapshot ownership.
Generation directories are preceded by an internal durable intent under the
owning queue root. Workers reconcile only bounded, dead-owner intents against raw
queue rows and retire the intent before starting a reserved child; cleanup retains
the intent whenever generation removal is uncertain. These intent files are
implementation state and must not be edited by clients.
ORCA snapshots also reject ambiguous duplicate `%pal`/`nprocs`, `%maxcore`,
`%moinp`, and route `PALn` directives before execution. External include/program
hooks that are not explicitly snapshot-bound are unsupported and fail closed.

New xTB/CREST terminal artifacts bind retained outputs to SHA-256 and byte-size
identities. Downstream readers verify the current file against that terminal
identity. A completed legacy artifact without an identity can be read only after
the reader computes and marks an `identity_backfilled_from_legacy_artifact`
identity; this proves the bytes seen at read time, not the bytes that existed at
the historical terminal transition.

## Standalone xTB-MD Job Contract

The public input marker is `xtb_md_job.yaml` in a directory under `runs_root`.
It is strict schema version 1. Required fields are `schema_version`, `input_xyz`,
`gfn`, `ensemble`, `temperature_k`, `time_ps`, `walltime_seconds`, `step_fs`, and
`dump_fs`; unknown fields are rejected. NVT and NVE are the only ensembles.
See [REFERENCE.md](REFERENCE.md) §7.2 for validated optional fields and exact
server-owned limits.

Each submission is one fresh generation and one attempt. There is no workflow,
automatic retry, checkpoint resume, arbitrary seed, `--omd`, raw xcontrol,
constraint, or metadynamics contract. Cancellation is terminal; service
interruption, crash, or orphan recovery must not silently requeue the attempt.

The adapter currently accepts exactly xTB 6.7.1. This is a compatibility pin,
not an issue-free claim. Return code 0 and `xtbmdok` alone do not prove success:
the adapter also requires fresh, finite, atom-consistent `xtb.trj` and
`mdrestart` evidence within the submitted budgets and rejects known
false-success markers.

Standalone xTB-MD writes these public artifacts at the job root:

- `job_state.json`
- `job_report.json`
- `job_report.md`

The immutable generated input, logs, `xtb.trj`, `mdrestart`, and `xtbmdok` are
retained under `.orca_auto_xtb_md_executions/<job_id>/`. Terminal JSON binds
validated outputs to path, SHA-256, byte size, and modification time.

## ORCA Job Artifact Contract

Completed, failed, cancelled, and skipped ORCA jobs write artifacts next to the
job directory:

- `job_state.json`
- `job_report.json`
- `job_report.md`
- `job_report.html` when a report renderer applies
- `si_block.md` for completed jobs ending on a stationary point (a copy-paste
  Supporting Information block: route, energies, thermochemistry, Nimag,
  coordinates) or for IRC routes (summary-only validation block, no coordinates)

`job_state.json` and `job_report.json` use the normalized engine artifact shape:

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

Stable top-level expectations:

- `schema_version` is `1` for the current normalized artifact schema.
- `engine` is `orca` for ORCA job artifacts.
- `job.id` identifies the run when available.
- `job.dir` points at the job directory.
- `status.state` is the job state.
- `status.reason` is the final or current reason when available.
- For snapshot-bound rows, `input.primary_path` is the exact private ORCA input
  that executed, not the subsequently mutable source path. ORCA execution
  provenance retains the selected source path and bound content identities.
- `timestamps.started_at`, `timestamps.updated_at`, and
  `timestamps.finished_at` are UTC-style ISO text when available.
- `artifacts.last_out_path` points at the last ORCA output path when known.
- `engine_payload.run_id`, `engine_payload.max_retries`,
  `engine_payload.attempts`, and `engine_payload.final_result` carry the
  ORCA-specific run details.

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

`max_crest_candidates` is capped at 32 per reaction side. Endpoint pairing
keeps only the requested best pairs while evaluating this bounded Cartesian
space, rather than materializing and sorting every pair. Geometry-metric pairing
compares at most 256 effective atoms and loads each candidate ensemble only once
per selection call.

The `crest` and `xtb` engine-job mappings, `xtb.ts_guess_validation`,
`rmsd_dedup`, and `interaction_energy` use strict schemas: unknown
keys, malformed booleans, non-integral integer fields, non-string routes, and
multiline/control/non-printable route or label text are rejected at admission.
Fragment labels are at most 80 characters. An enabled interaction-energy block
requires 2–8 fragments; each multiplicity is an integer in `[1, 100]`, and
`sp_route_line` must describe a pure single-point calculation. Fragment indices
must be a static, gap-free, disjoint partition of every input atom. Remote
workflow uploads may not set the server-owned `interaction_energy.priority`.
The former xTB `namespace` option is not part of the canonical artifact
contract: an absent or empty compatibility field is harmless, but a non-empty
value is rejected and must be removed before resubmission.

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
frequency/Hessian-producing inputs use the stricter 1,000-atom cap. Remote
Discord workflow and ORCA uploads use a 200-atom cap.

For trusted local CREST work, an explicit `mdlen` uses a default aggregate
`max_md_steps` budget of 10,000,000. If `mdlen` is omitted, admission evaluates
CREST's automatic-length worst case with a 14,000,000-step default budget;
under the standard non-quick trajectory multiplicity, GFN-FF and `gfn2//gfnff`
therefore require an explicit bounded `mdlen` or an explicit higher step budget
with its high-cost acknowledgement. Every local CREST job is also capped at
50,000,000,000 atom-steps. Remote workflow ingress
injects the server-owned `mdlen: 5.0` ps and rejects work above 50,000,000
atom-steps; uploaded manifests cannot override the CREST runtime/cost controls.

Workflow runtime artifacts:

- `workflow.json` is the durable workflow payload.
- `workflow_report.html` is rewritten on workflow advances as a human-facing
  summary.
- `workflow_si.md` and `si_data.csv` are rewritten on workflow advances when
  the workflow has ORCA stages: a paper-ready Supporting Information assembly
  (computational details, relative energies, per-structure blocks) and its
  machine-readable companion. A `conformer_screening` population set is emitted
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
  before representatives are selected. The reported `degeneracy` is a workflow
  duplicate count and is not a statistical/symmetry weight.
  `si_data.csv` appends five columns after `warnings` (`cluster_key`,
  `rel_E_kcalmol`, `rel_G_kcalmol`, `boltzmann_T_K`,
  `boltzmann_population`); the existing columns keep their names, order, and
  index. Markdown renders population as percent, while
  `boltzmann_population` is the fraction in `[0, 1]`. CSV `rel_E_kcalmol` and
  `rel_G_kcalmol` are relative to the lowest E and G within that row's
  population group under the shared convention, not global cross-group
  baselines.
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
  `merged_stage_ids` before treating groups as chemically identical. When
  enabled, `si_data.csv` appends `rmsd_group`, `degeneracy`, and
  `merged_stage_ids`; when disabled, those columns are absent.
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
- A resolved interaction energy requires exactly one completed current-generation
  complex SP and one completed fragment SP at every expected index. The selected
  input and parsed output must agree on route and electronic state; executed
  method, basis, solvation, ORCA version, optimized complex geometry, indexed
  geometry subsets, and the shared energy convention must also agree. Missing,
  duplicate, running, stale-generation, mixed-level, wrong-state, wrong-geometry,
  or non-finite input omits ΔE_int rather than using a partial sum.
- `interaction_energy.csv` is present only for an enabled feature with reportable
  rows. Its 23 columns are `parent_stage_id`, `complex_stage_id`, `complex_label`,
  `complex_charge`, `complex_multiplicity`, `complex_formula`, `E_complex_Eh`,
  `method`, `basis_set`, `solvation`, `orca_version`, `route_line`,
  `ghost_counterpoise_applied`, `fragment_label`, `fragment_stage_id`,
  `fragment_atom_indices`, `fragment_formula`, `fragment_charge`,
  `fragment_multiplicity`, `E_fragment_Eh`, `dE_int_Eh`, `dE_int_kcalmol`, and
  `note`. `ghost_counterpoise_applied=false` means no separate Boys–Bernardi
  ghost-atom counterpoise calculation was run; it does not deny a method's
  inherent correction such as r2SCAN-3c gCP. Formula-leading text is neutralized
  for spreadsheet safety.
- The adjacent owner marker binds the generated CSV to a hashed workflow identity
  and records current/pending content digests. Digest-bound ownership logic
  recovers safely from interrupted create, replace, or delete operations. A missing,
  foreign, malformed, or digest-mismatched marker never authorizes overwrite or
  deletion; user-modified content is preserved and released from ownership.
  Ownership conflicts are preflighted before replacing the last-good base SI.
  Uploaded archives may not supply either generated file.
- A restart preserves the interaction SP route, per-fragment electronic state,
  interaction-specific resources, and generation fingerprint. Interaction and
  RMSD grouping settings are immutable after fan-out; reopening an original
  primary stage is also rejected while its interaction fan-out exists. Disabling
  the feature retires those persisted interaction stages. Before accepting an
  enabled config, restart reloads the copied durable input XYZ and revalidates
  its exhaustive partition and per-fragment electron states.
- SI publication is checkpointed in workflow and registry state with
  `si_publish_pending`, `si_publish_attempts`, `si_publish_next_retry_at`,
  `si_publish_blocked`, generation, and error metadata. Transient failures use
  30/60/120/240-second exponential backoff and block after the fifth failed
  writer attempt. Deterministic conflicts block immediately. Pre-writer
  workflow/registry/report checkpoint failures do not consume this writer budget;
  any successfully persisted pending marker remains immediately due for infrastructure
  reconciliation. Registry clear uses workflow-then-registry lock order and
  rechecks authoritative workflow identity/status; publication-pending,
  publication-blocked, final-child-sync-pending, identity-quarantined, or
  authoritatively active records cannot be cleared as stale. A quarantined payload
  keeps its observed durable ID as evidence while the registry keys the single row
  by the trusted workspace name and records the observed ID in metadata. After fixing
  the cause, an operator can re-arm a blocked publication with
  `orca_auto run-dir <workflow_dir> --force`.
- The population temperature is the parsed thermochemistry temperature. The
  optional `boltzmann_temperature_k` manifest key is a finite, strictly positive
  pin validated at admission and stored in the durable workflow request; it must
  agree with every parsed frequency-job temperature within 0.01 K and cannot
  create thermochemistry at a temperature the jobs did not use. SI reads the
  durable request value rather than a subsequently edited source `flow.yaml`.
  Missing, non-finite, non-positive, or inconsistent data cause populations to
  be omitted rather than fabricated at an assumed temperature.
- `workflow_registry.json` and `workflow_registry.journal.jsonl` support
  cross-workflow listing and event history.
- xTB/CREST terminal output identities are verified before downstream parsing.
  A single output XYZ handed to a downstream stage has a 512 MiB
  materialization cap; larger output ensembles fail closed rather than being
  loaded without a bound.
- Internal engine queues and outputs live under workflow stage directories such
  as `<runs root>/<workflow_id>/01_crest`, `02_xtb`, and `03_orca`.

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
`reaction_ts_search_xtb_phase_failed`, `conformers_failed`, and
`xtb_ts_guess_missing`.

## Systemd Contract

Supported unit filenames:

- `systemd/orca_auto-runtime@.target`
- `systemd/orca_auto-queue-worker@.service`
- `systemd/orca_auto-bot@.service`

Supported operator commands:

- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto service status`
- `orca_auto service restart`

Stable behavior:

- The installer enables the queue worker.
- The selected Telegram/Discord bot is enabled only when its interactive config is complete;
  otherwise the install remains worker-only.
- `service status` reports the runtime target, queue worker, and bot status.
- `service restart` restarts the runtime target when enabled; otherwise it
  restarts the queue worker.

## Non-Contracts

These are intentionally outside the stable public surface:

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
