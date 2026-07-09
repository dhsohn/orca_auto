# Changelog

All notable changes to orca_auto are documented in this file.

This project follows a lightweight [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
style. Version numbers are recorded in `pyproject.toml`; release procedure lives
in [docs/RELEASE.md](docs/RELEASE.md).

## [Unreleased]

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

### Removed

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

### Changed

- An exhausted ScanTS retry-recipe chain now reports the actionable reason
  `scants_recipes_exhausted` (previously the generic `rewrite_failed`, which
  read like a rewriter bug). `rewrite_failed` is reserved for genuine rewrite
  crashes.

## [0.1.0] - Initial public development series

### Added

- Queue-first ORCA runtime with durable queue state, worker execution, retry
  state/report files, and organized output support.
- Internal workflow support for xTB and CREST stages.
- Linux/WSL-first CLI, configuration template, systemd user-service assets, and
  CI coverage for fake-engine integration paths.
