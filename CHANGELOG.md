# Changelog

All notable changes to orca_auto are documented in this file.

This project follows a lightweight [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
style. Version numbers are recorded in `pyproject.toml`; release procedure lives
in [docs/RELEASE.md](docs/RELEASE.md).

## [Unreleased]

### Fixed

- The workflow report takes a non-completed ORCA stage's imaginary-mode count
  from the ORCA output that stage's accepted `machine.json` binds, not from
  the engine's private `job_state.json` markers. Nothing publishes or
  re-verifies those markers, so an edited job state could dictate a published
  `Nimag`. `load_report_json` recomputes the `orca-output` receipt from the
  recorded terminal output and rejects the generation unless it equals the
  stored one, so the count now rests on bytes whose size and SHA-256 were
  re-checked when the generation was accepted. It is counted by the same
  scanner the analyzer uses, only when the reason the observation pins is
  `ts_criteria_met` or `ts_criteria_failed`, and only for a stage whose task
  kind can enter the candidate table.
- A generation whose markers predate `final_frequency_section` publishes its
  imaginary-mode count again instead of nothing. No generation on this machine
  carries that marker: of the 104 ORCA generations holding both a
  `machine.json` and a `job_state.json`, 85 record only
  `imaginary_frequency_count` and 19 record no markers at all, so the released
  code publishes no `Nimag` for any non-completed ORCA stage here.
- The output analyzer sections a small output by the same line rule as a large
  one. `str.splitlines()` also breaks on a form feed, a vertical tab, the
  file, group and record separators, NEL and the Unicode line and paragraph
  separators, so an output carrying one of those inside a frequency section
  could be counted one way when it fitted the 256 KB TS tail window and
  another way when it did not. None of the 759 ORCA outputs on this machine
  contains such a character, so no recorded count changes.

- The cancel-transition drain only locks, reads or writes a workspace that
  resolves under the workflow root; a registry row whose raw workspace string
  resolves elsewhere is left alone.
- `workflow clear` keeps a terminal record whose payload still stores cancel
  transitions the worker has not journaled, as it already keeps records with
  a pending SI publication or child sync.
- The scan endpoint geometry handed to a continuation or reverse scan is the
  `.NNN.xyz` of the last retained surface row's step number; counting retained
  rows picked an earlier step whenever the surface parser refused a row.
- A CREST ensemble file that exists but cannot be handed off (a malformed
  frame, an atom sequence that differs from the input, an identity that
  changed during the read) is recorded on the result and in the detail
  artifact as `rejected_retained_outputs` with a reason, and logged, instead
  of vanishing from `retained_conformer_paths` with nothing recorded.
- CREST ensemble frames carry their comment-line energy: the bare number of
  `crest_conformers.xyz` and `crest_best.xyz`, and the energy of a
  `crest_rotamers.xyz` line (energy, weight, `!`), are read as the frame
  energy, so `source_frame_energy` is recorded for CREST candidates as it
  already was for xTB and ORCA frames. A bare integer is not an energy.
- ORCA's `!!!` error banners are no longer read as the executed route: a route
  line starts with exactly one `!`.
- The ScanTS surface parser ends the actual-energy table at the SCF-energy
  table or the first later section marker (timings, total run time, final
  energy, optimization done, termination) and accepts only rows of the first
  row's width with a finite negative energy, so timing lines are no longer
  read as scan points when the SCF table is absent.
- The output analyzer scans the whole file for the optimization verdict when
  the tail window holds none, so a not-converged marker followed by a long
  normal-modes matrix is `GEOM_NOT_CONVERGED` for the analyzer as it already
  was for the parser.
- The Opt/OptTS report card characterizes from the final output first, as the
  SI block does; a count taken from an earlier attempt's frequency block says
  so on the card.

### Added

- The stationary SI block prints `Last output: <name>` before the
  coordinates, naming the output file its values were read from.

- An enqueue whose queue lock timed out, or that found `queue.json` corrupt,
  is reported as that failure with the submission snapshot compensated. Both
  are raised before anything is committed, so the recovery scan that turned
  them into an outcome-unknown result (and left the snapshot in place) no
  longer runs for them.
- The admission slot file is rewritten only when a dead owner is dropped; a
  worker poll that finds every slot live, or a reservation refused at the
  capacity limit, no longer fsyncs an unchanged file.
- The workflow worker journals cancellation status transitions that a cancel
  command persisted but could not append before it crashed: on the next
  advance of a workflow still cancelling, or on the next cycle for one that
  resolved to `cancelled`, `cancel_failed` or `failed`, each stored transition is
  appended exactly as the cancel command would have appended it (a row the
  command did write dedupes) and removed from the payload under the workspace
  lock.
- Queue-lock contention no longer turns a temporary claim conflict into active
  child cancellation, and queue/activity listing remains projection-only rather
  than mutating durable queue state.
- Crash recovery reuses one strict claim and target generation across replay,
  preserves generation fences, and lets an already committed cancellation
  terminalize before recovery-only metadata validation or replacement work.
- ORCA scratch admission remains conservative: every unresolved, live-owner, or
  stale `attempt-*` workspace is preserved and blocks new admission; only an
  interrupted cleanup tombstone is removed. PR #257 briefly allowed a strictly
  validated dead-owner workspace for another durable generation to remain
  without blocking; PR #258 withdrew that behavior before this changelog entry.
- A crash-recovery rejection (invalid durable claim, exhausted rebind budget,
  diverged submission inputs, or an already existing replacement generation)
  now leaves its reason in the failed queue row's `error` instead of
  `exit_code=1`, and the existing-generation case names the directory to
  inspect. The terminal notification keeps reporting `crashed_recovery` when
  the run state was already repaired before the rejection.
- Parsed thermochemistry (ZPE, H, G, G-E(el), temperature) is bound to the
  output printed after the last final single point energy. Optimizations with
  `Calc_Hess`/`Recalc_Hess` print a thermochemistry block for every Hessian
  they compute, and the parser previously published the first one, so SI
  blocks, SP reports and composite G carried the initial-guess geometry's
  values (15 and 49 kcal/mol off in G on two completed TS outputs). An output
  whose final stage has no thermochemistry block, such as an `OptTS` without
  `Freq`, or whose final energy is missing or unparseable, now publishes no
  thermochemistry instead of an earlier geometry's, as an output whose final
  SCF is annotated as unconverged already did. The parser's imaginary-mode
  fields still use the last frequency block in the file.
- Transition-state verification and reported frequencies are bound to the
  same final stage. A `VIBRATIONAL FREQUENCIES` section followed by another
  final single point energy belongs to an earlier geometry, so an `OptTS`
  with `Calc_Hess` but without `Freq` no longer completes as
  `ts_criteria_met` on its initial Hessian's imaginary mode, and the SI block,
  the SP/Opt/NEB/IRC/ScanTS reports and their `Nimag` no longer show modes
  from a superseded Hessian. When every section was superseded the analyzer
  counts zero imaginary modes and the reports show no frequency analysis; an
  output that never prints a section keeps the legacy whole-file count. The
  analyzer records whether its count is a verdict on the final geometry
  (`final_frequency_section`: counted in a final-stage section and the TS
  criteria were reached), and the workflow report's stage table and
  `machine.json` `orca_results` show a `Nimag` for a stage that did not
  complete only in that case, so a normally terminated candidate rejected for
  0 or 2 imaginary modes keeps the count that explains the rejection while an
  unfinished run, a geometry or SCF failure, or a record written before the
  marker existed shows none.
- Graceful shutdown of the ORCA queue worker no longer requeues a child that
  had reached its own conclusion by the time termination returned. A child
  that had finished ORCA (completed or failed) and was writing its state and
  report exits with a non-negative code; requeueing it re-executed or failed
  force rows and gave plain rows a new run id that unbound their published
  report and repeated the terminal notification. Such a child, and one that
  stopped mid-run and requeued itself (which also exits 0), now takes the
  normal completion path, which marks the row only while it is still running
  and leaves a self-requeued pending row untouched. A child killed by a signal
  exits negative, and a child that exited non-negatively with its row still
  running and a non-terminal run state (it died while handling the stop) is
  an interrupted calculation; both keep the resume path. If finalizing during
  shutdown fails, the row and its durable replay marker are left for the next
  worker start and the remaining jobs are still shut down. A failed read of
  the cancel flag no longer aborts a job's shutdown, and if a job's shutdown
  still fails before its child was asked to stop, the worker attempts to stop
  that child's process group as a last resort and logs when it could not
  confirm the stop.
- A failed terminal notification no longer stalls the ORCA queue. The replay
  of a finished job's side effects retained itself until the messenger had
  durably recorded the finished notification, which pinned that job's
  admission slot, blocked every new ORCA admission in every root until the
  messenger recovered, and made the row impossible to clear. The notification
  is now advisory at terminal replay as it already was at submission: a
  delivery failure is logged with the redacted reason, the replay completes
  and releases the slot, and the failed message is not retried later. A
  notifier that raises instead of reporting a failed send is treated the same
  way, and the Discord adapter now reports a payload it cannot serialize (for
  example a lone surrogate from a non-UTF-8 path) as a redacted request error
  instead of raising. State and report replay and the once-only replay marker
  are unchanged.
- A child that dies after fencing its ORCA launch but before publishing the
  engine record no longer wedges the parent worker. The finalizer used to raise
  on the pending record forever, and while it retried, the periodic orphan sweep
  that could have cleared the record was paused, so the slot stayed reserved
  until a restart. Slot recovery now clears a launch-gated (or cross-boot)
  pending record whose owner is dead, and the finalizer marks the row and
  releases the slot; a pending record under a live owner or without a launch
  gate is still retained.
- The xTB worker no longer marks a row completed when its child requeued the
  row for recovery and exited 0 (a shutdown request during the run). The child
  leaves the row `pending` with the run state recovery-pending; the parent used
  to overwrite that with `completed` and `candidate_count: 0`, closing a
  generation that had not run. The parent now marks only a row that is still
  running, as the CREST worker already did.
- A workflow no longer gets stuck at `cancel_requested` after a pending
  engine row was cancelled without a terminal job state: rows cancelled before
  the pending-cancel publication of 3.0.0 existed (the live workflow from
  2026-08-11), or an engine whose pending cancel leaves no terminal state. The
  cancelled row is cleared later, but the job's `job_state.json` still says
  `queued`; the next stage sync loaded that stale contract and moved the
  already cancelled stage back to `queued`, so the workflow saw an active
  child forever and a repeated cancel found no row to cancel. An engine
  artifact contract now never moves a completed, failed or cancelled stage
  back to an active status, and the cancel pass re-applies a cancellation the
  engine already acknowledged for that same row instead of asking the engine
  for a row it no longer has.
- A workflow restart can no longer change the charge or multiplicity while a
  completed CREST or xTB stage retains its conformers. Those conformers were
  screened on the original electronic state, and the restart rewrote only the
  ORCA stage input, so ORCA would have run on another surface with nothing
  recording the mismatch. The restart now refuses such a change, as it already
  did for completed primary ORCA stages. The comparison uses the state each
  completed stage's job manifest carried, so restating the state of an older
  workflow that never recorded charge/multiplicity is accepted. An accepted
  electronic-state change is recorded in the workflow's restart summary, the
  restart journal and the command response (`previous` is null when the
  workflow never recorded it).
- `scripts/check.sh` no longer removes an arbitrary `ORCA_AUTO_VENV` target
  when it is not a usable virtual environment. Automatic repair is limited to
  the owned, non-symlinked repository `.venv` that carries an owned
  `pyvenv.cfg` marker; any other unusable target (an external directory, a
  symlinked repository venv, a path that normalizes onto the repository, a
  venv bound to the base interpreter) is refused with an explanation instead
  of being deleted, and the script tests pin each case.
- `queue list --limit` accepts only non-negative integers, and
  `queue list clear` rejects a negative `--limit` like any other listing
  filter instead of treating it as "no filter" and clearing everything.
- `service install` loads its unit templates from the required `--repo`
  checkout's `systemd` directory even when the installing `orca_auto` command
  is wheel-installed, instead of the installed package's location.
- `service install` no longer names the not-yet-created default
  `<runs_root>/.admission` directory as a mandatory `ReadWritePaths` entry,
  which made the worker unit fail to start on a fresh install; the writable
  `runs_root` parent lets the worker create it. An explicitly configured
  `scheduler.admission_root` must exist as a directory before installation;
  a directory the installing account cannot traverse is not treated as
  missing.

- xTB `ranking` jobs can be submitted again. Submission validated the job by
  building a single xTB command for job type `ranking`, which the runner's
  option builder does not know (a ranking job runs one single point per
  candidate), so every ranking submission failed with "Unsupported xtb
  job_type: ranking". The command is now validated as the candidate single
  point the worker actually runs.
- A workflow whose terminal `machine.json` is already published no longer
  loops forever after its SI publication is re-armed. The observation pins
  `workflow_si.md`, so the re-armed publication could never run, yet the
  pending flag kept every worker cycle re-advancing the workflow. A restart
  now leaves such a publication blocked and records the reason
  (`si_publication_pinned_by_terminal_observation`) instead of re-arming it;
  a forced restart with nothing else to restart is refused with that reason
  and leaves the workflow untouched. A re-advance retires a pending
  publication it finds under a published observation the same way. An
  unreadable or unsafe `machine.json` now refuses such a restart, as it
  already refused every advance.
- `service status` dates a checkout from its newest HEAD reflog entry that
  names the current commit, including one that re-selected the commit
  already checked out. A forced checkout (`git checkout -f main` while on
  main) and `git reset --hard HEAD` write the same reflog subjects as their
  no-op forms but restore the working tree, and the reflog cannot tell them
  apart, so the verdict errs toward stale: a plain `git checkout main` while
  on main also asks for a restart. (The fold of same-commit `checkout:`
  entries introduced earlier in this release is withdrawn.)
- `queue list` reports a configured `runs_root` that does not exist as an
  error instead of listing an empty queue with exit code 0.
- Crash recovery and retry/resume inputs no longer seed `MORead` from a torn
  `.gbw` checkpoint. A crash while ORCA writes its checkpoint can leave a file
  of the right size whose unflushed blocks read back as zeros; it used to be
  copied as the orbital guess, so the restarted run failed on a corrupt guess
  instead of restarting from geometry. A checkpoint whose leading bytes are all
  zero is now skipped (recovery falls back to an older intact attempt or to a
  geometry-only restart).

### Documentation

- The reference says that the `queue list` text view shows a listed child job
  beneath its parent workflow row as context and that the context row does
  not count toward `--limit N` (`--json` returns exactly `N`).
- The reference documents that a worker stop or restart of a running ORCA job
  resumes it through crash recovery: it consumes one of the three recovery
  rebinds and re-validates the submitted input and resource request, so
  workers should be restarted only in an idle window. The public contract
  states the systemd 247 floor that `service status` relies on, and says how a
  non-executable queue row is actually removed (a pending row is cancelled, a
  terminal row cleared, then resubmitted). The systemd README asks for a
  private `config` directory (`chmod 700`) beside the 0600 file and says that
  the installer's target restart does not restart running workers. A stale
  in-tree comment that claimed the opposite is corrected.
- The reference no longer claims that `run-dir` detects an already completed
  output and returns completion without relaunching ORCA. No such check
  exists: an active queue row is a submission conflict, a terminal row is
  enqueued again as a new generation, and only a row that still owns a pending
  terminal replay or a terminal fence marker is refused.

### Validation limitation

- No real-ORCA or ORCA/OpenMPI compatibility acceptance is claimed for these
  queue, recovery, or scratch changes. The merged PR records identify the
  verification that ran and state that real-engine re-validation was not run.

### Changed

- A non-completed ORCA stage publishes an imaginary-mode count only when the
  reason its machine observation pins is the analyzer's own TS verdict. A
  stage whose analyzer reached that verdict but whose record the engine then
  closed under a terminal reason of its own — the retry paths write
  `retry_limit_reached`, `scants_recipes_exhausted` or `rewrite_failed` over
  it and leave the last attempt's markers intact — now publishes no count,
  because none of those reasons says which verdict produced the last output.
  No live generation on this machine records one of them: the only job states
  carrying `retry_limit_reached` are fake-ORCA smoke fixtures that have no
  machine observation and so cannot reach the workflow report.

## [3.0.3] - 2026-08-26

Mostly internal consolidation, with one crash fix in CREST terminal repair.
No text in [docs/PUBLIC_CONTRACTS.md](docs/PUBLIC_CONTRACTS.md) changes and no
documented promise is broken, which is what makes this a patch: the CLI,
config, durable state and published reports behave as they did in 3.0.2. The
one documented behavior this release touches is terminal repair, which it makes
reliable rather than different.

### Fixed

- CREST terminal repair no longer crashes the worker on a malformed durable
  `job_dir`. It resolved the path by hand and caught only `OSError` and
  `RuntimeError`, so a queue row whose `job_dir` held an embedded NUL byte
  raised `ValueError`, and a row whose `metadata` was not a mapping raised
  `AttributeError` before the guard was even entered. The terminal-repair sweep
  has no per-row guard — unlike the publication repair sweep, which logs and
  continues — and the worker loop catches only `KeyboardInterrupt`, so either
  exception ended the CREST worker and stalled every CREST job behind it. Both
  engines now resolve the path through the same canonical resolver, which
  declines repair for such a row. A relative `job_dir`, previously resolved
  against the process working directory and then read from the wrong place,
  also declines now.

### Changed

- Internals that had grown a second copy now have one owner: the CREST worker
  execution context, executable content identity, the admission-slot ownership
  invariant, the CREST and xTB engine-launch blocks, the ORCA terminal
  run-state recorders, the queue input-snapshot directory removal, the
  xTB/CREST submission snapshot transaction, the repair-pending enqueue
  compare-and-set, the parent queue-worker argument parser, and config, state,
  manifest, workflow-metadata, coercion, path and maxcore ownership. The two
  CREST execution-context copies had already drifted: only the reachable one
  had its incomplete-snapshot check tightened, and that is the one that
  survives.
- Executable content identity has a single implementation shared by the
  identity helper and the ORCA pinned launch. The queue snapshot writer and the
  launch-time verifier previously derived the same dictionary independently,
  where any divergence between them would have failed every ORCA launch closed
  at pin time.
- Collecting ORCA run snapshots no longer globs and stats `*.out` files. That
  work existed only to fill snapshot fields nothing read, so activity listings
  and run cleanup now do correspondingly less filesystem work.
- A blank workflow ORCA route line fails closed where the input is rendered,
  not only where it is validated upstream. This is defense in depth rather than
  a fix: no reachable path was found that renders one, because every
  orchestration materializer already rejects an empty route earlier and the
  interaction-energy branch normalizes its route through the manifest schema.
- `MACHINE_OBSERVATION_FILE` is spelled out again instead of aliasing
  `RUN_REPORT_JSON_FILE`. Both are `machine.json`, but they answer to different
  contracts — one external, one the internal per-run report — and aliasing them
  meant renaming the run report would silently rename the file the external
  contract names.
- `scripts/clean_artifacts.sh` prunes empty directories under `src` and
  `tests`. Git cannot track an empty directory, so a package deleted in a
  commit survives in existing checkouts and Python imports it as a namespace
  package, which leaves a removed module looking importable.
- `docs/ARCHITECTURE.md` and its Korean translation describe the reduced engine
  definition and the direct notification wiring.

### Removed

- `EngineDefinition.queue_worker_module`, along with the dead engine artifact,
  context and notification metadata beside it. The directly bound
  `queue_worker_runner`, which every engine already supplied, is now the only
  way a definition names its parent worker; the removed fallback was
  unreachable and would have exited 2 had it run.
- The `finalize_intent` enqueue hook, whose only assigned implementation went
  with the standalone xTB-MD engine in 0.3.0. Snapshot-intent finalization
  itself is unaffected and still runs from the worker.
- The per-engine worker-child argument parsers and `main` functions, which
  stopped spawning anything once children moved to the shared worker-child
  module.
- Internal routines with no callers: the queue cancellation-poll loop and
  cancellation-signal path in `core/queue/lifecycle` and `core/queue/child`
  (cancellation itself is unaffected and still terminates the process group),
  `WORKFLOW_STATUS_ORDER` and `is_terminal_status`, `safe_is_subpath`, two ORCA
  termination patterns, and `flow/runtime/_common.py`.
- Two messenger config loaders exported from `orca_auto.core.messaging`, and
  the contract re-exports and `__version__` from `orca_auto.flow`. Import the
  workflow contracts from `orca_auto.flow.contracts` and the version from
  `orca_auto`.
- Fields that were written and never read: four on the ORCA run snapshot, the
  NEB optimizer and path length, and two on the workflow SI data. The relative
  electronic energies behind one of them are still computed, because they feed
  the check that suppresses populations rather than publishing an unreliable
  table.
- Parameters whose default was the only value any caller passed, including
  eight that turned type-checked attribute access into string lookups, plus a
  few that were never read at all. Values that were smuggled in as defaults are
  now named module constants.
- Exception-tuple members that another class in the same tuple already catches,
  across 22 files. Ruff's `B014` recognizes only exact duplicates and `UP024`
  the `OSError`/`IOError` alias, so subclass shadowing passed the gate
  unnoticed.

## [3.0.2] - 2026-08-25

### Fixed

- Workflow ORCA stage materialization now requires the route line recorded in
  the durable request parameters and fails closed when it is missing or empty,
  instead of silently building the stage at a hard-coded default level of
  theory. The per-template creation defaults are single-sourced in
  `flow/templates.py`; workflows created by any released version always record
  the parameter, so valid payloads are unaffected.
- Worker PID liveness now uses one fail-closed predicate everywhere: a PID
  probe that fails with a permission or unknown OS error is treated as alive,
  so orphan reconciliation and worker-status checks no longer disagree about
  the same PID. Previously the queue-side check treated any `OSError` as dead,
  which could requeue a job whose worker was still running.
- ORCA crash recovery no longer substitutes edited job-root dependencies for
  the bytes captured by the crashed submission; verified runtime geometry is
  reused even when its original job-root source is gone, while changed or
  unverifiable fallback inputs fail closed. Runtime XYZ seeds must preserve the
  submitted atom labels and order and contain exactly three finite coordinates
  per atom. Recovery also remains pinned to the originally snapshotted ORCA
  executable identity.
- ORCA input binding treats top-level `%moinp` and `%scf` `MOInp` declarations
  as one semantic namespace, rejects duplicates, and requires an explicit
  snapshot-bound orbital file whenever `MORead` is requested. Checkpoint
  rewrites update the sole semantic declaration in place instead of creating a
  second reference.
- Workflow ORCA stages now bind Opt, relaxed-scan, and exact OptTS+Freq routes to
  their durable task roles at creation, materialization, restart, and completed
  result acceptance. Routes are strings of route lines only; active injected
  input blocks, quoted route tokens, marker-prefixed payload tokens, and
  non-string values fail closed instead of being rendered. The direct submitter
  requires its selected path to match both durable copies, then validates the
  final rewritten bytes at the execution-snapshot boundary before those same
  bytes are written and identity-bound.
- Relaxed-scan coordinates now require one complete `B`/`A`/`D` coordinate,
  valid arity, finite non-equal endpoints, at least two points, distinct
  zero-based atoms, and indices within the submitted geometry. The same strict
  contract covers creation, dynamic extension, submission, and completed-result
  acceptance, rejects unclosed `%geom`/`Scan` nesting, and preserves endpoint
  values with shortest round-trip float formatting instead of eight-decimal
  rounding.
- Scan workflow restart keeps the relaxed-scan and OptTS route settings
  separate and selects the replacement route from each durable task kind.
  Once a primary ORCA stage completes, restart cannot change its route, charge,
  or multiplicity. Result sets whose members disagree on route, non-resource
  active input directives, ordered atom-label sequence, identity-bound
  non-geometry dependency content, electronic state, or ORCA-version
  provenance cannot publish relative energies or numeric candidate rankings.
  HTML, SI,
  and interaction representative selection share this scientific identity;
  `%pal`, `%maxcore`, and route `PALn` remain resource-only. Spoofed
  interaction-role metadata cannot hide a primary stage from these checks:
  a generated child must carry a canonical SHA-256 interaction fingerprint
  matching the workflow's durable current configuration before SI, fan-out,
  or restart treats it as interaction-owned.
- Discord bot tokens reject embedded whitespace, control characters, and
  non-ASCII values after documented surrounding-whitespace normalization,
  without echoing credentials, and request-construction failures remain
  redacted and advisory to an otherwise durable queue submission. Response-body
  read failures, including rate-limit error bodies, are advisory too.
- Same-boot dead-owner ORCA admission records can reclaim pending engine-launch
  slots when the durable launch-gate policy proves that no engine escaped;
  legacy and direct-launch records retain the conservative behavior.
- Expected queue list, clear, and cancel configuration or state failures now
  produce concise stderr diagnostics instead of Python tracebacks or partial
  stdout payloads. A downstream pipe closing during list, clear, or cancel
  output is handled separately and is never misreported as state corruption.
- Systemd installation escapes literal percent characters in rendered data
  paths without disabling template-owned instance specifiers and rejects path
  characters whose quoting or expansion would change unit syntax.
- Worker freshness now uses import provenance captured by each active process,
  not its working directory, and compares that actual checkout's matching
  HEAD-reflog update time instead of commit time or the status command's own
  checkout. HEAD evidence is refreshed per worker, and an imported package
  tree with uncommitted changes is reported as undetermined rather than fresh.
- Existing-worker conflicts now follow the CLI error contract by writing the
  error, recorded command, and recovery hint to stderr only.
- Concrete mypy override entries now match the current notification module
  inventory, with a regression test that rejects future stale module entries.
- Unused existing-worker exception state, queue command-name plumbing, and a
  redundant systemd config-path local have been removed.

### Removed

- The dead `--admission-root` child-command plumbing: no released worker-child
  parser ever accepted the flag, its only emitters were reachable solely from tests,
  and its default configuration built a command argparse would reject. The
  `admission_root`/`include_admission_root` parameters are gone from the whole
  worker-start callback chain.
- The always-empty `stage_root_name` parameter chain in ORCA stage
  materialization; every caller passed the empty string, so stage directories
  resolve exactly as before.
- A sweep of runtime-unreached internal surfaces confirmed dead by unfiltered
  search: the `EngineWorkerChild` facade class, the `run_engine_worker_entry`
  adapter, never-produced status constants (`partially_submitted`, `deferred`,
  `admission_blocked` and the two unused submitted-status sets — durable state
  on the operating host was scanned to confirm no row carries them), three
  unused artifact-name constants, duplicate `queue_entry_by_id` /
  `build_queue_entry_lookup` / `coerce_dict` definitions, five constructor
  forwarders and rename-only aliases, the `worker_builder` injection seam
  nothing injected into, the static callback layer
  `flow/engines/xtb/queue_runtime_terminal.py`, the `orca/runtime` one-module
  package (flattened to `orca/run_lock.py`), `orca/commands/_helpers.py` (a
  byte-duplicate of the shared config-path helper), dead workflow-worker
  `getattr` plumbing for CLI flags no parser defines (durable payload shape
  unchanged), the always-`None` normalizer-injection seams whose only
  production binding was the canonical `normalize_text`, and re-export facades
  with zero package-level consumers (`core/__init__`, `core/state/__init__`,
  `stage_runtime/__init__`, engine `__init__` version re-exports, and trimmed
  `orca/report`, `orca/job_locations`, `core/indexing/engines` barrels).
- Duplicated logic collapsed onto single sources: `"queue.json"` now comes
  from `core/artifacts.py` everywhere, and the engine job-manifest filename
  constants in the engine artifact/state modules do too;
  the restart module reuses the canonical `workflow_request_parameters` and
  `workflow_stage_dicts` accessors; the per-report relative-energy chart
  builders share one `relative_energy_cycle_chart_svg` (the NEB TS chart
  y-label unifies to the `ΔE / kcal mol⁻¹` form the other reports already
  use); the stage-kind test is a shared `is_orca_stage_kind` predicate; and
  the two single-importer `stage_runtime` xtb modules were folded into their
  consumers.

## [3.0.1] - 2026-08-20

### Fixed

- The workflow energy chain treats a recorded final output as authoritative,
  matching the per-job rule: an earlier attempt's clean value no longer
  stands in for the final geometry when the recorded final is unreadable or
  prints no final energy line. For observations that recorded the final
  output as available, the verified resolution already rejects the report
  once that file is missing from disk; this closes the remaining shapes —
  the window between that verification and the energy scan, a readable
  final with no energy line, and a receipt that never recorded the final as
  available. Earlier attempts stay consulted as annotation evidence, read
  newest-first up to the nearest readable value, so an annotated
  newest-readable attempt still refuses the retained `.engrad`; records
  that never captured a final output path keep the attempt scan.
- `queue cancel` on a workflow now refuses a hardlinked or non-regular
  `workflow_registry.journal.jsonl` before any durable write, alongside its
  existing oversized and symlink refusals, and the workflow restart path
  (`run-dir` on an existing workspace) gains the same up-front symlink and
  single-link-regular refusal (the restart append never reads the journal
  back, so size is not checked there). Restart previously had no journal
  guard at all: its append runs after the mutation is committed, so a
  corrupt journal made the command report failure for a restart that had
  taken effect and poisoned the retry into the no-restartable-stages
  refusal.
- The workflow report's annotated-energy refusal now locates the true final
  `FINAL SINGLE POINT ENERGY` line by scanning backwards from EOF instead of
  reading one 256 KiB tail, which a Freq block's normal-modes printout
  routinely pushed that line beyond. An output whose final energy line is
  annotated as not fully converged therefore refuses its retained `.engrad`
  energy in `workflow_report.html`, the Best-energy card, and terminal
  `machine.json` `results.orca_results` regardless of output size —
  previously such a stage published the unconverged value while its
  `si_block.md` refused the same output. A clean final line beyond the old
  tail now also publishes its energy for stages without an `.engrad`, where
  the value was silently absent.

## [3.0.0] - 2026-08-13

This release refuses to publish numbers it cannot vouch for, so four
documented surfaces change shape.

**Manifests**: `xtb.ts_guess_validation` no longer accepts `enabled`. Drop the
key before submitting — a workflow carrying it is rejected when its first xTB
stage is submitted, which is where the `xtb` mapping's own field schema has
always been checked.

**Reaction workflows**: the xTB→ORCA handoff now requires an explicit
successful geometry verdict and refuses with the new reason
`xtb_ts_guess_geometry_unvalidated` when none exists.

**Published reports and `machine.json`**: a stage energy, its thermochemistry,
or a whole ranked row can now be absent where a value used to appear. An
output whose final energy line is annotated as not fully converged publishes
no energy or thermochemistry — and no `si_block.md` at all, since a partial SI
block is worse than none — while interaction-energy fan-out stages stop
feeding the ranked table and `results.orca_results`. `lineage.upstream` gains
the stages that publish no HTML, and a terminal observation published while SI
regeneration is blocked now carries the delivery code
`orca_auto/si_publication_blocked` beside the pinned SI — normally on a
delivery that is otherwise complete, so a consumer that treats any code as
failure needs updating. Already published
terminal observations are immutable and unchanged; every difference applies to
future publications.

**Systemd**: `service restart` now restarts the worker services themselves, so
it ends in-flight ORCA work — run it in an idle window. A worker whose restart
fails is left stopped instead of running stale code, an unreadable
workflow-worker state changes nothing and exits non-zero, and both service
commands now address the units of the account behind `sudo` rather than root.

One more operational note: an oversized or symlinked
`workflow_registry.journal.jsonl` refuses `queue cancel` for that workflow up
front, with a message naming the file — and, for the size limit, the
remediation.

### Changed

- The reaction xTB→ORCA handoff now requires an explicit successful geometry
  validation verdict on the TS guess: a candidate without one refuses the
  handoff with the new reason `xtb_ts_guess_geometry_unvalidated`, while an
  explicitly failed validation keeps refusing with
  `xtb_ts_guess_geometry_invalid`.
- Queue and workflow activity timestamps parse through the shared strict ISO
  parser; a non-string value is treated as absent instead of being coerced to
  text first. No JSON-loaded state can carry such a value today.
- `AGENTS.md` documents the repository validation entrypoint and the states
  `make check` cannot certify: deployment, live-runtime safety, and
  real-engine acceptance.
- Removed dead internal surfaces and collapsed pass-through wiring across
  queue rendering, transition events, flow imports, ISO parsing, xTB ranking
  re-exports, systemd unit names, and the admission-store test double; queue
  text output and workflow journal event order were verified unchanged.

### Removed

- The unused `manifest_scalar_text` helper and the lenient `as_bool`/`as_float`
  config coercers. None had a production consumer; they were kept alive only by
  re-exports and their own tests, and `manifest_scalar_text` silently treated
  an explicit `false` as unset.
- The `xtb.ts_guess_validation.enabled` manifest key. After the validated
  geometry handoff landed, `true` matched the default and `false` made every
  reaction workflow refuse the ORCA handoff with a misleading
  `xtb_ts_guess_geometry_unvalidated` reason, so the knob had no supported
  use. Geometry validation always runs; the threshold keys `bond_scale`,
  `max_spurious_bond_changes`, and `reacting_bond_stretch_scale` remain
  tunable. A direct xTB job submission carrying `enabled` is rejected at
  admission as an unknown field; a workflow manifest carrying it is rejected
  when its first xTB stage is submitted, the same deferred point where any
  unknown key in this block has always surfaced. A stale `enabled` value
  inside an already-submitted per-job manifest is ignored by the runner on
  restart.

### Fixed

- A terminal workflow observation published while SI regeneration is blocked
  now carries the delivery code `orca_auto/si_publication_blocked` whenever a
  last known-good `workflow_si.md` is pinned: consumers can see the pinned SI
  may predate the final payload instead of reading an unqualified
  `delivery: complete`. A blocked workflow that never published an SI keeps
  reporting `incomplete` with `required_artifact_unavailable`.
- The per-job SI block and HTML report never take numbers from a wrong
  attempt: a recorded final `last_out_path` that is absent on disk now reads
  as no output instead of silently substituting an earlier attempt's file.
  Records that never captured a final result path keep the attempt scan.
- An output whose final energy line is annotated `(SCF not fully
  converged!)` publishes no energy or thermochemistry derived from that SCF:
  `si_block.md` refuses the whole block, `job_report.html` prints neither an
  unconverged E(el) nor thermochemistry parsed from the same output, and the
  workflow ΔE entry reports no energy — a retained `.engrad` carries the
  same unconverged SCF's value and is refused too, and no fallback to an
  earlier clean line that belongs to a different geometry occurs.
  Imaginary-frequency counts and per-cycle traces (opt progress, NEB) keep
  rendering: a trace value is the energy of its own cycle.
- An interrupted scratch cleanup no longer pins tmpfs RAM invisibly: the next
  scratch run completes the removal of `.orca_auto_cleanup.*` tombstones
  under the scratch-root lock.
- A corrupt `.engrad` spelling `nan` or `inf` now reads as unavailable instead
  of rendering NaN in `workflow_report.html` and crashing the terminal
  machine-observation writer on every later advance of that workspace.
- The `--admission-token` flag on all four worker-child entrypoints defaults
  to an empty string, so a token-less diagnostic invocation runs unmanaged as
  the signatures advertise instead of silently exiting with the literal token
  `"None"`. The supervised systemd path always passes a real token and is
  unaffected.
- A configured messenger config path that does not exist now logs a warning
  before notifications are disabled, matching the existing parse-failure
  warning.
- `scratch_provenance.omitted_transient_bytes` counts only regular-file bytes;
  an omitted scratch directory no longer contributes its dirent size.
- `queue cancel` on a workflow whose `workflow_registry.journal.jsonl` exceeds
  the 8 MiB caller-event read limit, or is a symlink, now refuses up front,
  before any stage cancel or durable write, with a message naming the journal
  and — for the size limit — the remediation. The oversized journal previously surfaced only after the
  cancelled status was already persisted, so the command reported failure
  for a cancel that had taken effect and every later cancel in that root
  failed the same way.
- The workflow candidate ranking no longer includes interaction-energy
  fan-out single points. Fragment and complex SP stages (metadata role
  `interaction_*`) carry a different stoichiometry or level of theory, so
  ranking them listed fragment species as conformer candidates and let a
  cross-level complex energy set the ΔE baseline in `workflow_report.html`,
  the Best-energy card, and the terminal workflow `machine.json`
  `results.orca_results`. The stages remain in the stage chain, in workflow
  lineage, and in the SI ΔE_int table; like `relaxed_scan` stages, their job
  reports are no longer linked from the candidate table, and the ORCA jobs
  metric card now counts candidates only. Already-published terminal
  observations are immutable and unaffected.
- A workflow observation's `lineage.upstream` now records every ORCA
  `machine.json` the workflow actually consumed, including stages that publish
  no HTML report and prerequisite `relaxed_scan` stages; the co-located HTML
  report is no longer treated as lineage authority.
- Terminal `machine.json` probing fails closed before the linked artifact
  writers: an existing observation that is a symlink, hard link, or unreadable
  now aborts report finalization before the HTML and SI writers run, so a
  no-HTML retry can no longer delete a published `job_report.html` before the
  terminal write refuses.
- `service restart` now restarts the worker services themselves after the boot
  target. Restarting the target left both workers' start timestamps untouched
  on the deploy host, so a worker kept serving pre-deploy code while the
  command reported success — and the opt-in workflow worker belongs to no
  target at all, which made it unreachable through the very command
  `service status` names when it reports that worker as stale. The workflow
  worker is included once it is running, starting up, or failed — a crash loop
  is where a bad deploy leaves it, and clearing its start limit is what the
  command is for. One that is stopped or stopping stays that way, and an
  unreadable state now changes nothing and exits non-zero rather than
  reporting a restart that did not happen. Two consequences worth planning
  for: the command now ends in-flight ORCA work, so run it in an idle window,
  and a worker whose restart fails is left stopped instead of running stale
  code.
- `service status` and `service restart` now resolve the account behind `sudo`
  instead of reporting root. Both commands act on system units, so operators
  run them through `sudo`, where `getpass.getuser()` returned root and every
  unit name resolved to an `@root` instance nobody installed. systemd calls
  those a success — `reset-failed` exits 0 on a unit it reports as not
  loaded — so the restart silently skipped the real workers. Template units
  cannot catch this either: they load for any instance name.
- Pending xTB/CREST workflow-child cancellation now publishes same-generation
  terminal state before the queue row becomes terminal. Workflow cancellation
  persists one stable journal transition before best-effort notification and
  recovers interrupted payload/registry or uncertain journal writes without
  adopting a successor generation; replay of an existing event does not resend
  the notification. Standalone engine cancellation also adopts a concurrent
  same-generation `cancelled` row while rejecting a successor generation.
- Standalone `run-dir --json` now writes exactly one JSON document to stdout,
  using the same submission result as the human renderer instead of printing
  `key: value` lines.
- Queued standalone ORCA root and generation `job_state.json` now preserve the
  queue id and immutable generation identity from the claimed row. Direct
  unqueued runs keep those fields empty, and mutable run/replay metadata no
  longer changes the generation token.

## [2.0.0] - 2026-08-10

### Changed

- ORCA generations and terminal workflow roots now expose exactly one public
  machine metadata file, `machine.json`, using
  `factory/machine-observation` v1 with a `chemistry/results-bundle` v1
  payload. Artifact receipts bind package-relative paths to exact bytes and
  SHA-256, terminal observations publish last, and the former public
  `job_report.json` is removed. `job_state.json` and `workflow.json` remain
  private durable recovery state; human HTML and SI files remain artifacts.
- `scan_ts_search` generations now materialize their ORCA stages directly
  under the generation workspace as workflow-ordered directories (`01_scan`,
  then `02_scan_maximum`/`02_scan_extension`, ... in creation order) instead
  of nesting them under a `03_orca` engine root, and no longer keep an
  `inputs/` copy of the source geometry — the scan stage materializes it
  straight from the scaffold source. The engine-numbered root encodes the
  crest → xtb → orca pipeline order, which carries no information in an
  ORCA-only template, and the `inputs/` copy was a leftover of the conformer
  builder this template reused: the geometry existed three times per
  generation. Existing generations keep their old layout and remain valid;
  ORCA queue entries live in the shared runs-root queue either way.

### Added

- `service status` compares each active worker process against the checkout's
  HEAD commit and gates on workers older than the last deploy. The units import
  the checkout live (editable install) but never reload, so a deploy that adds
  a symbol to an already-imported module leaves a long-running worker on a torn
  module graph; one such worker rejected every reaction TS candidate submission
  with an `ImportError` and failed the workflow as candidate exhaustion. The
  command reports `worker_staleness` in its JSON payload (stale and
  undetermined workers, with unit, PID, and start time), writes a
  `service restart` hint to stderr, and exits non-zero. A source tree that is
  not a git checkout reports `worker_staleness: null` rather than a verdict.
  The worker start time is systemd's `ExecMainStartTimestamp`, which snapshots
  the real-time clock when the process forks; deriving it from
  `/proc/<pid>/stat` ticks plus `btime` was tried first and gave a false fresh
  verdict in the very deploy that shipped the gate — on WSL2 the wall clock is
  stepped forward after host sleeps while the monotonic clock stands still, so
  every `btime`-derived start time drifts forward past a HEAD it actually
  predates.
- `service status` compares the installed distribution metadata against the
  source tree the process imports and gates on a mismatch. An editable install
  freezes its metadata at install time, so a checkout that fast-forwards without
  rerunning the install runs one version's code while every version report names
  another; that state went unnoticed for two months. The command reports
  `version_drift` in its JSON payload with the interpreter it inspected, writes
  the mismatch and a `pip install -e .` hint to stderr, and exits non-zero. The
  verdict covers only the interpreter that ran the command, since a host may
  hold several editable installs of one checkout. An install with no source
  `pyproject.toml` reports `version_drift: null` rather than a verdict.

### Changed

- `service status` can now exit non-zero while reporting `ok: true`: `ok` still
  means every required unit is active, and the new non-zero cases are a stale
  installed version and a stale (or uninspectable) worker process. Callers that
  treat any non-zero exit as a dead worker should read `version_drift` and
  `worker_staleness` to tell the cases apart.
- The `reaction_ts_search` scaffold manifest now writes `max_orca_stages: 3`
  explicitly, with a comment stating that it is a total attempt budget consumed
  in xTB stage order. The default and the semantics are unchanged, but the knob
  was previously invisible: a run that handed off five TS candidates silently
  dropped the last two at the default limit, and nothing in the scaffolded
  `flow.yaml` said such a limit existed.

### Fixed

- Submitting to a queue whose worker is already running no longer logs a
  warning with a full traceback when the worker retires the snapshot intent
  marker first. Once the durable queue row is committed, the worker's
  reconciliation pass may unlink the intent before the submitting process
  performs its own retirement; that race is expected and leaves nothing to
  repair, so it is now reported as a single INFO line and `run-dir` no longer
  prints `worker_detail: queued snapshot ownership marker repair is pending`
  for it. Any other marker-update failure still logs the warning with its
  traceback and reports the repair detail. The same reclassification applies
  to the internal-engine (xTB/CREST) submission path, which duplicates the
  marker logic and emitted the same spurious warning.

- A rejected stage submission now records its reason on the stage. When a
  workflow stage submission fails, stage metadata gains `reason` (the
  submission's reason, or `queue_submission_failed` when it reports none) and
  `submission_error_detail` (the stderr, or stdout when stderr is empty,
  truncated to 1,000 characters), and a later successful resubmission clears
  both. The stage summary row, stage events, and the workflow report read
  stage metadata, so a rejection's cause now reaches all three — previously
  the detail survived only on the stage's raw `submission_result` payload,
  while the workflow error message told the reader to "see each stage's
  submission_error_detail", a field nothing wrote. This applies to ORCA, xTB,
  and CREST stages alike. For ORCA stages the contract-metadata writer also
  needed the same preserve-on-empty rule that `queue_id` and `run_id` already
  had: a submission-failed stage has no run, so the contract loader returns
  an unknown contract with an empty reason on the same sync tick and every
  later one, and writing that empty value would have erased the recorded
  reason immediately. The regression tests for the original incident (OptTS
  submissions rejected with no recorded reason) now run against this path,
  including an integration test through the ORCA stage sync.

### Removed

- Removed the workflow registry's dead `submission_summary` plumbing: the
  record field, its `updated_at` fallback, and the workflow-summary key. Its
  only writer was removed with the unreachable submitter cluster in 1.0.0 and
  no production run ever produced the data, so registry rows and workflow
  summaries simply stop carrying an always-empty mapping. Registry ordering is
  unchanged — with the field always absent, the fallback already resolved to
  the record-build timestamp.
- Removed the producer side of the submission-intent-token plumbing. Its only
  producer of a non-empty token was the workflow-level submitter cluster
  removed in 1.0.0, so `submit_reaction_dir` loses its always-empty
  `submission_intent_token` parameter, the trace-kwarg branch is gone, and
  queue submission no longer carries the dead metadata write. The restart
  stale-key scrub keeps clearing `submission_intent_token` out of durable
  stage metadata written by earlier releases, so old workflows are unaffected.
- Removed three helpers in `orca/commands/_helpers.py` whose consumers died
  releases ago: `_human_bytes` (its last production callers went with the
  cleanup and monitor command removals; the messenger reduction later removed
  a separate same-named duplicate elsewhere), `finalize_batch_apply` (last
  consumer removed with the organize feature), and the unused
  `_MAX_SAMPLE_FILES` constant, together with the module's unused logger.

## [1.0.0] - 2026-08-02

orca_auto's first stable release. Every surface documented in
[docs/PUBLIC_CONTRACTS.md](docs/PUBLIC_CONTRACTS.md) is now a committed
contract; from here on, breaking a documented behavior requires a major
version. Readers upgrading from 0.2.x should also read the
"Upgrading from 0.2.x" section at the end of this release's notes.

### Changed

- `orca_auto queue list` now expands CREST, xTB, and ORCA child jobs beneath
  each workflow by default, so the combined text view shows every queued
  workflow simulation and its current status without requiring engine filters.

- A queued row is claimable only once its publication is committed. Owner-PID
  liveness is no longer consulted in that decision. Previously a row left in
  `preparing` or `repairing` became claimable as soon as the recorded publisher
  PID looked dead, reused, or zombied. Each engine worker already repaired
  publications before reserving work, so in practice this only decided rows that
  appeared between that repair pass and the reservation read — but for those it
  let a worker start a calculation whose durable queued record had never been
  written, and moving the row to `running` then permanently prevented the repair
  path from ever writing it. This also contradicted the documented contract that
  only rows carrying a publication marker are executable. Such a row is now
  parked until the repair pass publishes the record and marks the lease
  complete. Recovery from a dead or reused publisher is unchanged in capability:
  the repair pass reclaims a lease under the publication lock whether or not the
  recorded owner is alive.

- The xTB and CREST workers now report a publication-repair problem instead of
  stalling admission silently. A blocked reservation logs the same warning the
  ORCA worker has always logged, and a repair that raises now logs the failing
  queue id and root with its traceback before the sweep moves on, which no
  engine reported before.

- `orca_auto init` validates an executable path with the same rule the config
  loader applies, so a path the wizard accepts can no longer be rejected at
  startup. The prompt's messages change accordingly and no longer echo the
  rejected path.

- Stage report and SI identity matching read only the nested `job` and
  `engine_payload` identities. The flat top-level `job_id`, `run_id`, and
  `queue_id` keys belonged to a pre-schema artifact layout that the engine and
  schema gates already refuse to load, so a stray flat key can no longer widen
  the conflict guard and reject a state that matches its stage.

- A pending SI publication is retried on every worker cycle instead of waiting
  out an exponential backoff. `si_publish_next_retry_at` is gone, along with the
  30/60/120/240-second ladder; the attempt budget is unchanged, so a repeatedly
  failing publication now blocks after about two minutes at the default 30-second
  cycle rather than about seven and a half. A transient outage lasting longer
  than that will block the workflow and need
  `orca_auto run-dir <workflow_dir> --force`. This is a public contract change.
  Upgrading with a workflow mid-backoff makes it immediately due: nothing reads
  the stored timestamp any more, so it is retried on the first cycle after the
  worker restarts, and the key is scrubbed the next time the workflow is
  re-armed.

- An identity mismatch that has not been quarantined yet no longer writes a
  reconciliation marker into the registry. That marker existed to keep such a row
  visible, but the authoritative identity recheck at clear time already covers
  the case, and the marker is derived state recomputed on every sync. One
  behavior goes with it: a row that was already hidden by a cleared marker before
  the mismatch appeared now stays hidden rather than being resurrected on the
  next reindex. Quarantined rows are unaffected — they keep their marker and are
  still protected from being cleared as stale.

- Writing `workflow_si.md` fails closed on every path. `write_workflow_si` and
  `collect_workflow_si_data` had switches that logged and degraded instead of
  raising, so a failure inside a configured SI feature could publish a document
  that reads as complete but is not. For the interaction-energy assembly that
  meant a missing section; for the RMSD re-dedup it was worse, because the
  degraded path returns the un-deduplicated ensemble — the merged duplicate
  reappears as a second row and the Boltzmann populations are recomputed over a
  double-counted ensemble, so the published numbers are wrong rather than
  absent. Production already passed the strict value at both call sites; the
  lenient default was what nothing selected, and it is gone. A failure now
  leaves the last known-good `workflow_si.md` untouched and reaches the durable
  publication retry state machine, which retries or blocks the workflow.
  `write_workflow_si` returning `None` now means only that the workflow has no
  ORCA stages, never that publication failed.

- A directory fsync that fails is now reported instead of being skipped. Five
  errno values (`EINVAL`, `ENOSYS`, `ENOTSUP`, `ENOTTY`, `EOPNOTSUPP`) used to be
  swallowed on the assumption that the filesystem could not fsync directories,
  which meant a durable artifact could be published with no durability barrier
  while the caller was told it had one. Every supported filesystem fsyncs
  directories, and callers already compensate for a barrier that fails after the
  rename made the file visible.

- `ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES` now works for stage events. Setting it to
  include a stage event type had no effect: a second gate dropped every stage
  notification whose engine was CREST, xTB, or ORCA — which is all of them — so
  the opt-in was silently inert for exactly the operator who had configured it.
  The opt-in is now the only thing deciding which journal events notify. The
  default set still excludes stage events, so an operator who has not set the
  variable sees no change.

- The CREST phase summary reports the conformers CREST actually retained.
  "Retained conformers" counted the named ensemble *files* that survived
  validation, which the engine caps at four, so a run that produced dozens was
  reported as `2`. The metric is now "Conformers" and counts the frames in
  `crest_conformers.xyz`, the same number the HTML report already showed. A
  stage with no readable ensemble file reports `-` rather than a false `0`.

- A non-string identity value in `orca_auto.yaml` now reports "must be a string"
  rather than "must be a string or integer". The integer form was never accepted
  on any key that used this validator, so the old wording described an option
  that did not exist.

- `orca_auto queue worker` no longer labels a conflicting worker as `orca_auto`
  or `unknown`. The refusal, its exit code, and the `command:` line naming the
  process that holds the queue root are unchanged; only the label and the
  wording it selected are gone.

- The public contract document no longer carries the 0.x Stable
  Core / Experimental tier split: every documented surface is committed. As part
  of the promotion, `systemd install --worker-only` and `--config` appear in
  `--help` instead of being hidden, and the workflow notification environment
  variables `ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES` and
  `ORCA_AUTO_FLOW_NOTIFY_DISABLED` are documented in the reference. Behavior of
  all four is unchanged.

- `queue.json` rows are now validated against the canonical schema instead of
  being defaulted field by field. A row that is missing any of the thirteen
  fields, carries an unknown one, or holds a value of the wrong type raises
  `Queue entry fields do not match the canonical schema: missing=… unknown=…`.
  Validation is whole-file and fail-closed, so one malformed row makes the queue
  unreadable to `queue list` and to every worker until it is repaired. 0.3.0
  silently substituted defaults in each of those cases.
- Admission slot records changed shape and are validated on load.
  `owner_boot_id` is now required rather than optional, `engine_process_state`
  defaults to `"idle"` instead of an empty string, and serialization no longer
  omits unset keys. **This is an upgrade hazard.** 0.3.0 wrote generic
  queue-worker slots that omit the five engine keys and can omit
  `owner_boot_id`; such a record now raises
  `Admission slot file contains an invalid process record: <path>` for the
  entire shared admission file, which blocks admission for every engine until
  the file is removed. Drain the queue and delete
  `<runs_root>/.admission` (or the configured `scheduler.admission_root`) before
  starting workers on this release if any 0.3.0-era slot file survives.
- The ORCA run lock is now a kernel `flock` rather than a JSON file with
  stale-owner recovery. Its payload keeps `pid` and `started_at` and drops
  `boot_id` and `process_start_ticks`. `Lock file exists but owner PID is
  unreadable. Remove manually: <path>` and `Detected stale lock but failed to
  remove it (pid=…)` no longer occur, and the surviving conflict message can
  now report `pid=unknown`.
- A workflow workspace whose persisted `workflow_id` is blank is now
  quarantined with `workflow_id is required` instead of falling back to the
  directory name.
- Reworded four admission error messages, dropping "managed" from three
  (`Managed admission slot owner identity is incomplete` →
  `Admission slot owner identity is incomplete`, and likewise for the two
  identity-verification messages) and narrowing
  `Admission owner and engine boot identities are incomplete` to
  `Active admission engine boot identity is incomplete`. The workflow
  phase-summary notification changed from `Stages 5  completed=3  failed=1` to
  `Stages 5 | completed=3, failed=1`.
- Replaced the transactional systemd installer with a plain write-and-reload
  installer. A failed or interrupted `orca_auto systemd install` no longer rolls
  back: the new unit files are already in place, and the command stops with the
  failed step's exit status after reporting
  `systemd install command failed after unit files were updated; fix the failure
  and rerun \`orca_auto systemd install\``. Recovery is to fix the cause and
  rerun the install. Concurrent installs are no longer serialized, since the
  install lock is gone. `dry-run`, the warnings, and
  `service status` / `service restart` are unchanged.
- Re-scoped `docs/PUBLIC_CONTRACTS.md` into two tiers. The document had grown to
  declare almost the whole surface — CLI, config, queue, artifacts, workflow,
  systemd — as stable, which made every removal read as a breaking change. Only
  a small Stable Core is now a committed contract: `run-dir`, `queue list`, and
  `queue cancel` with their queue-first, cancellation, and recovery semantics;
  `runs_root`; the engine executable paths; the shared concurrency limit; and
  `job_state.json` / `job_report.json` as durable machine artifacts. Everything
  else the document describes is accurate for the release but may change. This
  re-scopes only the public API — naming, shape, and presence. It does not relax
  the fail-closed validation, durability, and recovery behavior the
  implementation applies regardless of tier.

- The ORCA output-tail reader used for the delta-E table no longer re-derives
  the path it already holds open. A component-by-component directory walk, a
  root-node round trip, three by-name stats duplicating what the descriptor had
  pinned, and five seven-field stat comparisons are gone. What remains is a
  weaker containment guarantee, and deliberately so: the removed walk proved
  from the root downward that no component had been swapped, whereas the
  surviving check is a path resolution taken before the file is opened plus a
  device/inode comparison. Opening the parent with `O_NOFOLLOW` rejects only
  that final component being a symlink; a swapped intermediate ancestor is
  followed silently. For any run whose runs root is not being manipulated
  underneath it, the reader returns exactly what it returned before.

- Merged the CLI parser-wiring modules into one `cli_parsers` module and the
  worker spec/conflict helpers into `cli_workers`. The command surface, flags,
  help text, and error styling are unchanged.

- Flattened the remaining single-implementation queue-worker indirection:
  orphan reconciliation now lives in one module with direct calls, the
  worker-lifecycle hook mapping layer is gone (replay builds the core hooks
  directly), and the file-lock helper lost its grouped-options wrapper.
  Reconciliation decisions, lock semantics, and log/error messages are
  unchanged.

- Replaced the single-implementation dependency-injection plumbing in the ORCA
  job-location loaders and the workflow ORCA adapter with direct imports. The
  loaded payloads, contracts, and error behavior are unchanged; the modules
  regain static typing.

- Collapsed the internal engine-notification pipeline into a single module.
  The rendered notification lines, severities, workflow-child suppression, and
  the public `notify_{xtb,crest}_job_{queued,started,finished}` entry points
  are unchanged.

### Removed

- Removed the `resources.max_cores_per_task` and `resources.max_memory_gb_per_task`
  manifest aliases. A `flow.yaml` setting either under a `crest:` or `xtb:`
  block's `resources:` mapping now fails with
  `Unknown manifest resource fields: [...]`; use `max_cores` and
  `max_memory_gb`, which mean the same thing. The
  `Conflicting manifest resource aliases:` error is gone with them. The config
  keys of the same name under the top-level `resources:` section are unaffected.
- Removed the `--resolve-pending-restart` flag from `orca_auto systemd install`,
  together with the install transaction directory
  `/etc/systemd/system/.orca_auto-install-transaction/` (`owner.json`,
  `manifest.json`, `committed.json`, `backup/`) and the install lock. The flag
  was hidden from `--help` but was the documented recovery procedure in
  `systemd/README.md`, and passing it now fails as an unrecognized argument. A
  transaction directory left behind by 0.3.0 is ignored rather than recovered
  and can be deleted; the 0.3.0 instructions to preserve it no longer apply.
  About twenty transaction-specific error messages went with it.
- Removed the ORCA process record: `<generation>/orca.process.json` and
  `.orca.process.lock` are no longer written, and PID/PGID ownership lives in
  the shared admission record instead. Both names were documented in 0.3.0. As a
  consequence they are also no longer rejected as ORCA dependency basenames or
  reserved as scratch/durable artifact names, so a submission 0.3.0 refused on
  that ground now succeeds. Files left by 0.3.0 are inert and can be deleted.
- Removed the `02_orca` workflow stage-directory alias; only `03_orca` is
  recognized. A workspace still carrying an `02_orca` stage directory from an
  older layout is no longer discovered by `queue list`, the activity views, or
  indexing. Rename the directory to `03_orca` to keep such a workspace visible.
- Removed six engine manifest keys. `crest:` no longer accepts `no_reftopo`,
  `no_topo`, `no_cbonds`, or `len`; `xtb:` no longer accepts `namespace` or
  `opt`. Both mappings are strict, so a `flow.yaml` still carrying any of them
  fails submission with `Unknown CREST manifest fields:` or
  `Unknown xTB manifest fields:` rather than ignoring the key. Five of the six
  were aliases and must be renamed, not deleted: `no_reftopo` → `noreftopo`,
  `no_topo` → `notopo`, `no_cbonds` → `nocbonds`, `len` → `mdlen`, and
  `opt` → `opt_level`. **Deleting `xtb.opt` instead of renaming it silently
  changes the calculation**: `opt` set the xTB optimization level, and without
  it the run falls back to `--opt normal`, so an `opt: tight` job becomes a
  looser optimization with no error. `len` is the same shape — it set the CREST
  MD length in ps and 0.3.0 documented it as an alias of `mdlen` that had to
  agree — so a working manifest can quietly change meaning if the key is dropped
  rather than renamed. Only `xtb.namespace` has no replacement and should simply
  be deleted; it was already rejected when non-empty in 0.3.0, and is now
  rejected whatever its value.
- Reduced per-run report output to one machine artifact and one human artifact.
  `job_report.md` is gone, along with the `artifacts.report_markdown_commit`
  marker embedded in `job_report.json` and the `report_md_path` runtime lookup;
  stage detail links now go `job_report.html` → `job_report.json`. The two
  format duplicates of the SI dataset, `si_data.csv` and
  `interaction_energy.csv`, are also gone together with the latter's
  ownership-marker file. `job_report.json` keeps every field except the
  `report_markdown_commit` marker, `job_report.html` is unchanged, and
  `workflow_si.md` loses one clause: the RMSD-representative note no longer
  points readers at the removed `si_data.csv` for per-structure degeneracy. No
  computed value in any surviving artifact changes. Existing files from earlier
  runs are left in place and are no longer read.
- Removed the workflow-level ORCA submitter cluster:
  `flow/submitters/orca_submission.py`, `flow/submitters/orca_cancellation.py`,
  `flow/submitters/orca_models.py`, and the
  `submit_reaction_ts_search_workflow` / `cancel_reaction_ts_search_workflow`
  entry points they backed, together with
  `recover_exact_reaction_dir_submission`, whose only caller in `src` they were.
  This pair submitted and cancelled an entire reaction_ts_search workflow in one
  call by walking its stages itself. Nothing reached it:
  `default_orchestration_services()` wires the per-stage path instead, and no
  command, adapter, or orchestration module imported either function. ORCA
  stages are still submitted and cancelled the way production already did it —
  `sync_orca_stage_impl` through `services.engines.submit_reaction_dir`, and
  workflow cancellation through `services.engines.orca_cancel_target`, both
  unchanged; CREST and xTB stages have their own submitters and are untouched.
  `submit_reaction_dir` and `cancel_target` in `flow/submitters/orca.py` are
  untouched. This also settles the `skip_submitted` keyword deferred in the
  earlier switch removal: it was a parameter of the removed entry point and goes
  with it.
- Two workflow metadata keys lose their only writer with the cluster and are no
  longer produced: `submission_error_detail`, the stderr excerpt promoted onto a
  stage whose submission was rejected, and `submission_summary`, the per-workflow
  submission rollup. Both were written only from the unreachable path, so no run
  that this release replaces produced them either. The rejection `reason` and
  `stderr` themselves are unaffected: they are still recorded on the stage's
  `submission_result` and still surface in the workflow report.
- Removed the `orca_auto scan-notify` command and the DFT monitor subsystem
  behind it, including the public `has_monitor_updates`, `monitor_message`, and
  `notify_monitor_report` helpers. The command scanned the runs root for ORCA
  outputs, diffed them against a state file, and posted a digest of what had
  changed since the last scan. The runtime's own lifecycle notifications cover
  the same events without a periodic scan: a card is posted when a run is
  queued and again when it reaches a terminal state. They do not carry the
  digest's parsed chemistry — molecular formula, method/basis, final energy,
  calculation type, and the `NOT CONVERGED` / imaginary-frequency notes are no
  longer pushed to the channel, and neither is the digest's scan-parse-failure
  list. That detail remains in `job_report.json` and the HTML report. Operators
  who invoked `scan-notify` from cron or a timer should drop that entry. The
  monitor's `<runs_root>/.dft_monitor_state.json` is no longer read or written
  and can be deleted. `docs/DISCORD_SETUP.md` now verifies delivery with
  `run-dir`.
- Removed the DFT discovery module. Its scan entry point was reachable only
  from the monitor; run snapshots keep the one helper they imported from it —
  the latest-`.out` lookup inside a run directory — so `queue list` behavior is
  unchanged.
- Removed thirteen keyword switches whose non-default branch production never
  selected, and the one function that existed only to feed one of them. The HTML
  report components lose the toggles the composer always passed at the same
  value, `manifest_int` loses `zero_is_absent`, `runtime_paths` loses
  `include_state`/`include_report`, the workflow worker parser loses
  `include_json`, and the internal-engine replay loses
  `suppress_queued_notification` — replaying an existing entry never re-announces
  it, so the context key is now written unconditionally. The three switches that
  look inert but are flipped through a dict literal, an engine definition table,
  and a late-bound local are untouched.
- Removed two internal surfaces with no readers: the `provider` and `message_id`
  fields on `SendResult`, which nothing read once the messenger became a one-way
  notifier (the response is still checked for a message id, since a response
  without one is not a confirmed delivery), and the re-normalization of payload
  keys that their producer already normalizes. Both the loader path and the
  context path build that payload through one function, so the assembler was
  normalizing its own output. The three request fallbacks keep their
  normalization, because those read caller input rather than producer output.
- Removed the `/proc` cmdline classifier behind the worker-conflict message. It
  was not dead code — it ran whenever a conflicting worker was detected — but
  its only effect was choosing between two wordings of the same refusal. The
  surviving message names the holder's argv, which identifies it more precisely
  than the label did.
- Removed the owner-liveness helpers behind the claimability decision above:
  `queue_record_sync_is_stale`, which `orca_auto.core.queue` also re-exported,
  and the private `/proc`-based probes it called. `process_start_token` and
  `current_process_start_token` are unaffected, and publication leases still
  record the owner pid and its start token.
- Removed the write-only DFT SQLite index (`dft.db`). Nothing read the database,
  so it is no longer created or updated. (The `scan-notify` monitor, which kept
  a separate change-detection state file, is also removed in this release — see
  below.)
- Removed dead internal surfaces with no remaining callers: the ORCA
  job-location reindex chain, the five standalone per-job-type HTML page
  renderers superseded by the report composer, unused queue-adapter and
  admission entry points (`reserve_slot_or_raise`, per-app admission limits,
  work-dir exclusion hooks), the unused messenger `silent`/multi-message
  result fields and `code_block` span, the unreachable engine terminal
  notification entry points, and assorted dead helpers in `core.state`,
  `core.queue`, and `core.config`. None of these were reachable from the CLI,
  the workers, or the documented contracts.

- Removed the `orca_auto smoke` CLI command and the `orca_auto.smoke` package
  behind it, retiring the retained smoke-review harness and its packets. This is
  a public-contract change.
- Removed the `python -m orca_auto.smoke` and `scripts/smoke.sh` entrypoints;
  there is no longer a supported way to run the smoke harness.
- Removed the reserved `<runs_root>/.orca_auto_smoke` runs-root namespace. That
  name is no longer created or special-cased in submission, discovery,
  reindexing, snapshots, or cleanup.
- Reduced the messenger to a one-way Discord outbound notifier. These were
  Experimental surfaces:
  - Removed the Telegram messenger provider; `messenger.provider` now accepts
    only `discord`.
  - Removed the interactive bot command/action framework, the `orca_auto bot`
    CLI, and the `orca_auto-bot@.service` systemd unit.
  - Removed the remote Discord archive-upload ingestion
    (`messenger.discord.uploads`); run directories can no longer be submitted
    over Discord.
  - Dropped the `discord.py` dependency.

  Before upgrading, remove `messenger.telegram`, any top-level `telegram:`
  block, and `messenger.discord.uploads`, `messenger.discord.channel_ids`, and
  `messenger.discord.allowed_user_ids` from `orca_auto.yaml`; those keys now
  fail configuration loading. `channel_ids` and `allowed_user_ids` were valid
  `messenger.discord` keys in 0.3.0 and served the interactive bot, which is
  gone; outbound delivery uses `default_channel_id` alone. The error names the
  section rather than the offending key, so this list is the recovery path.
  After upgrading, rerun
  `orca_auto systemd install --user <name> --repo <path>` to rewrite the runtime
  target, and, if the `orca_auto-bot@<user>.service` unit was installed, remove
  it by hand (`systemctl disable --now orca_auto-bot@<user>.service`,
  `rm /etc/systemd/system/orca_auto-bot@.service`, then
  `systemctl daemon-reload`). The installer no longer renders that unit, and a
  stale runtime target would otherwise pull in a unit whose `ExecStart` is gone.

### Upgrading from 0.2.x

An audit of every commit in the 0.2 series found behavior that shipped without
a changelog entry. The published 0.2.x sections are left as they shipped; what
still matters on an upgrade to this release is recorded here instead.

- **Workflow directory names cannot contain `(` or `)`, and an existing
  workflow directory must never be renamed.** Since 0.2.0, `scaffold` and
  `run-dir` reject a parenthesised name, and advancing a workflow whose
  directory name no longer matches its persisted `workflow_id` fails it with
  `workflow_error.scope = workflow_identity_validation`. Restore a renamed
  directory to its original name, or create a new workflow.
- **A config that still holds a top-level `telegram:` block fails loading**
  with `Unknown top-level config fields are not supported.` — the error names
  no key, so this note is the recovery path. 0.2.0 moved messenger settings
  under `messenger:` behind a compatibility shim that has since been removed,
  and the Telegram provider itself is gone. Delete the block and configure
  `messenger.discord`.
- **Your ORCA `.inp` files are no longer edited in place.** Through 0.2.0,
  submission rewrote the selected input to inject `%pal nprocs` / `%maxcore`
  when missing, logging `Updated ORCA input resource directives in <path>`.
  Since 0.2.1 the values are resolved in memory into the private execution
  snapshot and the log line reads `Prepared private ORCA input resource
  directives for <path>`. The resources the job runs with are unchanged; a
  script that read the injected directives back out of the input will now find
  the original values.
- **`flow.yaml` and engine job manifests must be plain single-link regular
  UTF-8 files.** A symlinked or hardlinked manifest has failed closed since
  0.2.0; replace it with a real copy.
- **Elements above atomic number 86 no longer pass xTB/CREST admission**, and
  charge/multiplicity must leave a non-negative electron count with
  parity-consistent unpaired electrons. Manifests that silently defaulted a
  malformed `multiplicity` in 0.1.x now fail.
- **`job_report.json` `input.primary_path` points at the bound executed input**
  under the execution generation, not the source path you submitted. Scripts
  that resolved sibling files relative to it should use the recorded
  source-path provenance instead.
- **Reaction endpoint pairs are ordered by rank gap** since 0.2.0, so a capped
  fan-out samples both endpoint ensembles instead of exhausting the first
  reactant's conformers. Comparisons against 0.1.x runs should not expect the
  same candidates under a cap.
- **TS-guess screening is tunable** via the `xtb.ts_guess_validation` manifest
  block (`enabled`, `bond_scale`, `max_spurious_bond_changes`,
  `reacting_bond_stretch_scale`); a rejected guess fails its handoff with
  reason `xtb_ts_guess_geometry_invalid`.
- **Messenger delivery knobs are clamped**, not honoured verbatim:
  `timeout_seconds` to 0.1–120 s, `max_attempts` to 1–10,
  `retry_backoff_seconds` to 0–120 s.
- **A malformed config reports only `Invalid YAML syntax: <path>`.** The
  parser's line/column detail is withheld because config files carry
  credentials; locate the error with your own YAML tooling.
- **`queue list` no longer prunes stale admission slots** (since 0.2.1). The
  `active_simulations:` count is a lock-free read; reclaiming capacity left by
  a crashed worker is solely the recovery path's job.
- **Housekeeping:** a `.job_state.mutation.lock` dotfile at each ORCA job root
  is internal and expected (since 0.2.1); execution snapshots under
  `.orca_auto_input_snapshots/` and `.orca_auto_orca_executions/` are retained
  with no GC command — budget disk and reclaim them only with the retired job.

## [0.3.0] - 2026-07-21

### Removed

- Removed the standalone xTB-MD engine (the deliberately narrow public exception
  introduced in 0.2.0). This deletes the `xtb_md_job.yaml` manifest contract, the
  `--engine`/`--app xtb_md` CLI values, the `scheduler.max_active_xtb_md` config
  key, the `orca_auto-xtb-md-worker@.service` unit, and the `xtb_md`
  queue/activity engine value. General xTB and CREST remain available as internal
  workflow stages, and ORCA remains the standalone engine. Existing terminal
  xTB-MD run directories are left in place as historical artifacts. Before
  upgrading, drain any pending/running xtb_md queue rows, remove
  `scheduler.max_active_xtb_md` from `orca_auto.yaml`, and, if the
  `orca_auto-xtb-md-worker@<user>.service` unit was installed, disable and
  remove it (`systemctl disable --now orca_auto-xtb-md-worker@<user>.service`,
  then `systemctl daemon-reload`).

### Changed

- Workflow-internal xTB and CREST jobs now use `job_state.json` as their only
  terminal metadata artifact. They no longer write or read `job_report.json`
  or `job_report.md`; report-only jobs, completed outputs without terminal
  identities, and stale artifact paths that require basename remapping are
  unsupported and must be resubmitted. ORCA reports now follow the
  generation-only contract described below.
- Added optional ORCA RAM-backed attempt workspaces below `/dev/shm`. Bound
  inputs are staged privately, durable queue/state/process ownership stays on
  disk, and surviving non-temporary outputs are copied once into the visible
  generation after the process tree exits; ORCA `*.tmp` files are not persisted.
  A root lock admits one scratch attempt, and tmpfs free-space plus host-memory
  headroom checks run before launch. Generation identity is inode-pinned;
  journaled file-set publication rolls partial replacement back, reserves
  runtime-state names, and preserves unresolved workspaces fail-closed.
  Completed attempts and interrupted committed publications record distinct
  scratch evidence without altering immutable execution-snapshot provenance.
  Root/workspace descriptor pinning prevents pathname substitution, captured
  input payloads close preflight-to-staging races, and a durable PID/PGID launch
  gate prevents an ORCA process from starting before ownership is recorded.
- Simplified runtime ownership by replacing workflow service locators and
  engine forwarding facades with canonical orchestration, engine, queue, and
  ORCA-domain owners. Bot command/upload handling and workflow SI collection,
  science, rendering, and publication now have explicit module boundaries while
  retaining their public CLI and artifact contracts.
- Added a default engine-worker target for the ORCA queue-worker systemd
  service. Worker-only and full-runtime install, status, and restart operations
  manage it, while the opt-in workflow worker remains separate. The bot now uses
  the same bounded systemd restart policy.
- Reject unknown, misspelled, removed, or malformed execution configuration
  instead of silently applying defaults. Engine settings now use only the
  canonical shared scheduler, resource, and messenger sections; omitted keys
  retain their documented defaults.
- Publish and consume ORCA reports only inside a provenance-verified execution
  generation. Pre-relocation job-root reports remain untouched but are no
  longer runtime inputs, and existing identityless queue rows fail closed
  instead of adopting adjacent state or reports. Operators who need old report
  detail must migrate it to the verified generation or archive it separately
  before removing the root files.
- Runtime lookup now exposes generation-local `job_report.md` only when the
  schema-version-1 JSON contains the current byte-length and SHA-256 commit
  marker. Existing schema-version-1 JSON remains readable, but its uncommitted
  Markdown path is hidden after upgrade. Committed Markdown is capped at 8 MiB;
  oversized reports retain JSON without publishing a Markdown path. There is no
  public migration command:
  republish only through a controlled tool that invokes the current report
  writer against verified generation state, or archive the old Markdown outside
  public runtime lookup.
- Removed duplicate ORCA status decoding, dead private helpers, and repeated
  read-only workflow request-parameter traversal; refreshed workflow/systemd
  documentation, release metadata, and workstation-neutral test fixtures.

### Fixed

- Post-merge review findings from the recent change series, batched:
  - A claim whose crashed generation already holds a completed,
    analyzer-verified output is no longer rebound into a fresh generation
    (which re-ran the whole calculation); the claim-time verification admits
    the finished generation's runtime files and the ordinary
    completed-adoption path claims the result.
  - Crash-recovery checkpoint seeding considers every attempt stem (base,
    `retryNN`, `resume`) and seeds the newest-written intact `.gbw`, so a
    crash during a generated retry or resume attempt no longer seeds a stale
    first-attempt checkpoint or misses the only one.
  - A generation-shaped directory name alone no longer grants workflow
    engine paths: the parent must be a scaffold or the workspace must carry
    its committed `workflow.json` (which also keeps a workspace addressable
    after the scaffold's mutable `flow.yaml` is removed).
  - `worker_cycle_started`/`worker_cycle_finished` notifications (opt-in via
    `ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES`) render through the worker-lifecycle
    template again instead of a generic card that dropped the session id.
  - A scan-TS payload stripped of `max_scan_extensions` fails closed instead
    of silently regrowing a default extension budget, matching the required
    stage-budget contract.
  - Same-second execution generations order by actual recency (directory
    mtime breaks the timestamp tie) instead of by random hex suffix, so
    direct-directory readers no longer pick a stale generation as newest
    after a rapid resubmission.

### Added

- Crash recovery seeds the SCF checkpoint alongside the geometry: when the
  crashed generation holds a non-empty runtime `<stem>.gbw` within the
  snapshot byte budgets and the submitted input does not already direct its
  own orbital chain (`MORead`/`%moinp`), the replacement generation
  materializes it as `<stem>.moinp.gbw` and the bound input gains `MORead`
  plus `%moinp "<stem>.moinp.gbw"`, so a recovered run restarts from the
  last written orbitals instead of reconverging from scratch. The rename is
  the one sanctioned basename mapping and is enforced fail-closed by the
  claim-time verification via the `recovery.checkpoint_role` provenance
  field; an absent, empty, or over-budget checkpoint degrades to
  geometry-only recovery. A checkpoint truncated by the crash itself cannot
  be detected up front (the gbw format carries no integrity marker here); if
  ORCA then rejects it, the run fails visibly with the MORead error in its
  output rather than silently restarting from scratch.

### Fixed

- A queued ORCA job interrupted by a host or worker crash resumes again
  instead of failing at reclaim with `Queued ORCA private dependency ...
  snapshot is corrupt`. The strict claim-time snapshot verification treated
  the runtime-updated same-stem geometry of a started run as corruption, so
  the first worker restart after a crash terminally failed the job (and its
  workflow). Recovery now materializes a fresh execution generation through
  the ordinary submission machinery — seeded from the crashed generation's
  last written geometry when it is intact, falling back to the submitted
  geometry when it is absent or truncated — swaps the queue row to the
  replacement atomically before execution, and leaves the crashed generation
  frozen as that attempt's record. Rebinds never consume the scientific
  retry budget and are bounded by a durable per-row recovery counter
  (`recovery_rebind_count`, limit 3) that is reserved before any new
  generation exists; at the limit the claim fails closed with an explicit
  `recovery limit` reason. The replacement snapshot records a `recovery`
  provenance block naming the crashed generation and every seeded input, and
  a claim with a pending cancellation is turned terminal through the ordinary
  cancel chokepoint instead of misreporting the crashed generation as
  corrupt. Dependency roles are assigned in the canonical order of the
  stored source paths (a recovery seed lives inside the previous
  generation, which sorts differently than the submission source it
  replaces). Engine-internal surfaces added:
  `build_orca_execution_snapshot(..., recovery_from=...)` and
  `orca_execution_started_evidence()`.

### Changed

- Standalone ORCA job reports (`job_report.md`, `job_report.json`,
  `job_report.html`, `si_block.md`) are written inside the execution
  generation that produced them instead of the job root, so a submitted job
  directory keeps only the user inputs, the coordination lock files
  (`.job_state.mutation.lock`, `.orca.process.lock`, `run.lock` — these stay
  at the root to serialize same-directory resubmissions), the live
  `job_state.json` while a run is active, and one generation per submission.
  Jobs without a bound generation (legacy states, submissions rejected before
  binding) keep the job-root reports as a fallback; readers check the
  generation first and fall back to the root, and the next run in a reused
  directory removes stale pre-relocation root copies when it publishes its
  generation reports.
- A workflow ORCA stage that is resubmitted after a `submission_failed`
  attempt clears the stale failure `reason` (alongside
  `submission_error_detail`) once the new submission succeeds, so a stage
  that later completes no longer reports the superseded rejection in the
  final workflow report.

### Removed

- Removed `queue list --watch`, its `--interval` option, and the display-only
  system/per-job CPU, RAM, and load samplers. Ordinary `queue list`, JSON,
  cancellation, durable admission, execution memory limits, and RAM-scratch
  headroom checks are unchanged.
- Internal over-engineering residue identified by the 2026-07-17 audit
  (no public CLI, config, or artifact contract changes):
  - The inert ORCA retry-recipe scaffolding: `retry_recipes.py` (a no-op
    since the recipe ladder was retired), `RetryRecipeName`,
    `RetryPolicy.recipes`, and the unreachable generic non-ScanTS retry
    rewrite branch. Non-ScanTS inputs that reach the retry path through a
    resumed state's persisted budget still fail closed with
    `no_retry_rewrite_available`.
  - The five-layer forwarding stack behind the shared engine job-location
    exports (`EngineLocationRoots`/`Store`/`Artifacts`/`Service`/`Module`
    and the supplier-injected API builder). The three engine consumers keep
    the same `build_store_backed_engine_job_location_exports` entry point,
    now returning one `EngineJobLocations` object with identical behavior,
    including call-time store-function lookup for monkeypatching.
  - Six request-carrier dataclass round-trips in `orca/attempt/reporting.py`
    (public function names and signatures unchanged).
  - Pure-coercion dependency injection: the triplicated `SafeIntFn`
    protocol and the `safe_int`/`normalize_text`/`normalize_bool` `*_fn`
    plumbing in the ORCA contract adapters, plus the never-overridden
    `_normalize_text`/`_stage_metadata` orchestration override hooks.
  - The empty `behavior` config section (`EmptyBehaviorConfig`,
    `BehaviorConfig`) and the constant `paths_cls`/`behavior_cls`/
    `app_config_cls` injection seam in the shared engine config loader;
    `orca.config` now uses `RetryRuntimeConfig` directly instead of the
    `CommonRuntimeConfig`/`RuntimeConfig` alias pair.
  - Assorted dead surfaces: four unused smoke procfs access-path properties,
    the path-based fallback half of `rebuild_smoke_index`, argument-preserving
    submitter lambdas, `(*args, **kwargs)` protocol ceremony in the queue
    worker deps, the trivial `build_engine_notifier` factory, the dead
    `max_concurrent` parameter of `normalize_admission_limit`, and the
    redundant CI `systemd-units` job (the same test already runs in all
    three matrix legs).
  - The post-publication re-verification pass of the smoke review packet
    (`_verify_review_projection` and helpers). Copy-time no-follow,
    identity-pinning, size, and SHA-256 checks remain; the packet records
    copy-time provenance, and sources that mutate after their copy no
    longer abort publication of the already-consistent projection.
  - The `authorized_operator` action audience, which had no production
    issuer. All interactive bot actions are originator-bound.
  - The `EngineNotificationModule` layer between `EngineJobNotifications`
    and `EngineNotificationDelivery`, along with its consumer-less
    keyword `notify_finished` variant. `build_engine_job_notifications`
    keeps its signature and the xTB/CREST notification entry points are
    unchanged.

### Fixed

- The reaction TS handoff materializes the xTB seed Hessian as
  `<inp stem>.inhess.hess` instead of `<inp stem>.hess`. The old name is what
  ORCA itself writes under a `Freq` route, so the execution-snapshot binding
  rejected every OptTS+Freq candidate submission with
  `ORCA referenced input basename conflicts with a generation runtime/output
  file` and a workflow whose xTB phase succeeded still failed with
  `ts_candidate_limit_exhausted` before any ORCA job ran. This is a forward
  fix: ORCA stages are not re-materialized on restart, so a workspace whose
  candidate directories were written before this fix still carries the old
  `InHessName "<stem>.hess"` input and must be re-run as a fresh workflow
  rather than restarted.
- A rejected workflow ORCA queue submission now records why: the failure
  reason lands in the stage metadata (`reason`), the trimmed stderr in
  `submission_error_detail`, and both flow into the stage summary and the
  `workflow_stage_failed` journal event instead of leaving a bare
  `submission_failed` with no cause anywhere. The candidate-exhaustion
  workflow error message also distinguishes candidates whose submission was
  rejected before execution from candidates that ran without verifying a
  transition state.
- `queue list` shows workflow rows under the directory the user submitted:
  the scaffold name (for example `TS8_wf`) for scaffolded workspaces, whose
  generation directory name doubles as the workflow id and used to repeat the
  ID column verbatim. Stage-derived labels (reaction keys, candidate paths)
  stay in the Detail column.

- The xTB/CREST phase summary notification now derives its outcome, severity,
  and per-stage results from the same canonical aggregation as the workflow
  journal (`phase_snapshot`), instead of a private bucketing that ignored the
  reaction handoff verdict. A phase whose stages all completed at the process
  level but whose ORCA handoffs were all refused (for example nine xTB path
  searches with `xtb_ts_guess_missing`) now reports `Outcome: failed` with
  error severity instead of `completed` with success severity. The handoff
  verdict outranks the raw stage status in both directions: a stage whose
  handoff reached `ready` counts as completed even if a later attempt failed,
  and a stage whose handoff was refused counts as failed even though the
  engine exited cleanly. Rows whose stage and task statuses disagree, or that
  rest in an unsettled state, now classify fail-closed as failed instead of
  contributing an optimistic `mixed`/`completed` reading, and the summary is
  sent only once every stage *and* task status of the phase is terminal.

### Changed

- Workflow stage materialization reads the persisted stage budgets
  (`max_crest_candidates`, `max_xtb_stages`, `max_xtb_handoff_retries`,
  and `max_orca_stages` across the reaction, conformer, and scan
  pipelines) as required payload parameters instead of silently
  substituting defaults. Creation always records them, so a payload
  missing one is corrupt or hand-edited and now fails closed with an
  explicit error rather than expanding a stored workflow by a guessed
  amount.

- `reaction_ts_search` now expands all reactant × product CREST pairs by
  default: `max_xtb_stages` defaults to 9 (was 3), matching the default
  `max_crest_candidates: 3` per endpoint, so a default run explores every
  3×3 conformer pairing instead of silently stopping at the top three. The
  remote upload ceiling for `max_xtb_stages` rises from 8 to 9 to admit
  the new default; `max_orca_stages` (OptTS children) stays at 3. The
  `ts_search` scaffold template now writes the cap explicitly and its
  comment explains the pair-expansion limit.

- Workflow Discord/Telegram notifications show the workspace `Directory`
  (the generation directory inside its scaffold) on every workflow-scoped
  event — status changes, stage transitions, handoffs, phase summaries, and
  advance failures — mirroring the standalone ORCA `Directory` field. The
  `Worker session` token (an operational identifier that changes on every
  worker restart) no longer appears there; it remains on worker lifecycle
  events and in the durable workflow journal.

- Workflow workspaces now use the same layout as standalone ORCA executions:
  `run-dir` on a scaffold creates a timestamped generation directory
  (`YYYYMMDD-HHMMSS-<8hex>`) inside the scaffold itself, and that generation
  name is the workflow id shown by `queue list` and accepted by
  `queue cancel` and restart. Re-running `run-dir` on the same scaffold
  starts a fresh sibling generation. The previous layout (a
  `wf_<type>_<name>` workspace created next to the scaffold directly under
  `workflow_root`) and its prefix-derived ids are gone; workflow scaffolds
  must now sit directly under the configured `runs_root`, matching where
  the generated workspaces are discovered. Bot workflow uploads publish
  the extracted scaffold under `runs_root` and materialize the generation
  inside it, so a published upload directory now contains its own results
  and the bot's post-exception commit probe reduces to checking for a
  generation child (the `metadata.source_inputs` scan and its
  `requested_at` freshness bound are gone). Workspace discovery (registry
  reindex/listing, runtime scans, workflow summaries) recognizes both
  scaffold-nested generations and direct root children (direct API
  submissions without a scaffold); dot-directories such as the upload
  staging area are never scanned. The reserved-generation submission guard
  now distinguishes workflow workspaces (which carry `workflow.json`) from
  ORCA execution generations, which stay reserved.

### Fixed

- `orca_auto run-dir` works again for a scaffold placed directly under
  `workflow_root` (e.g. `orca_auto run-dir ~/orca_runs/rxn_001`). The CLI
  used to reuse such a scaffold's own directory name as the workflow id,
  which the hardened atomic workspace creation (0.2.0) always rejects with
  `FileExistsError: workflow already exists`. The scaffold now materializes
  into a fresh prefixed workspace (`wf_reaction_ts_<name>`,
  `wf_conformer_screening_<name>`, or `wf_scan_ts_<name>`, where `<name>` is
  the sanitized lowercased directory name, with a numeric suffix on
  collision) exactly like scaffolds elsewhere, and the scaffold
  directory itself is left untouched as input material. Bot workflow
  uploads publish the extracted archive directly under `runs_root` before
  submitting, so they hit the same collision and had been failing since
  0.2.0 as well; they submit again now, with the published upload kept as
  input material next to the new workspace.

- Uploaded workflows with a malformed or non-allowlisted
  `solvent`/`solvent_model` pair in the `crest:` or `xtb:` block are
  rejected at remote admission (upload confirm time) using the same
  validator the CREST/xTB runners apply at command construction, instead of
  enqueuing and failing only when the job builds its command line.

- Workflow payloads record the pre-copy source input paths as
  `metadata.source_inputs` provenance, and the bot's post-exception workflow
  commit probe and publication reconcile sweep use it to locate the durable
  workflow a published upload produced (the in-place `workflow.json` layout
  is still honored for historical workspaces). Previously they only looked
  inside the published upload directory, so a failure after the durable
  write — or a crash before the commit mark — would report a created
  workflow as failed and invite a duplicate resubmission.

- Queue dispatch order no longer depends on the wall clock. Same-priority
  pending entries are dequeued in queue-file row order (the true arrival
  order — rows are only appended under the queue lock) instead of by their
  `enqueued_at` stamp, both in the core store's `dequeue_next` and in the
  workers' cross-root selection. A WSL2 clock-skew correction between two
  enqueues could stamp the first arrival with a later time than the second
  and make a worker start the second job first (the intermittent
  `test_fill_slots_refills_immediately_after_completion` failure). Across
  different queue roots the wall clock remains the fairness comparator, as
  separate queue files share no arrival order.

### Changed

- The bot's remote-admission policy (server-owned resource caps, atom-count
  ceilings, the CREST cost policy, and the ORCA file-reference confinement
  walker) moved out of `flow/bot/application.py` into a dedicated
  `flow/bot/remote_admission` module of plain functions. A new test pins the
  bot's directive-key tables against the execution scanner's file-reference
  key sets, so a key added to `orca/input_blocks.py` without a matching
  remote-admission decision (validate or forbid) fails CI instead of silently
  opening a remote confinement gap. Behavior is unchanged.

- The Telegram delivery stack (API client, config, network, logging,
  transport, and HTML format helpers) moved from `core/notifications` into
  `core/messaging`, removing the one import cycle between the two packages;
  a new import-linter contract keeps messaging from ever importing the
  notification layer again. The three Telegram HTML escape implementations
  share one primitive now, and the 4096-character message limit has a single
  definition instead of three.

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
  the core queue package. Behavior converges on the safest of the previous
  per-engine copies: a failed
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
  `YYYYMMDD-HHMMSS-<8-hex>` directory directly under the submitted
  job directory. That name shape is reserved: any directory whose name
  matches it (ASCII date, time, and 8 lowercase hex digits) is treated as an
  execution generation everywhere under `runs_root`, excluded from production
  scans, and rejected as a `run-dir` submission target. The bound `.inp`, supported referenced inputs under their
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
  The live `job_state.json` remains the public summary at the job root, while
  `job_report.json` is published inside the execution generation it describes
  (see the report-placement change above).
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
