# orca_auto Detailed Reference

**English** | [한국어](REFERENCE.ko.md)

orca_auto is a queue-first executor for ORCA and workflow orchestration. ORCA
uses the shared internal-engine queue lifecycle for worker admission, child
entry execution, terminal side effects, and orphan recovery while preserving
its public ORCA queue contract. General xTB and CREST run as internal workflow-stage engines. This reference standardizes the shared public CLI and keeps the deeper
ORCA runtime behavior documented in one place, since ORCA still has the richest
reporting and monitoring surface.

Current developer-facing package rule:

- The canonical implementation lives in `orca_auto.orca`
- Shared infrastructure lives in `orca_auto.core`
- Supported imports live under `orca_auto.*`

For the narrower set of CLI, config, JSON artifact, workflow, and systemd
surfaces that are treated as public contracts, see
[PUBLIC_CONTRACTS.md](PUBLIC_CONTRACTS.md).

## 1) Project Purpose

- Work only within the configured `runs_root`
- Select and bind the most recently modified `*.inp` at submission
- Submit work durably through the queue
- Let a supervised worker execute queued jobs
- Record calculation failures without rerunning or overwriting the original input
- Use matching non-empty ORCA `.gbw` files for interrupted-run recovery inputs when available
- Record execution status and results alongside the calculation

## 2) Runtime Model

Current intended semantics:

- Public `run-dir` enqueues new work durably
- `run-dir` does not inspect existing outputs: a reaction directory whose queue
  row is still active is refused as a submission conflict, and one whose row is
  terminal is enqueued again as a new generation (unless that row still owns a
  pending terminal replay or a terminal fence marker, either of which is
  refused until it clears). Re-running a closed directory therefore relaunches
  ORCA as a new generation.
- Successful queue submission returns `status: queued`
- Public `run-dir` does not launch ORCA directly for new work
- Background execution is managed by externally supervised queue workers
- The ORCA worker starts queue children by queue identity
  (`--queue-root/--queue-id`), then the child resolves the current queue entry
  and runs through the shared `core.queue.engine.worker_execution.EngineWorkerExecutionSpec`
  lifecycle
- ORCA state, report, and notification behavior remain ORCA-domain
  behavior; parent queue finalization still records the terminal queue result
  after the child exits
- On WSL, the recommended supervisor is `systemd`

Operational consequences:

- Closing the submission terminal after `status: queued` is safe
- If the worker is down, the job remains in `queue.json` until the worker returns
- Worker stop/start is managed by `systemctl`

## 3) Directory Structure

```text
<repo_root>
  config/orca_auto.yaml
  src/
    orca_auto/
      core/               # Shared chemistry-platform infrastructure
      flow/               # Workflow orchestration package
        engines/
          xtb/            # Internal xTB workflow-stage engine
          crest/          # Internal CREST workflow-stage engine
      orca/               # Canonical ORCA implementation
        commands/
        runtime/
        state.py
        ...
  systemd/
    orca_auto-runtime@.target
    orca_auto-engine-workers@.target
    orca_auto-queue-worker@.service
    orca_auto-workflow-worker@.service
  scripts/*.sh / *.py
  tests/
    integration/
    flow/
    ...
```

## 4) Required Environment

- Access to an ORCA Linux binary path such as `/opt/orca/orca`
- ORCA runtime dependencies such as OpenMPI and BLAS/LAPACK
- For the supported platform, Python version, and path requirements, see the
  [Runtime Contract](PUBLIC_CONTRACTS.md#runtime-contract)

## 5) Installation and Initial Setup

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
```

`bootstrap_wsl.sh`:

- Prepares `.venv`
- Installs Python dependencies and the repository itself into `.venv`
- Seeds `config/orca_auto.yaml` if missing

This reference standardizes on `orca_auto ...` for public commands; the
supported command list and the default config discovery order are specified in
the [Public CLI Contract](PUBLIC_CONTRACTS.md#public-cli-contract) and the
[Config Contract](PUBLIC_CONTRACTS.md#config-contract).
Activate `.venv` first, or call `.venv/bin/orca_auto ...` directly.
Add `--config <path>` only when you want to override default config discovery.

## 6) Configuration File

Configuration file: `<project_root>/config/orca_auto.yaml`

The config discovery order is specified in the
[Config Contract](PUBLIC_CONTRACTS.md#config-contract).

```yaml
runs_root: "/path/to/orca_runs"

resources:
  max_cores_per_task: 8
  max_memory_gb_per_task: 32

scheduler:
  max_active_simulations: 4
  admission_root: "/path/to/chem_admission"

workflow:
  paths:
    xtb_executable: "/path/to/xtb"
    crest_executable: "/path/to/crest"

messenger:
  provider: discord
  discord:
    bot_token: ""
    default_channel_id: "123456789012345678"
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5

orca:
  runtime:
    scratch_root: "/dev/shm/orca_auto"
    scratch_min_free_gb: 8
  paths:
    orca_executable: "/path/to/orca/orca"
```

Field descriptions:

- `runs_root`: The single runs root shared by standalone ORCA jobs and workflow
  workspaces; completed runs stay here under their submitted directory names
- `orca.runtime.scratch_root`: optional shared dedicated directory below
  `/dev/shm` for private per-attempt ORCA and workflow
  xTB/CREST working directories
- `orca.runtime.scratch_min_free_gb`: positive tmpfs free-space launch guard;
  defaults to `8` when RAM scratch is enabled
- `scheduler.max_active_simulations`: Shared total active-run cap across ORCA, internal xTB stages, and internal CREST stages
- `scheduler.admission_root`: Shared admission root for machine-wide slot
  coordination; defaults to `<runs_root>/.admission`. Scheduler controls belong
  at the top level; engine-scoped values may not diverge because that would
  split the shared admission pool.
- `workflow.paths.xtb_executable`: xTB executable path used by workflow-managed internal stages
- `workflow.paths.crest_executable`: CREST executable path used by workflow-managed internal stages
- `messenger.discord.bot_token`: Discord bot credential; after surrounding
  whitespace is trimmed, a non-empty token must use printable ASCII characters
  without whitespace
- Internal xTB/CREST runtimes are scoped to each workflow
- Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and outputs are stored only under the generation workspace `<runs_root>/<scaffold>/<workflow_id>/<NN_engine>` (`01_crest`, `02_xtb`, `03_orca`)
- `orca.paths.orca_executable`: ORCA executable path

Notes:

- Config parsing and validation behavior — YAML document and duplicate-key
  rules, messenger identity and delivery clamping, tmpfs scratch closure
  mechanics, the `MemAvailable` launch guard, unknown-key fail-closed
  validation, and the Windows-path/executable-path rejection rules — is
  specified in the [Config Contract](PUBLIC_CONTRACTS.md#config-contract).
- When RAM scratch is enabled, keep the shared scheduler cap conservative and
  size `/dev/shm` for the largest accepted calculation: the conservative
  launch-time memory snapshot reduces swap pressure but cannot prevent later
  system activity or tmpfs swap, and `scratch_min_free_gb` is a launch guard,
  not a directory quota.
- If `workflow.paths.xtb_executable` or `workflow.paths.crest_executable` is
  left blank, submission resolves it from PATH and binds that executable
  identity to the queued generation.

## 7) CLI Usage

All public queue, submission, scaffold, and cleanup commands should be
documented through `orca_auto ...`. The supported public command surface is
listed in the [Public CLI Contract](PUBLIC_CONTRACTS.md#public-cli-contract);
general xTB and CREST work remains workflow-internal.

### 7.1 `init`

```bash
orca_auto init
```

Behavior:

- `init` interactively creates or updates the shared `orca_auto.yaml`
- ORCA, internal xTB, internal CREST, and workflow settings are collected in one place

### 7.2 `run-dir`

```bash
cd <repo_root>
orca_auto run-dir '/absolute/path/to/orca_runs/Int1_DMSO'
orca_auto run-dir '/absolute/path/to/orca_runs/reaction_case'
```

Successful ORCA submission example:

```text
status: queued
job_dir: /absolute/path/to/orca_runs/Int1_DMSO
queue_id: q_20260403_151220_ab12cd
priority: 10
worker: active
worker_pid: 12345
```

Shared behavior:

- Inspects the target directory and routes it to ORCA or workflow handling automatically
- Validates the target directory against the detected run type and configured roots
- Rejects duplicate active queue entries for the same directory
- Writes the queue entry durably before returning
- Leaves actual execution to a worker

ORCA-specific notes:

- Visible-generation naming, the reserved `YYYYMMDD-HHMMSS-<8-hex>` name
  shape, resubmission/`--force` barriers, dependency basename-collision and
  reserved-name rules, ambiguous duplicate resource/orbital-input rejection,
  explicit snapshot-bound `MOInp` for every `MORead`, and the rejection of
  non-snapshot-bound external include/program hooks are specified in the
  [Queue And Activity Contract](PUBLIC_CONTRACTS.md#queue-and-activity-contract).
- Queue workers execute by queue id rather than passing a direct
  `reaction_dir` command line. The queue entry still stores `reaction_dir`, and
  downstream ORCA/workflow contracts should keep using that field.
- Standalone ORCA resource metadata comes from the selected input's `%pal`
  and `%maxcore` directives, with config defaults applied only when those
  directives are missing. The defaults are resolved for the run and recorded in
  the private execution snapshot; the input file you selected is never modified.
  The shared `--max-cores` and `--max-memory-gb`
  flags do not override standalone ORCA input directives. Resource readers use
  the largest active value before normalization so a later duplicate cannot
  hide a larger request.
- Resumed worker-shutdown inputs add `MORead` plus `%moinp`
  when the source input has a matching non-empty `.gbw` checkpoint whose
  leading bytes are not all zero (a checkpoint torn by a crash reads back as
  zeros and is skipped, as it is for crash recovery). Top-level
  and `%scf` orbital-input forms are interpreted together and never duplicated.
  Recovery verifies the originally snapshotted executable and can use a valid
  frozen runtime-geometry seed without reopening its deleted source file. Such
  a seed must preserve atom labels/order and contain exactly three finite
  coordinates per declared atom, with no trailing rows.
  Resumed inputs are written as `*.resume.inp` so the original user input is not
  mutated.

Workflow notes:

- Workflow name/ID restrictions (no `(` or `)`; never rename an existing
  workflow directory) are specified in the
  [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract).
- `run-dir` materializes a workflow only when `flow.yaml` is present in the target directory
- Each run creates a timestamped generation directory (`YYYYMMDD-HHMMSS-<8hex>`)
  inside the submitted scaffold — the same layout standalone ORCA executions
  use — and that generation name is the workflow id shown by `queue list` and
  accepted by `queue cancel`. Re-running `run-dir` on the same scaffold starts
  a fresh generation next to the previous ones. The scaffold must sit directly
  under the configured `runs_root`.
- If the target already contains `workflow.json` (a generation directory), `run-dir` restarts failed/cancelled stages in that existing workspace instead of creating a new workflow
- Once that workspace has published its terminal observation (`machine.json`),
  such a restart can no longer produce a new `workflow_report.html`,
  `workflow_si.md` or `machine.json`: the observation pins their bytes and no
  later advance regenerates them, so the reopened stages can succeed while the
  published report and SI still describe the earlier run. The restart says so
  on stdout. The supported route to a fresh record is to run `run-dir` on the
  scaffold directory, which starts a new generation — accepting that its
  CREST/xTB stages re-run.
- If a directory mixes raw ORCA `*.inp` files with scaffold-style filenames but does not include `flow.yaml`, `run-dir` prefers ORCA direct submission
- reaction-path and conformer workflows create and submit xTB/CREST stages internally
- `reaction_ts_search` orders the selected reactant × product CREST pairs
  deterministically by rank gap, so a capped fallback samples both endpoint
  ensembles instead of exhausting the first reactant. It expands at most
  `max_xtb_stages` pairs into xTB child jobs, waits for that xTB phase to reach
  terminal states, then submits at
  most `max_orca_stages` total ORCA OptTS candidates, including stages already
  attempted before a restart. Candidates omitted by either cap are never queued.
- `conformer_screening` starts with one CREST child job and then hands off up to 20 retained conformers to ORCA child jobs in the next workflow cycle. The scaffold shortcut is `orca_auto scaffold conformer_search <path>`.
- `scan_ts_search` starts with an ORCA relaxed scan built from `orca.route_line`
  plus the required `scan_coordinate` manifest key (ORCA scan syntax, 0-based
  atom indices). The coordinate is one exact `B`/`A`/`D` instruction with the
  matching arity, distinct in-bounds atoms, finite unequal endpoints, and at
  least two points. When the scan completes, one OptTS+Freq child job is chained
  per interior maximum of the combined profile with prominence above
  `barrier_threshold_kcal` (default 0.5; endpoints excluded; capped by
  `max_orca_stages`; route from `orca_optts_route_line`), and the workflow
  report ranks the candidates. A barrierless profile first gets up to
  `max_scan_extensions` (default 1) extension scan stages continuing past the
  previous endpoint (max(6, 20% of the range) extra points each) before the
  workflow fails with `scan_profile_no_barrier`. When every forward candidate
  finishes without verifying a TS, a reverse scan stage walks the full range
  back from the forward endpoint geometry and its interior maxima fan out as a
  second candidate batch; only when those are exhausted too does the workflow
  fail with `ts_candidates_exhausted`. Being ORCA-only, its stages live
  directly under the generation workspace as workflow-ordered directories
  (`01_scan`, then `02_scan_maximum`/`02_scan_extension`, ... in creation
  order) with no `03_orca` engine root and no `inputs/` copy of the source
  geometry. The scaffold shortcut is `orca_auto scaffold scan_ts <path>`.
- Workflow ORCA routes are role-checked at creation, restart, materialization,
  pre-submission selection, and completed-result acceptance. Reaction TS routes and
  `orca_optts_route_line` require the exact active, unquoted `OptTS` token plus
  `Freq`, `NumFreq`, or `AnFreq`, and reject `ScanTS`/`NEB-TS`; conformer
  and relaxed-scan routes require a non-TS optimization, and relaxed scans also
  require exactly one closed `%geom Scan` coordinate block whose atom indices
  fit the selected geometry. The same strict scan contract is reused during
  dynamic extension and completed-result acceptance. A route must be a string of
  route lines; quoted tokens, marker-prefixed payload tokens, and active
  non-route input are rejected rather than rendered. Tokens
  inside a closed `# ... #` inline comment and after an unmatched `#` marker are
  ignored. Submission requires equal durable `reaction_dir`/`selected_inp`
  copies, resolves the same actual input as the direct submitter, and validates
  the final rewritten bytes at the snapshot boundary before binding those same
  bytes. After a primary ORCA stage completes, restart cannot change its route,
  charge, or multiplicity; after a CREST or xTB stage completes, restart cannot
  change the workflow charge or multiplicity either, because those conformers
  were screened on the electronic state their job manifest carried. An
  accepted electronic-state change is recorded in the restart summary, the
  restart journal and the command response (a `previous` value is null when
  the workflow never recorded it). Reports omit energy comparisons across
  missing or mixed
  route, non-resource active input directives, electronic-state, ORCA-version,
  or identity-bound non-geometry dependency content provenance, or when the
  selected geometries do not share one ordered atom-label sequence. Geometry
  coordinates remain candidate-specific, and private dependency pathnames are
  canonicalized away. HTML, SI, and interaction
  RMSD representative selection share this scientific identity; `%pal`,
  `%maxcore`, and route `PALn` are resource-only. The HTML report preserves
  stage order and omits numeric rank in that case. Interaction-role metadata
  excludes only a structurally valid ORCA single-point child, so it cannot hide
  a primary stage. A mismatch fails closed instead of accepting scientifically
  incompatible output.
- Every workflow advance rewrites `workflow_report.html` in the workflow
  workspace: a self-contained visual summary with the stage chain, the
  CREST → (xTB) → ORCA funnel, and a ranked ORCA results table (relative
  energies, imaginary-frequency counts, links to per-job `job_report.html`).
  Failed workflows also show a top-level explanation and a failed-stage table
  sourced from `workflow_error`, engine job reports, and recognized CREST
  safety-termination diagnostics.
- Workflows with ORCA stages also rewrite `workflow_si.md`
  on every advance: a paper-ready Supporting Information assembly with a
  computational-details paragraph generated from the routes and ORCA versions
  that actually ran, the CREST → xTB → ORCA funnel provenance, a relative
  energy table (ΔE/ΔG), and each completed structure's SI block. A single-point
  stage pairs only through a globally unique 1:1 identical-geometry match with
  the same charge/multiplicity. The relative table and populations then use one
  shared energy convention: SP E requires complete coverage at one exact
  executed provenance, and composite G = E(SP) + [G − E(el)](opt level)
  additionally requires complete corrections at one exact
  optimization/frequency provenance.
  Exact provenance includes the executed method, basis, solvation, ORCA version,
  route, charge, and multiplicity. Missing optimization/frequency route or
  ORCA-version evidence omits populations; incomplete optional SP provenance
  disables that refinement. Parsed charge/multiplicity must also match the
  selected input. Partial or mixed refinements fall back consistently to the
  applicable optimization-level value and produce a note.
- For `conformer_screening`, the Boltzmann section is populated only after the
  workflow reaches terminal `completed` state with every ORCA ensemble member
  usable. Every route-classified minimum must have converged optimization,
  a complete 3N vibrational spectrum with `Nimag = 0`, finite electronic and
  Gibbs energies, a finite positive thermochemistry temperature, and exact
  optimization/frequency provenance shared within its
  `formula|charge|multiplicity` group. One unfinished, failed,
  or unusable member omits the entire set; a partial ensemble is never
  renormalized to 100%.
- Populations are normalized independently within each
  `formula|charge|multiplicity` group. This is a stoichiometric proxy rather than
  a connectivity identity: each retained minimum has statistical weight one,
  with no symmetry/degeneracy correction. Optional post-DFT deduplication validates
  the full ensemble first, and its duplicate count is not a statistical weight.
  The population temperature and the optional `boltzmann_temperature_k` pin
  (admission validation, durable-request persistence, and the 0.01 K agreement
  rule) are specified in the
  [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract).
- `conformer_screening` accepts an optional `rmsd_dedup:` block that groups
  optimized minima and keeps the lowest-energy representative; its eligibility,
  threshold, provenance, and heuristic-risk rules are specified in the
  [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract).
- `conformer_screening` accepts an optional `interaction_energy:` block that
  reports ΔE_int = E(complex) − Σ E(fragment_i): the complex and each fragment
  run a fresh single point on the complex-optimized geometry, and
  `sp_route_line` defaults to `! r2scan-3c TightSCF`. The fragment
  partition/spin validation, fan-out eligibility, result-resolution and restart
  rules, and SI publication checkpoint/retry/re-arm behavior are specified in
  the [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract).
- Set `runs_root` in `orca_auto.yaml` (or `workflow_root`/`workflow.root` in
  `flow.yaml`) before submitting workflow directories.
- Public workflow `run-dir` reads workflow type and XYZ inputs from `flow.yaml`
  or the standard filenames written by `scaffold`; it accepts only
  `--max-cores` and `--max-memory-gb` as workflow resource overrides.
- The `flow.yaml`/engine-manifest YAML loader limits (file size, alias, node,
  and nesting bounds) are specified in the
  [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract).
- Manifest-controlled input paths (`reactant_xyz`, `product_xyz`, `input_xyz`,
  and `xtb.xcontrol_file`) default to the submitted workflow directory trust
  boundary: relative paths are resolved from `workflow_dir`, absolute paths or
  `..` escapes must still resolve inside that directory. To intentionally reuse
  trusted local files outside the workflow directory, set
  `allow_external_inputs: true` in `flow.yaml`; CLI-supplied input path overrides
  are treated as an explicit operator action and may point outside. Use
  Linux/WSL POSIX paths, not Windows drive paths such as `C:\\...`.
- xTB `xcontrol` target names are separate from `xcontrol_file` source paths:
  `xcontrol_file` names the source file to copy, while `xcontrol` must be a
  plain file name materialized inside the xTB job directory.
- The `crest:` and `xtb:` engine mappings are strict at engine submission:
  unknown option names are rejected instead of ignored. xTB always emits
  explicit `--chrg` and `--uhf` values plus `--norestart`, so a restart file
  cannot silently alter a new generation.
- CREST topology overrides can be placed under `crest:` in `flow.yaml`, including `gfn: ff`, `no_preopt: true`, `noreftopo: true`, `notopo: true`, and `nocbonds: true`
- The electronic-state authority of workflow-level
  `orca.charge`/`orca.multiplicity`, the element/electron-count/UHF-parity
  validation, and the 10,000-atom (1,000 for Hessian/frequency inputs)
  admission caps are specified in the
  [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract).
- xTB exit code 0 alone does not complete an opt, sp, or hess job: the run must
  also yield a valid artifact. An optimization without xTB's `.xtboptok` success
  marker, an SP without a finite energy, and a Hessian without a valid matrix
  are failed with `xtb_opt_no_valid_geometry`, `xtb_sp_no_finite_energy`, or
  `xtb_hess_invalid_hessian` respectively.
- CREST exit code 0 is accepted only when a retained output contains at least
  one strictly valid, finite XYZ frame. Every valid named retained ensemble is
  preserved: geometries found only in later rotamer outputs remain candidates,
  while cross-file overlaps do not duplicate downstream candidates. Non-finite xTB energies and XYZ
  coordinates are unusable and are never materialized for ORCA.
- CREST receives an absolute immutable input-snapshot path and an explicitly
  bound xTB executable (`-xnam`). orca_auto does not pass `--scratch`, because
  CREST 3.0.2's native scratch implementation invokes an unsafe shell path.
  The `gfn2//gfnff` composite emits CREST's required `--legacy` CLI flag;
  charge and UHF are always explicit, including neutral singlet values.
- `solvent_model` must be `gbsa` or `alpb` and must be paired with `solvent`.
  Both xTB and CREST accept only these canonical solvent tokens:
  `acetone`, `acetonitrile`, `aniline`, `benzene`, `benzaldehyde`, `ch2cl2`,
  `chcl3`, `chloroform`, `cs2`, `dmf`, `dmso`, `dioxane`,
  `dichlormethane`, `ether`, `ethanol`, `ethylacetate`, `furane`,
  `hexadecane`, `hexane`, `h2o`, `methanol`, `nitromethane`, `nhexan`,
  `n-hexan`, `nhexane`, `n-hexane`, `octanol`, `phenol`, `thf`, `toluene`,
  `water`, and `woctanol`. Free-form or multi-token values and shell syntax are
  rejected rather than forwarded.
- CREST conformational-search knobs are accepted under `crest:` with flag
  semantics implemented against CREST 3.0.2. `mdlen` (MD length in ps) and
  `wscal` are finite positive reals rendered without exponent notation to
  at most six decimal places; values below `0.000001` are rejected.
  `tstep` and `mddump` each require an explicit MD length. Without an expert
  override, `tstep` is at most 5.0 fs for GFN-xTB, 1.5 fs for GFN-FF, and 2.0 fs
  for `gfn2//gfnff`; `shake: 1` tightens that cap to 2.0 fs. Setting
  `allow_high_tstep: true` permits the native 0.001–2500 fs range but does not
  bypass the work budget. `mddump` is an integer in `1..2147483647`.
  The default aggregate `max_md_steps` budgets, the GFN-FF/`gfn2//gfnff`
  requirement for a bounded `mdlen` or an explicit higher budget plus
  `allow_high_cost_md: true`, and the absolute 50,000,000,000 atom-step ceiling
  are specified in the
  [Workflow Contract](PUBLIC_CONTRACTS.md#workflow-contract). The budget counts
  CREST's estimated trajectory/restart/rotamer multiplicity: base 6 for `nci`
  or a quick mode and 14 otherwise, multiplied by 1 restart for `mquick` or 5
  otherwise, then by 1 for `nci`, a quick mode, or `norotmd` and 2 otherwise.
  Without `mdlen`, CREST's automatic 2.5–500 ps range is admitted at its 500 ps
  worst case; this admits standard GFN-xTB defaults. A larger step bound, up to
  the native integer limit, requires `allow_high_cost_md: true`. The default
  `max_dump_frames` is 100,000 estimated aggregate frames (aggregate simulated
  time divided by `mddump`); a larger bound
  requires `allow_high_volume_md: true`. `shake` is `0`, `1`, or `2`. The exact
  `norotmd`, `cross`, and
  `nocross` keys accept YAML booleans or canonical boolean forms
  (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`), and `cross`/`nocross` are
  mutually exclusive. `cross: true` keeps CREST 3.0.2's default GC crossing
  without emitting its broken redundant `--cross` flag; `nocross: true` emits
  `--nocross`. Malformed values fail the job closed rather than reaching CREST.
- xTB ranking admits at most 100 candidate evaluations by default. Local
  reaction-workflow manifests may set `xtb.max_ranking_evaluations` up to the native candidate
  cap of 1,000; values above 100 also require
  `xtb.allow_high_cost_ranking: true`.
- `scaffold ts_search` and `scaffold conformer_search` write `flow.yaml` with `crest_mode: standard` by default; change it to `nci` when needed

There is no public direct-execution mode for new work. `run-dir` is the durable submission path.

#### Immutable execution, provenance, and upgrade boundary

- xTB, CREST, and ORCA bind selected inputs at submission. Each source file is
  limited to 64 MiB. xTB and ORCA additionally cap one generation's aggregate
  bound input at 256 MiB; ORCA accepts at most 128 file-reference directives.
  CREST has the per-file limit but no separate aggregate limit. A downstream
  output XYZ materialization is bounded at 512 MiB.
- The visible-generation layout and provenance recording, snapshot namespaces,
  the upgrade drain requirement for the ORCA visible-generation format, and
  the xTB/CREST terminal-identity and state-only metadata rules are specified
  in the
  [Queue And Activity Contract](PUBLIC_CONTRACTS.md#queue-and-activity-contract);
  the engine trust and isolation boundary (captured environment, qualified
  distributions, same-UID processes) is specified in the
  [Runtime Contract](PUBLIC_CONTRACTS.md#runtime-contract).
- Snapshot and generation trees are retained for queue replay, recovery,
  reconciliation, and audit. There is no standalone snapshot-GC command. Never
  edit or delete a generation used by a pending, running, retrying,
  cancel-pending, or repairable terminal row. Reclaim it only with an
  intentionally retired job/workflow after confirming that no queue or recovery
  record still references it.
- xTB/CREST additionally run with a job-local clean `HOME`/`XDG_CONFIG_HOME`,
  and their internal state-only `job_state.json` contains status,
  command/provenance, resource use, retained-output identities, and
  engine-specific result fields.

### 7.3 `queue cancel`

```bash
orca_auto queue cancel q_20260403_151220_ab12cd
orca_auto queue cancel /absolute/path/to/orca_runs/Int1_DMSO
```

`queue cancel` cancels a whole workflow when given a workflow id; the full set
of accepted targets and aliases is specified in the
[Public CLI Contract](PUBLIC_CONTRACTS.md#public-cli-contract).

### 7.4 `queue list`

```bash
orca_auto queue list
orca_auto queue list --engine orca
orca_auto queue list --status pending
orca_auto queue list --engine xtb
orca_auto queue list --limit 20
```

`queue list` shows workflow and engine activity in one view, but workflow child simulations
are rendered underneath their parent workflow with indentation. The text view prints a table
with `Status`, `Name`, `Detail`, `ID`, and `Elapsed` columns, where the detail field surfaces
workflow or job intent such as `ts_search(nci)`, `IRC`, or `NEB`. CREST, xTB,
and ORCA child jobs are all expanded beneath workflow parents in the default
combined text view, so every queued workflow simulation and its current status
are visible together. The `--engine ... --kind job` filters and `--json` expose
the same jobs, and a non-negative `--limit N` caps the listing to the newest
`N` activities after filtering (`0` leaves it uncapped); in the text view a
listed child job is shown beneath its parent workflow row, and that context row
does not count toward `N` (`--json` returns exactly `N`). Top-level ORCA jobs
remain top-level entries. The
`active_simulations` line counts only the currently running
simulations that consume the shared `scheduler.max_active_simulations` slots.

On an interactive terminal the text view is styled: a summary band replaces the plain
`active_simulations:` line with per-status counts (running, queued, done, failed,
cancelled), workflow children are drawn with box-drawing tree connectors (`├─`/`└─`)
instead of plain indentation, and each row carries a status-colored left rail.
These affordances are terminal-only: piped text keeps the plain table layout — including the
`active_simulations:` line and plain indentation — while `--json` remains machine-readable
JSON. Piped text is ANSI-free unless
`FORCE_COLOR` is explicitly set. On a real terminal, `NO_COLOR` and `--no-color` keep the
released plain table.

`queue list clear` prunes completed, failed, and cancelled entries from the unified list.

### 7.5 CLI Output and Global Flags

- Table output is colorized by status when stdout is a terminal. Color is disabled
  automatically when piped or when `NO_COLOR` is set, and can be forced off with
  `--no-color` (e.g. `orca_auto --no-color queue list`). The `queue cancel`, `run-dir`,
  and `service status` outputs colorize status fields the same way.
- `orca_auto --version` prints the installed version, and running `orca_auto` with no
  command prints help. Errors and recovery hints are written to stderr.
- `orca_auto service status --json` emits machine-readable output for scripting.
- `orca_auto service status` also gates the declared version of the interpreter
  running it. An editable install freezes its metadata at install time, so a
  checkout that has moved on keeps reporting the version it was installed at;
  the command reports that mismatch as `version_drift`, names the interpreter it
  inspected, prints a `pip install -e .` hint on stderr, and exits non-zero.
  `orca_auto --version` keeps reporting the installed version alone, so use
  `service status` rather than reading the version back.
- `orca_auto service status` also gates the age of each running worker against
  a per-worker snapshot of the latest matching HEAD-reflog update in the
  checkout recorded from that worker's actual imported module (every entry
  naming the current commit counts, a same-commit checkout or reset included:
  a forced checkout restores files under the same reflog subject as a no-op
  one, so the verdict errs toward stale). The provenance
  is bound to the process PID and start ticks; it does not use cwd, commit
  timestamp, or the command's own checkout. An imported package tree with
  uncommitted source changes is undetermined. Stale or undetermined git-backed workers are reported
  in `worker_staleness` with per-worker evidence, print a `service restart` hint
  on stderr, and make the command exit non-zero. Non-git workers are not judged;
  in a mixed deployment they appear as `uncompared`. Restart workers in an idle
  window after every deploy that touches code they import.

### 7.6 Long-Running Services

Long-running worker processes are managed through `systemd`.
The public `systemd install` and `service` commands operate on those units rather
than launching unmanaged worker processes.

Behavior:

- Target/service ownership — which target starts which unit, and the opt-in
  workflow worker — is specified in the
  [Systemd Contract](PUBLIC_CONTRACTS.md#systemd-contract)
- ORCA, xTB, and CREST share the same admission cap. ORCA reserves a slot in
  the parent worker, attaches queue identity metadata after the child starts,
  and lets the ORCA child activate/release that reservation during execution.
- Workflow notification alerts keep per-job ORCA messages, but summarize internal CREST and reaction-path xTB child phases in one message each after those phases finish

Two environment variables gate workflow journal notifications in the worker's
environment (set them in the systemd unit or shell that runs the workflow
worker; they do not affect standalone ORCA queue notifications):

- `ORCA_AUTO_FLOW_NOTIFY_EVENT_TYPES`: comma-separated event types to deliver.
  Unset or blank means the default set `workflow_status_changed`,
  `workflow_advance_failed`, `worker_started`, `worker_stopped`,
  `worker_interrupted`, `worker_lock_error`. Naming event types replaces the
  default set rather than adding to it.
- `ORCA_AUTO_FLOW_NOTIFY_DISABLED`: `1`, `true`, `yes`, or `on` disables all
  workflow journal notifications regardless of the event-type list.

Each variable is read by the process that appends the journal event. Every
event in the default set is emitted by the workflow worker; if you opt into
`workflow_restarted`, note it is emitted by the `run-dir` CLI process, so the
variable must be set in that shell too.

## 8) WSL systemd Setup

WSL should have `systemd` enabled:

```ini
[boot]
systemd=true
```

If you change `/etc/wsl.conf`, restart WSL from Windows:

```powershell
wsl --shutdown
```

This repository includes service assets under `systemd/`:

- [`systemd/orca_auto-runtime@.target`](../systemd/orca_auto-runtime@.target)
- [`systemd/orca_auto-engine-workers@.target`](../systemd/orca_auto-engine-workers@.target)
- [`systemd/orca_auto-queue-worker@.service`](../systemd/orca_auto-queue-worker@.service)
- [`systemd/orca_auto-workflow-worker@.service`](../systemd/orca_auto-workflow-worker@.service)

Recommended always-on runtime install flow:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
```

Assumptions of the unified runtime templates:

- Repository path: `/home/<user>/orca_auto`
- Config path: `/home/<user>/orca_auto/config/orca_auto.yaml`

The installer renders these paths into every unit; pass explicit `--repo` and
`--config` values when the defaults differ. `--worker-only` selects the
engine-worker target as the boot target instead of the full runtime target;
literal `%` path characters are escaped, while quotes, backslashes, and dollar
signs are rejected before unit files are written.
Templates always come from the required `<repo>/systemd` directory, even when
the installer command itself came from a wheel. The default nested
`<runs_root>/.admission` need not exist yet because the rendered writable
`runs_root` parent lets the worker create it. A separately configured
`scheduler.admission_root` must be created as a directory with suitable
service-user ownership before installation; a missing explicit root fails the
install before unit files or systemd state are changed.
`service status` reports such an install as `worker-only`. The runtime target
currently pulls in only the engine-worker target, so both modes start the same
unit set today — the flag fixes the boot selection, and a worker-only install
stays worker-only if the runtime target later grows.

The default engine-worker target starts the ORCA
service. A configured workflow root does not implicitly start the workflow or
its internal-engine workers. Start
`orca_auto-workflow-worker@<user>.service` explicitly when workflow supervision
and its internal CREST/xTB workers are needed. The shared
`scheduler.max_active_simulations` setting still limits the combined number of
active simulations across ORCA and workflow-managed internal
engine stages.

Workflow supervision belongs to the opt-in
`orca_auto-workflow-worker@.service` unit.

## 9) Completion Determination Rules

The mode is determined from the input route line (`! ...`).

- TS mode: Contains `OptTS` or `NEB-TS`
- Opt mode: Everything else

TS mode completion:

- `****ORCA TERMINATED NORMALLY****` exists
- Exactly 1 imaginary frequency is present in the frequency section printed
  after the last final single point energy (a section printed before a later
  final energy belongs to an earlier geometry and verifies nothing)
- If the route contains `IRC`, the IRC marker is also required

Opt mode completion:

- `****ORCA TERMINATED NORMALLY****` exists

## 10) Failure Classification and Automatic Recovery

Representative statuses are the ORCA analyzer statuses listed in the
[ORCA Job Artifact Contract](PUBLIC_CONTRACTS.md#orca-job-artifact-contract)
(for example, `error_geometry` covers an ORCA zero-distance geometry
collapse).

Execution policy:

- Every ORCA calculation runs once. Failure preserves the analyzer reason.
- Direct `ScanTS` is unsupported and rejected before generation/queue publication.
- Plain relaxed scans and the `scan_ts_search` workflow remain supported.
- Original charge, multiplicity, and input files are never changed automatically.
- Interrupted worker/host recovery may create a verified `*.resume.inp` checkpoint input.
- Remove `orca.runtime.default_max_retries` from configuration before upgrading;
  even zero is rejected. Older execution snapshots are not run or converted.
- Existing generations remain read-only history; terminal replay/notification
  bookkeeping for them is written only at the root, in the current format.

Worker restarts and crash recovery (documented limitation):

- A running ORCA job that is interrupted by a worker stop or restart is
  requeued and resumed through the same crash-recovery path as a genuine crash.
  Each such resume consumes one of the three recovery rebinds of that
  submission, and the resume re-validates the submitted source input and the
  configured resource request against the queued row: a source `.inp` edited
  after submission fails the row instead of resuming it, and so does a
  configuration change that alters the recomputed resource request (an input
  that pins its own `%pal`/`%maxcore` is not affected by
  `resources.max_cores_per_task`). Restart workers only in an idle window (no
  running simulation), and do not edit a submitted input or the resource
  configuration while its job is queued or running.

## 11) Output Files

The submitted ORCA job directory keeps the user-authored inputs, `run.lock`,
and one visible execution generation per submission. Each generation holds
that run's state and reports:

- `job_state.json` (internal state and recovery)
- `machine.json` (the only public machine metadata)
- `job_report.html` (Opt, OptTS, NEB-TS, IRC, and relaxed-scan jobs):
  self-contained visual report assembled from common page chrome plus
  calculation components. Depending on the parsed route/output it may include
  the scan energy profile (plain relaxed scans, i.e. `Opt` routes
  with a `%geom Scan` block), CI-NEB path profile plus TS refinement trace
  (NEB-TS), IRC path profile with combined OptTS/Freq sections when present, or
  optimization convergence trace (Opt/OptTS), the attempt history, and a
  vibrational summary (imaginary modes, dominant atom displacements, and — for
  scans — alignment with the scanned coordinate)
- `si_block.md`: for completed jobs ending on a stationary point (single points
  included, relaxed scans excluded), a copy-paste Supporting Information block
  with the route line and ORCA version, E(el)/ZPE/H/G and the G−E(el)
  correction, Nimag with an imaginary-mode summary, the final coordinates, and
  `⚠` lint lines for reviewer-visible problems; for IRC routes, a
  summary-only validation block without coordinates. No block is written when
  the output yields no trustworthy final energy or geometry, which includes a
  final energy line annotated as not fully converged

Each submission places its bound inputs and raw outputs in one visible direct
child, for example:

```text
TS8(NEB-TS)/
├── nebts.inp
├── input.xyz
├── output.xyz
├── guessTS.xyz
├── run.lock
└── 20260714-224054-959479f2/
    ├── nebts.inp
    ├── input.xyz
    ├── output.xyz
    ├── guessTS.xyz
    ├── nebts.out
    ├── nebts.gbw
    ├── nebts.NEB.log
    ├── job_state.json
    ├── machine.json
    └── job_report.html
```

This example is not an exhaustive file listing. The internal synchronization
file `.job_state.mutation.lock` may remain at the job root, which also carries
the live `job_state.json` until terminal cleanup removes it. Active engine
PID/PGID ownership is stored in the shared admission record rather than the
generation.

The generation's bound `.inp` has the exact selected source basename, so ORCA
uses the expected output stem rather than adding `.run` or `.bound`. Report
placement and verification rules — reports exist only inside a verified
generation, unbound root reports are ignored, and a run rejected before
generation binding has no report — are specified in the
[ORCA Job Artifact Contract](PUBLIC_CONTRACTS.md#orca-job-artifact-contract).
`run.lock` stays at the job root; the mere
presence of its file is not proof that a process currently owns the lock.

`job_state.json` uses the internal normalized engine artifact schema
(`schema_version` 1). Public `machine.json` uses
`factory/machine-observation` version 1 with a `chemistry/results-bundle`
payload, exact artifact receipts, and no absolute runtime paths. The full
boundary is described in the
[ORCA Job Artifact Contract](PUBLIC_CONTRACTS.md#orca-job-artifact-contract).

Important `engine_payload.attempts[]` fields:

- `index`
- `inp_path`
- `out_path`
- `return_code`
- `analyzer_status`
- `analyzer_reason`
- `markers`
- `patch_actions`
- `command`
- `input_identity`
- `executable_identity`
- `output_identity`
- `started_at`
- `ended_at`

For snapshot-bound jobs, the primary-input and attempt `inp_path` records name
the exact bound input in the visible generation. ORCA execution provenance
records the user-facing source input selected at submission; the
bound/materialized and attempt identity records carry their path, SHA-256, and
byte size.

## 11.1) Downstream Contract Freeze

The ORCA handoff contract exposes the following fields to downstream tooling
such as `orca_auto.flow`.

Queue entry fields currently consumed downstream from `queue.json`:

- `queue_id`
- `task_id`
- `run_id`
- `reaction_dir`
- `status`
- `cancel_requested`
- `resource_request`
- `resource_actual`

Tracked job-location fields currently consumed downstream from
`job_locations.json`:

- `job_id`
- `app_name`
- `job_type`
- `status`
- `original_run_dir`
- `molecule_key`
- `selected_input_xyz`
- `latest_known_path`
- `resource_request`
- `resource_actual`

The normalized ORCA contract exposed downstream should continue to provide at
least these fields:

- `run_id`
- `status`
- `reason`
- `state_status`
- `reaction_dir`
- `latest_known_path`
- `optimized_xyz_path`
- `queue_id`
- `queue_status`
- `cancel_requested`
- `selected_inp`
- `selected_input_xyz`
- `analyzer_status`
- `completed_at`
- `last_out_path`
- `run_state_path`
- `report_json_path`
- `attempt_count`
- `attempts`
- `final_result`
- `resource_request`
- `resource_actual`

Queue worker note:

- `reaction_dir` remains the ORCA queue and downstream contract field.
  Shared core helpers may also understand generic `job_dir` metadata for other
  engines, but ORCA producers should not replace `reaction_dir` with `job_dir`.
- Engine workers run only from queue identity. The unified child entrypoint is
  `python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`.

## 12) Recommended Workflow

1. Ensure the worker service is active under `systemd`
2. Submit with `run-dir`
3. Confirm `status: queued`
4. Close the submission terminal if desired
5. Monitor with `list` or `journalctl`
6. Review `job_report.html`; automation reads `machine.json` after completion
7. To rerun a fully closed standalone ORCA directory, submit it again; a new
   sibling generation is created without `--force`

## 13) Frequently Encountered Issues

1. `Job directory must be under allowed root`
- Cause: the job directory path is outside `runs_root`
- Action: Check `runs_root` in `config/orca_auto.yaml`

2. `Job directory not found`
- Cause: Path string or quoting problem
- Action: Use an absolute path and quote it if needed

3. `State file not found`
- Cause: No job has executed in that directory yet
- Action: Submit with `run-dir` and let the worker pick it up

4. `worker: inactive`
- Cause: The queue submission succeeded, but no worker is running
- Action: Start or restore the worker; the queued job remains durable

5. `error_multiplicity_impossible`
- Cause: Electron count and multiplicity mismatch
- Action: Manually adjust the input, because orca_auto ORCA does not rewrite charge or multiplicity

## 14) Testing

```bash
cd <repo_root>
pytest -q
```

Focused regression commands:

```bash
pytest tests/flow -q
pytest tests/integration -q
pytest tests/test_run_job.py tests/test_queue_worker.py tests/test_orca_queue_publication_repair.py tests/test_orca_terminal_replay.py tests/test_queue_adapter.py -q
pytest tests/core/test_engine_child.py tests/core/test_engine_admission.py -q
```

For package-layout and import guidance, see [DEVELOPMENT.md](DEVELOPMENT.md).
