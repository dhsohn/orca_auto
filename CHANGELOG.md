# Changelog

All notable changes to orca_auto are documented in this file.

This project follows a lightweight [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
style. Version numbers are recorded in `pyproject.toml`; release procedure lives
in [docs/RELEASE.md](docs/RELEASE.md).

## [Unreleased]

### Changed

- The `FINAL SINGLE POINT ENERGY` marker is parsed by one shared, line-anchored
  pattern (with Fortran D-exponent support and the real ORCA near-converged
  annotation captured separately) across the ORCA parser, the queue progress
  summary, opt-progress scanning, the NEB report, and the workflow report's
  bounded output-tail fallback. The previously loose copies could match the
  phrase mid-line and crash on malformed number fragments; annotated
  "(SCF not fully converged!)" values still feed progress and parsing as
  before, while the workflow report's fallback keeps rejecting them, and a
  non-finite exponent value is now rejected everywhere instead of silently
  truncating to a wrong number.

- ORCA submission and its worker's publication repair now run on the shared
  enqueue-publication driver, completing the unification of all three
  engines. An ORCA enqueue that committed but lost its result is parked
  `repair_pending` for the worker's pre-claim repair pass instead of
  continuing publication inline under a REPAIRING lease, and its queued
  notification is then not sent (at-most-once); ambiguous multi-row recovery
  still terminally fences every candidate and reports
  `queue_enqueue_outcome_unknown`; the worker repair now claims with a
  freshly minted token (hard-fencing the original publisher) under a single
  publication-lock acquisition, and can no longer misreport a COMPLETE
  written by another lease as its own. A completion durability error is now
  reported as a deferred publication (the row is durably COMPLETE and the
  repair pass short-circuits) instead of an inline "durable COMPLETE state
  recovered" success detail.
- The workflow xtb/crest submitters now run on the shared enqueue-publication
  driver as well. Their COMPLETE short-circuit is token-verified (a COMPLETE
  written by another lease no longer counts as this publisher's success), the
  submit-side repair holds one publication lock across claim, publish, and
  completion instead of two separate acquisitions, and an enqueue that
  committed but lost its result is parked `repair_pending` for the worker's
  pre-claim repair pass instead of being published immediately by the
  submitter (its queued notification is then not sent, consistent with
  at-most-once delivery); a recovery scan failure is reported as a failed
  submission with the row already parked, never as success.
- The durable enqueue-publication protocol now has a single shared driver in
  the core queue package; standalone xTB-MD submission is the first engine on
  it. Behavior converges on the safest of the three previous copies: a failed
  queued-record publication parks the row as `repair_pending` for the
  worker's pre-claim repair pass instead of terminally failing the job (the
  old `submission_publication_failed` result is gone — the submission reports
  `"status": "queued"` with `"publication": "deferred"` and a warning), a
  COMPLETE sync lease written by another publisher is treated as ownership
  loss rather than one's own success, an enqueue whose commit outcome cannot
  be determined is reported as `queue_enqueue_outcome_unknown`, and an
  enqueue that committed but lost its result is recovered by strict identity
  matching and parked for repair.
- The smoke review packet's Markdown/HTML rendering moved into an internal
  render-only module; the discovery, projection, and verification logic stays
  in the review module. The rendered `summary.md` and `review/index.html`
  output is byte-identical.
- The smoke package's descriptor-anchored safety checks — directory identity
  capture and re-verification, the pinned open flags, and the bounded
  change-detecting reads of regular files — now live in one internal module
  shared by the runner, manifest, and review code instead of three parallel
  private implementations. Review artifact reads now open files with
  `O_NONBLOCK` like the rest of the package, so a file swapped for a FIFO
  between discovery and open is rejected instead of blocking the reviewer.
- The smoke manifest module no longer exports the test-only wrappers
  `artifact_counts`, `observe_terminal`, and `create_batch_directory`, and its
  writers require the pinned directory descriptor the runner always supplies
  (the path-based fallback branches are gone). The directory-pinning helpers
  the runner uses are plain public names now instead of underscore-private
  imports.
- The smoke runner now removes pytest's transient `*current` convenience
  symlinks after each scenario instead of rewriting them to durable targets,
  and the review packet's dedicated hidden-alias pipeline is gone: the
  `hidden_harness_alias_count` fields, the `alias_target_*` provenance
  columns, and the `H`-numbered artifact ids no longer appear in
  `batch.json`/`artifacts.json`. The numbered pytest directories keep the
  real artifacts; any symlink that still appears in a runtime (for example in
  batches recorded before this change) is listed as an ordinary blocked
  entry and is never followed.
- The smoke review packet and manifests publish through one shared
  staging-and-rename write. The hardlink backup/rollback layers, the repeated
  post-publication re-verification passes, and the staging-substitution
  defenses are gone: smoke outputs live in an owner-only directory whose only
  writer is the suite itself and are regenerable from the retained runtime,
  so a failed publication now surfaces as an error instead of restoring the
  previous surfaces. The no-follow discovery, symlink/hardlink blocking,
  bounded budgets, redaction, and fail-closed verdicts are unchanged.
- Smoke source provenance no longer content-hashes untracked files (up to
  256 MiB per file, twice per suite). Untracked file names still reach the
  identity through the git status digest; tracked changes keep their diff
  digest.
- New standalone ORCA submissions now create one visible
  `generation-YYYYMMDD-HHMMSS-<8-hex>` directory directly under the submitted
  job directory. The bound `.inp`, supported referenced inputs under their
  original basenames, and raw ORCA outputs all live at that one level; new ORCA
  submissions no longer create `.orca_auto_orca_executions/`, a nested
  `.inputs/`, or an ORCA `.orca_auto_input_snapshots/` tree. Referenced files
  from different source paths that have the same basename always fail
  submission, even when their bytes match. A sole main same-stem `* xyzfile`
  geometry is inlined into the bound input and
  remains visible under its exact basename for ORCA to update; same-stem
  auxiliary NEB Product/TS inputs remain rejected. Dependencies whose basenames
  are reserved for base, retry, or checkpoint-resume runtime inputs and outputs
  are also rejected before the generation is submitted.
- A fully closed standalone ORCA job directory can be submitted again without
  replacing prior results. Each submission receives a new sibling generation,
  while an active row or incomplete terminal replay still blocks a successor.
  `job_state.json` and `job_report.json` remain the latest public summaries at
  the job root and are mirrored into the generation they describe.
- Visible generations retain an invisible filesystem owner token. State/report
  mirroring, historical lookups, cleanup, and DFT discovery verify that token so
  a deleted and recreated same-name directory is not mistaken for the submitted
  generation, even if a filesystem reuses its inode.

Deployment upgrade note: before switching to this ORCA generation format, drain
old-build pending and active ORCA rows and finish every terminal replay and
snapshot intent, or cancel/clear and resubmit them after the upgrade. Existing
terminal hidden generations are retained as history; there is no in-place
migration.

### Removed

- The retired pre-neutral Telegram bot stack (`orca_auto.flow.telegram`: the
  legacy `python -m orca_auto.flow.telegram.bot` entrypoint, its duplicate
  `/list`/`/cancel`/`/help` handler layer, keyboards, dispatch, the
  environment-variable-only credential path, and the `bot_api` transport
  facade). The provider-neutral bot (`python -m orca_auto.flow.bot.runner`,
  the path the systemd unit already uses) is the only bot implementation; the
  Telegram polling adapter now talks to the core transport client directly.
- The legacy top-level `telegram:` configuration block is no longer read.
  Configuration loading now fails with a pointed error when one is present —
  move the block to `messenger.telegram`. Previously the legacy block was
  accepted during a compatibility window and silently ignored when the
  canonical nested block was also present.

### Fixed

- The xTB-MD queue worker now repairs committed submissions whose queued
  record was never published before it will claim any work, matching the
  repair pass the other engines already had. Previously a submitter killed
  between the durable enqueue commit and the record publication left a stale
  lease that was eventually claimed and run without any published record.
  Live publisher leases are left untouched, and a row whose repair fails is
  parked repair-pending and stays unclaimable instead of running.
- `queue list --watch` now keeps system and per-job CPU/RAM sampling active on a
  real terminal when `NO_COLOR` or `--no-color` disables ANSI painting; piped,
  JSON, and messenger output remain unchanged. Per-job CPU counters also retain
  waited-for child CPU to reduce dropped busy refreshes during short-lived
  ORCA/xTB/CREST subprocess churn.
- Restarting the ORCA worker no longer treats historical terminal queue rows as
  fresh active-to-terminal transitions, so stable state/report provenance,
  `run_id`, timestamps, failure reasons, and terminal notifications are preserved;
  supported terminal writers now persist incomplete side-effect evidence with the
  queue transition so real crash windows still recover without replaying
  administrative publication fences.
- ORCA execution snapshots now render simple `* xyzfile` geometry paths without
  quotes, bind and rematerialize the official `%neb Product` and `TS` files,
  enforce their geometry admission cap, and preserve the analyzer reason when a
  no-retry run fails on its first attempt.
- The workflow SI no longer downgrades a stage to unverified provenance when its
  selected input splits the route across multiple `!` lines: the SI and the
  interaction-energy fan-out now share one selection contract
  (`flow/conformer_selection.py`) for route normalization, geometry tolerance,
  minimum eligibility, single-point pairing, and RMSD representatives, so such
  stages participate in SP pairing, populations, RMSD dedup, and ΔE_int again.
- ORCA restart rematerialization now scans input file references with the same
  scanner execution binding uses (`orca/input_blocks.py`), so references only
  the execution side recognized before — the spaced `% moinp` form and block
  keys such as `moinp`, `hess_filename`, `neb_restart_xyzfile`, and
  `restart_allxyzfile` — are copied into the restart directory and rewritten
  instead of silently left pointing into the previous reaction directory.
  Restart also now fails closed at rematerialization time on unsupported
  auxiliary/external-program directives and on more than 128 references
  (previously such hand-edited inputs restarted and only failed at re-run).
- Molecule keys no longer derive a plausible-but-wrong Hill formula from a
  truncated or corrupt XYZ geometry: an unreadable header, a missing atom
  line, or an unparseable atom line now falls back to the directory-name key
  instead of silently skipping lines. Interaction-energy assembly likewise
  treats absent or corrupt fragment electronic-state metadata as a blocker
  instead of defaulting it to the expected value, and corrupt request
  charge/multiplicity values now fail the feature closed instead of being
  silently read as 0/1.

## [0.2.1] - 2026-07-14

This patch release adds retained smoke-review packets and interactive queue
resource visibility, and hardens real-engine smoke admission cleanup.

### Added

- Added `orca_auto smoke`, with 11 retained fake ORCA, standalone xTB-MD, and
  workflow success/fail-closed scenarios plus opt-in real ORCA and real xTB
  profiles. Each batch is isolated under `<runs_root>/.orca_auto_smoke` and
  includes a short-path offline HTML/Markdown review packet with provenance,
  bounded previews/copies, and expected-versus-observed terminal states.
- `queue list --watch` now shows a live system resource line on an interactive
  terminal — CPU utilization, RAM used/total, and load average with colored
  block-bar gauges — sampled from Linux `/proc` between refreshes with no new
  dependency. It fails closed: on a host without a readable `/proc` (or for any
  field that cannot be read) the line is simply omitted, and it never appears in
  piped, `--json`, or `--no-color` output.
- `queue list --watch` also annotates each running job with its own CPU% and
  resident memory on an interactive terminal, across every engine (ORCA, internal
  xTB/CREST, and standalone xTB-MD). Attribution reuses the engine PID/PGID the
  worker already records in the durable admission slot — validated against its
  boot id and process start ticks so a recycled PID/PGID is never mis-attributed —
  and aggregates `/proc` by process group. Same fail-closed, terminal-only rules
  as the system line.

### Changed

- Interactive Telegram and Discord command replies now share the rich
  notification presentation, while retaining plain-text fallbacks and preserving
  queue or workflow IDs in successful submission replies.
- Reserved `.orca_auto_smoke` trees are excluded from production submission,
  discovery, reindexing, snapshots, and cleanup. Queue publication and
  ORCA/xTB-MD snapshot rollback now preserve path identity and fail closed
  across post-commit and namespace-replacement races; workflow reports use a
  bounded ORCA-output tail fallback when no `.engrad` energy is available.
- `queue list` now renders a styled view on an interactive terminal: a summary
  band with per-status counts, box-drawing tree connectors (`├─`/`└─`) for
  workflow children, and a status-colored left rail, plus a spinner and clock in
  the `--watch` banner. The styling is terminal-only — piped output, `--json`,
  `NO_COLOR`, and `--no-color` keep the previous plain, byte-stable table
  (including the `active_simulations:` line and plain indentation), and the
  messenger `/list` view is unchanged.

### Fixed

- Cleanup-proven real-engine smoke startup failures now return pending admission
  leases to the idle state before release, so a pre-launch supervisor failure
  cannot leave smoke capacity stuck until manual cleanup.

## [0.2.0] - 2026-07-13

Second tagged release. This release adds standalone xTB molecular dynamics,
strengthens durable queue execution and recovery, and expands the workflow and
messenger surfaces introduced after `v0.1.0`.

Deployment upgrade note: drain active and pending pre-snapshot internal-engine
queue rows with the old build before switching, or cancel, clear, and resubmit
them after upgrading. The generation-bound workers in 0.2.0 intentionally reject
legacy rows that lack immutable execution snapshots or generation fingerprints.

### Added

- Standalone xTB molecular dynamics is now a first-class, queue-first engine,
  independent from ORCA and workflow orchestration. A strict `xtb_md_job.yaml`
  admits bounded NVT or NVE jobs, snapshots the geometry, manifest, executable
  identity, environment, and resource request, and runs one private supervised
  attempt. Success requires the requested step count, both normal-termination
  markers, a fresh `xtbmdok`, a complete finite `xtb.trj`, and a valid finite
  `mdrestart`; return code zero alone is not sufficient. The adapter accepts
  exactly stable xTB 6.7.1. Workflow integration, retry, and resume remain out
  of scope for this release.
- The messenger stack now has provider-neutral notification and interactive-bot
  contracts with Telegram and Discord adapters. Discord notifications no longer
  depend on the removed webhook-only path, and interactive commands share the
  same actor-bound action surface across providers.
- Reaction workflows can hand a validated xTB Hessian to ORCA OptTS through
  `InHess Read`. TS guesses are screened for finite geometry, plausible reacting
  distances, and duplicate structures before ORCA fan-out.
- `conformer_screening` gained two optional, fail-closed manifest blocks. `rmsd_dedup:`
  collapses DFT-degenerate optimized minima to one lowest-energy representative using
  all atoms by default; comparable candidates require converged results without a known
  imaginary mode and exact
  optimization provenance, while merging requires low proper-rotation RMSD, low maximum
  aligned-atom displacement, and a small energy gap. Nondegenerate global reflection-preferred pairs stay
  separate, but this remains a heuristic that can merge nearby distinct/local stereochemical
  minima—inspect `merged_stage_ids`, especially with `heavy_atoms_only: true`. When enabled it
  appends `rmsd_group`, `degeneracy`, and `merged_stage_ids` to `si_data.csv` (the file is
  unchanged when disabled) and notes the merge in the relative-energy table.
  `interaction_energy:` reports ΔE_int = E(complex) − Σ E(fragment) from fresh
  same-level single points on the optimized geometry for 2–8 fragments that exhaustively
  partition the complex, conserve charge, have electron-count-compatible multiplicities,
  and can spin-couple to the complex multiplicity.
  Only a pure-SP `sp_route_line` is accepted. It fans out terminal valid RMSD representatives,
  fingerprints the RMSD grouping, and omits ΔE_int unless the current generation has exactly
  one completed, same-level/same-geometry, input/output route-and-state-verified complex stage
  plus every fragment stage. The 23-column `interaction_energy.csv` distinguishes parent and
  actual complex-SP stages and records that no separate Boys–Bernardi ghost calculation was
  performed (method-inherent corrections such as r2SCAN-3c gCP remain possible). Strict schemas,
  immutable post-fan-out science settings, role-aware restart, uploaded-artifact denial, and a
  content-digest owner-marker transaction protect provenance and user files. Restart also
  revalidates partition/electron states against the copied durable input. SI publication now
  persists generation/attempt/backoff/blocked state, retries SI-writer failures at most five times,
  blocks deterministic conflicts, and can be re-armed explicitly with `run-dir --force` after the
  cause is fixed. Registry reconciliation also preserves publication/child-sync liveness, prevents
  identity-quarantine hot loops and duplicate workspace rows, and keeps those durable states from
  being cleared as stale. Disabling the feature retires its stages.
- Workflow Supporting Information (`workflow_si.md` and `si_data.csv`) now reports
  Boltzmann populations for a complete, terminal conformer ensemble only. Every
  route-classified minimum must be converged and carry a complete 3N vibrational
  spectrum with `Nimag = 0`, finite electronic/Gibbs energies, and a finite
  positive thermochemistry temperature;
  an unfinished, failed, or unusable member omits the whole population set instead
  of renormalizing a partial ensemble to 100%. Populations use the same E/G
  convention as the relative-energy table; SP/composite refinements require
  complete, uniform exact-provenance coverage. Missing optimization/frequency
  route or ORCA-version evidence omits populations, while incomplete optional SP
  provenance disables that refinement and falls back; parsed charge/multiplicity
  must also match the selected input. They are normalized
  independently within each `formula|charge|multiplicity` group. Each retained
  minimum has unit
  statistical weight; no symmetry/degeneracy correction or connectivity-aware
  grouping is applied. Optional post-DFT dedup checks ensemble completeness first,
  and its duplicate count is not a population weight. The optional
  `boltzmann_temperature_k` pin is validated as finite and strictly positive at
  admission, stored in the durable request, and must agree with every parsed
  thermochemistry temperature within 0.01 K. Markdown reports population as a
  percentage, while the appended `si_data.csv` `boltzmann_population` value is a
  fraction in `[0, 1]`; the other appended columns are `cluster_key`,
  `rel_E_kcalmol`, `rel_G_kcalmol`, and `boltzmann_T_K`. The two relative
  columns use the lowest E and G in that row's population group as their local
  baselines.
- CREST conformational-search knobs can be set under the existing `crest:`
  manifest block: `mdlen`/`len`, `wscal`, `tstep`, `mddump`, `shake`, `norotmd`,
  and `cross`/`nocross` (implemented against CREST 3.0.2 semantics). `mdlen`/`len` and `wscal`
  are finite positive reals, `tstep` is a finite positive real within CREST's
  native-safe range, and `mddump` is a positive native-safe integer; the derived
  aggregate MD step count is bounded to 10,000,000 by default for explicit MD
  lengths; an omitted `mdlen` uses a 14,000,000-step automatic-length budget,
  which admits standard GFN-xTB defaults. Under the standard non-quick trajectory
  multiplicity, GFN-FF/composite automatic length requires an explicit bounded
  length or acknowledged higher step budget. All local work remains at or below
  50,000,000,000 atom-steps. Estimated dump volume
  defaults to 100,000 frames. Larger local budgets require explicit high-cost and
  high-volume acknowledgements, and method-aware time-step limits require a
  separate expert override. The exact `norotmd`, `cross`, and `nocross` keys
  accept only canonical boolean values, and `cross`/`nocross` remain mutually
  exclusive. `cross: true` preserves CREST 3.0.2's GC-crossing default without
  emitting its broken redundant `--cross` flag; `nocross: true` emits
  `--nocross`.
  Malformed or unknown values fail the job closed instead of reaching CREST, and
  `crest_mode` is unchanged. Uploaded Discord workflows cannot override CREST
  length/time-step/dump or budget/acknowledgement keys; xTB ranking cost controls
  are likewise server-owned for remote ingress.
- Discord bot can accept a compressed run-dir (`.zip`/`.tar.gz`) attached to the
  `!run` command and submit it after an explicit confirmation. Disabled by
  default and gated to allowlisted operators, the ingress reserves bounded
  staging before downloading, streams only from Discord's trusted CDN, and
  persists idempotent, actor-bound confirmation state across restarts. Archive
  inspection rejects traversal, links, runtime state, ambiguous entrypoints,
  and compressed or metadata bombs; extraction is privately materialized and
  atomically published under `runs_root`. Server-owned roots and resource caps
  override uploaded data, while uncertain downstream commits preserve the run
  for reconciliation instead of deleting possibly queued work. Remote ORCA
  inputs also reject executable overrides, Compound/external-argument features,
  nested input includes, external path references, multi-job/MD directives, and
  ORCA host-list control files.

### Changed

- Discord and Telegram notification documents now use the same engine-aware
  identity, severity, author line, fields, and footer conventions. The provider
  adapters render the shared document model instead of maintaining divergent
  notification wording.

### Fixed

- Workflow failure reports now retain actionable failure reasons and resolve the
  canonical current workspace and retry report instead of linking stale attempt
  paths. Existing-output queue submissions preserve their original task identity.
- ORCA shutdown cleanup is idempotent under repeated termination signals, so a
  second `SIGTERM` cannot unwind the first cleanup path or release process state
  prematurely.
- The release fake-ORCA smoke follows the normalized
  `job_report.json` `artifacts.last_out_path` into the confined generation
  directory instead of assuming that execution output is copied back to the job
  root.
- Pre-MD workflow hardening now enforces `max_xtb_stages` and `max_orca_stages` as
  total fan-out caps, keeps workflow charge/multiplicity authoritative across
  CREST/xTB/ORCA, rejects non-finite energies and coordinates, and requires a
  successful CREST run to retain at least one strictly valid XYZ frame. Overlapping
  CREST retained files no longer duplicate downstream geometries. Explicit malformed
  scheduler/resource/workflow config fails closed instead of selecting defaults or an
  unintended PATH executable. Queue publication, terminal index persistence, and
  workflow submit/cancel writes now use repair/adoption or exact intent fencing across
  partial failures. xTB/CREST terminal publication now commits canonical state, JSON,
  Markdown, and index records under the exact queue-generation lock; cancellation wins
  completion races, direct pending cancellation is finalized on reconciliation, and
  partial terminal writes are repaired without replaying already-consistent rows.
  Internal-engine queue artifacts now carry an immutable-generation fingerprint;
  deployments upgrading from a pre-fingerprint/pre-snapshot build must first drain
  those rows with the old build, or cancel/clear and resubmit them with the new build.
  Integer priority `0` and negative values retain their ordering.
  Queue claim, orphan recovery, publication replay, and terminal artifact adoption now
  require one exact engine/task/generation identity; malformed publication markers are
  quarantined, cross-engine rows are excluded from activity, cancellation, duplicate,
  and clear operations, and xTB/CREST workers repair their own queued index before dequeue.
  Explicit workflow restart rotates its submission intent, and all valid CREST retained
  files contribute distinct downstream geometries while cross-file overlaps are removed.
  Built-in engine registry, worker, admission, workflow-path, and activity routing now
  share one import-safe catalog, and historical Telegram entrypoints use the actor-bound
  canonical action path.
- Queue execution is now bound at submission. xTB and CREST run content-addressed
  immutable input/manifest snapshots in unique, exclusively reserved submission
  namespaces, while ORCA executes a private per-generation
  tree with supported input references rewritten to confined copies. Inputs are
  limited to 64 MiB per file, xTB/ORCA generations to 256 MiB aggregate, ORCA
  file-reference directives to 128, and downstream XYZ materialization to 512 MiB. Exact executable
  identities and source/executed descriptors are recorded and checked; pre-upgrade
  xTB/CREST/ORCA queue rows without snapshots must be drained or resubmitted.
  A bounded queue-root intent journal is persisted before generation directories
  are created, reconciles abandoned pre-enqueue snapshots conservatively, and is
  finalized before a reserved worker child can start.
  Ambiguous duplicate ORCA resource/checkpoint directives fail closed, while
  resource admission evaluates the largest active request before normalization.
  Unbound external ORCA include/program hooks are rejected instead of remaining
  mutable after enqueue.
- YAML flow/job manifests are limited to 1 MiB, 32 aliases, 10,000 parsed/expanded
  nodes, and 64 nesting levels, with cyclic/recursive graphs rejected. Geometry
  admission caps local work at 10,000 atoms, xTB/ORCA Hessian-producing work at
  1,000, and Discord-uploaded work at 200. Remote CREST ingress injects a
  server-owned 5.0 ps MD length and enforces a 50,000,000 atom-step ceiling.
  Reaction endpoint fan-out accepts at most 32 CREST candidates per side,
  retains only the requested top pairs in memory, caps geometry comparison at
  256 effective atoms, and parses each candidate ensemble once per selection.
- xTB now always emits explicit charge/UHF and `--norestart`, rejects a non-empty
  legacy `namespace`, and caps ranking at 100 evaluations by default (up to 1,000
  with an explicit high-cost acknowledgement). CREST uses the absolute immutable
  input instead of its unsafe legacy `--scratch` copier, binds xTB with `-xnam`,
  emits `--legacy` for `gfn2//gfnff`, and accepts only documented solvent tokens.
  Both engine-job manifests reject unknown fields.
- xTB/CREST retained outputs now carry terminal SHA-256/size identities that are
  verified before downstream parsing. Legacy completed artifacts receive an
  explicitly marked read-time identity backfill, while irreparable same-generation
  terminal state/report loss becomes visible `repair_blocked` activity instead of
  an endless repair loop. xTB/CREST also run with a private clean HOME and captured
  runtime environment; external parameter/shared-library contents and semantic
  engine-version compatibility remain deployment trust requirements rather than
  snapshot-bound guarantees.

## [0.1.0] - 2026-07-09

First tagged release. This entry consolidates the initial public development
series: everything below shipped in the `v0.1.0` tag.

### Fixed

- IRC report parsers now match real ORCA 6 output. The settings parser keys on
  the `Intrinsic Reaction Coordinate Calculation` banner (there is no
  "IRC settings" header in real output) and accepts dotted rows whose labels
  contain periods or whose leader dots butt against the label, so the
  "IRC setup" section and the SI block's trajectory-file lines actually render;
  the iteration parser handles the asterisk-boxed `FORWARD IRC` / `BACKWARD
  IRC` banners and the real five-column table (`Iteration E dE max(|G|)
  RMS(G)` — there is no separate step column), so the "IRC iterations" section
  renders too. `IrcIterationPoint` lost its fictional `step` field and gained
  `delta_e_kcal`. Previously both parsers returned empty on every real output
  while passing tests against invented fixtures.
- NEB settings are no longer truncated at the `Generation of initial path ....
  idpp` row: dotted setting rows are matched before the section terminators, so
  the full table (RMSD handling, convergence tolerances, L-BFGS parameters —
  31 rows instead of 9 on a real output) reaches the "NEB setup" section.
- Job reports no longer let a content-free final attempt (a retry that died
  before the driver started, or a trailing Freq-only run) mask an earlier
  attempt's parsed data: the NEB, IRC, and Opt collectors now prefer the latest
  attempt output that actually contains path points / iterations / cycles,
  falling back to the latest parseable output for formula metadata.
- Workflow-report energy-axis tick labels use two decimals when the tick step
  is below 0.5 kcal/mol, so a 0.25-wide grid no longer labels its ticks
  "0.2"/"0.8".

### Changed

- Report-module cleanup: the status/reason badge pair, the meta line, and the
  dotted-settings table (`ReportSetting` in `orca.report.settings`) are shared
  helpers instead of five near-identical copies; `IrcSetting`/`NebSetting` are
  gone. `final_out_path` moved from `orca.report.si` to `orca.report.attempts`
  (the IRC module had a private reimplementation). The report composer's
  primary facet is a typed `JobReportData` union instead of `object` +
  `getattr` duck-typing.
- Config schema: the single runs root moved to a top-level `runs_root` key.
  `orca.runtime.allowed_root` and the `workflow.root` override are gone — with
  standalone ORCA jobs and workflow workspaces sharing one directory, one key
  defines the root for everything (ORCA execution, workflow workspaces, and the
  default `<runs_root>/.admission` directory). `orca.runtime` now only holds
  `default_max_retries`. Existing configs must rename the key; there is no
  fallback to the old locations.
- Single runs root: workflow workspaces now default to living inside the ORCA
  runs root (`workflow.root` falls back to `orca.runtime.allowed_root`), and
  the shared admission directory defaults to a hidden `<runs root>/.admission`
  instead of an `admission/` directory next to the config file. One disk
  directory now holds standalone ORCA runs, workflow workspaces, and admission
  state; explicit `workflow.root` / `scheduler.admission_root` overrides still
  work. Standalone filesystem scans (job-location reindex, activity run
  snapshots, `scan-notify` discovery) skip workflow workspaces under the runs
  root so workflow-internal ORCA jobs are not double-reported. `orca_auto init`
  now asks for one runs root instead of separate workflow/ORCA roots.
- An exhausted ScanTS retry-recipe chain now reports the actionable reason
  `scants_recipes_exhausted` (previously the generic `rewrite_failed`, which
  read like a rewriter bug). `rewrite_failed` is reserved for genuine rewrite
  crashes.

### Removed

- The remaining `organized_*` read-side plumbing (completes the organize
  removal): the `organized_output_dir` field on `JobLocationRecord` and on the
  ORCA/xTB/CREST artifact contracts, the `organized_ref` / `load_organized_ref`
  DI seam threaded through the generic engine indexing service, adapters, and
  contract loaders, the `organized_dir` / `orca_organized_root` resolution in
  the ORCA contract assembly and job-location runtime context, the unused
  `organized_root` runtime config field, the dead `organize`-summary
  notification helpers, and the hardcoded `"organized_dir": ""` artifact keys.
  All of it was uniformly empty/None (nothing populated it once the organize
  feature was gone) and the workflow stage handoff already uses
  `latest_known_path`; removal is behaviour-preserving.
- The unused `organized_ref.json` write machinery: `write_organized_ref` in
  the ORCA state module, the shared engine-state write method/field, and the
  xtb/crest re-exports. Nothing has written these stubs since the organize
  feature was removed.
- The dead numbered retry-recipe system: `RETRY_RECIPES` and `retry_step_1..4`
  / `set_geom_retry_keys` (`retry_recipes.py`), the `isinstance(step, int)`
  branch and `RetryRecipeName | int` unions in `inp_rewriter`/`resume`, and the
  `retry_recipe_step` helper with its `resume`/`engine` DI seam. Retry policies
  resolved to the two `RetryRecipeName` route no-ops (`scants_retry`,
  `no_route_rewrite`) long ago, so the integer ladder was unreachable in
  production; removing it is behaviour-preserving for real (string) inputs. The
  live ScanTS retry, maxcore clamping, and checkpoint/geometry restart paths are
  unchanged.
- The `orca.runtime.organized_root` config key and the `orca_outputs` sibling
  directory: with organize gone, the organized root always collapses to
  `allowed_root`. A stale `organized_root` key is silently ignored (the loader
  simply never reads it), `orca_auto init` no longer prompts for it, and
  rendered systemd units no longer grant ReadWritePaths to an outputs directory.
- The `organize` feature: the `orca_auto organize orca` CLI command, the
  `result_organizer` module and its JSONL organize index, the
  `behavior.auto_organize_on_terminal` config key, and the worker's
  auto-organize-on-terminal hook. Completed runs now stay where they ran,
  keeping their user-chosen directory names; existing `organized_ref.json`
  stubs and previously organized outputs are still readable by lookups.
  `queue list` remains the way to see completed work in one place.
- ScanTS completion-triggered retry recipes (endpoint-completion scan, reverse
  scan, and the barrierless-profile stop between them): these fired after
  non-failure outcomes such as `ts_not_found`, violating the
  retry-only-on-calculation-failure rule. The strategies live on as proper
  `scan_ts_search` workflow stages (scan extension and reverse rescue). ScanTS
  keeps its failure-triggered recipes only — mid-scan crash continuation and
  the zero-distance OptTS refinement fallback; any failure after a finished
  scan now ends with `scants_recipes_exhausted`. Surface/coordinate parsing,
  prominence analysis, and the ScanTS HTML report are unchanged, so historical
  ScanTS runs stay readable.

### Added

- `scan_ts_search` scan extension and reverse rescue: the recovery strategies
  proven in the ScanTS retry chain, re-homed as proper workflow stages. A
  completed forward scan with no interior barrier gets up to
  `max_scan_extensions` (default 1) extension scan stages continuing past the
  endpoint before `scan_profile_no_barrier` is recorded; when every forward
  OptTS candidate finishes without verifying a TS, a reverse scan stage walks
  the full range back from the forward endpoint (scan hysteresis yields new
  maximum geometries) and its maxima fan out as a second candidate batch,
  failing with `ts_candidates_exhausted` only when those are spent too.
- `scan_ts_search` workflow template (scaffold shortcut `scan_ts`): an ORCA
  relaxed-scan stage (route + `scan_coordinate` from `flow.yaml`) followed by
  automatic fan-out of one OptTS+Freq child job per interior maximum of the
  scan profile with prominence above `barrier_threshold_kcal` (default 0.5),
  each started from that maximum's numbered scan geometry and ranked in the
  workflow report's TS-candidates table. Profile endpoints never become
  candidates; a barrierless profile fails the workflow with
  `scan_profile_no_barrier`. Replaces the reverted job-level relaxed-scan
  auto-chain (#24) with per-candidate queue visibility: individual child jobs
  stay individual.
- JOSS-style open-source operating materials: contribution guidance, issue and
  pull request templates, release checklist, validation documentation, and a
  fake ORCA smoke example.
- Workflow HTML report: every workflow advance rewrites a self-contained
  `workflow_report.html` in the workflow workspace, showing the stage chain
  with statuses, the CREST → (xTB) → ORCA funnel counts, and a ranked ORCA
  results table (relative energies from `.engrad`, imaginary-frequency counts,
  links to per-job `job_report.html`). Covers both `conformer_screening`
  (conformer ranking) and `reaction_ts_search` (TS-candidate ranking).
- ScanTS OptTS fallback: when ORCA's TS-guess refinement aborts with a
  zero-distance geometry after the scan already bracketed a maximum (an ORCA
  6.x bug observed on TS6/TS8), the retry chain now runs one plain OptTS
  attempt directly from the highest surface point, bypassing the refinement.
  If that attempt also fails, the ordinary endpoint-completion/reverse-scan
  chain resumes from the crashed scan's artifacts.
- Relaxed-scan HTML report: plain relaxed scans (`Opt` route with a
  `%geom Scan` block) now get the scan-profile flavor of `job_report.html`
  (energy profile, interior-barrier prominence, scan-coordinate alignment)
  instead of the optimization-convergence flavor.
- HTML job reports: Opt, OptTS, NEB-TS, ScanTS, and IRC jobs now write a
  self-contained `job_report.html` next to `job_report.md` for successful and
  failed runs alike (shared renderer plus composable report components in
  `orca/report/`). ScanTS reports show
  the scan energy profile across all attempts (inline SVG); NEB-TS reports show
  the CI-NEB path profile, CI optimization history, and final TS refinement
  trace; IRC reports show the IRC path profile and endpoint summary, and for
  combined routes such as `OptTS Freq IRC` or `NEB-TS Freq IRC` also compose in
  the relevant TS optimization, NEB, IRC, and frequency sections; Opt/OptTS reports show the optimization
  convergence trace with an imaginary-frequency expectation check (0 for
  minima, 1 for TS). All flavors include the retry-recipe chain and a
  vibrational summary of the final frequency calculation — imaginary modes,
  dominant atom displacements, and (for ScanTS) their alignment with the scanned
  coordinate.
- ScanTS barrierless-profile detection: after the endpoint-completion scan, the
  assembled forward profile is checked for an interior maximum above
  0.5 kcal/mol; without one the run fails immediately with reason
  `scan_profile_no_barrier` instead of spending hours on a reverse scan that
  can only mirror the same monotonic profile.
- Queue-first ORCA runtime with durable queue state, worker execution, retry
  state/report files, and organized output support.
- Internal workflow support for xTB and CREST stages.
- Linux/WSL-first CLI, configuration template, systemd user-service assets, and
  CI coverage for fake-engine integration paths.
