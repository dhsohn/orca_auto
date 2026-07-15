# orca_auto Detailed Reference

**English** | [한국어](REFERENCE.ko.md)

orca_auto is a queue-first executor for ORCA, standalone xTB-MD, and workflow orchestration. ORCA
uses the shared internal-engine queue lifecycle for worker admission, child
entry execution, terminal side effects, and orphan recovery while preserving
its public ORCA queue contract. Standalone xTB-MD is an independent single-attempt
engine; general xTB and CREST run as internal workflow-stage engines. This reference standardizes the shared public CLI and keeps the deeper
ORCA runtime behavior documented in one place, since ORCA still has the richest
retry, reporting, and monitoring surface.

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
- Retry conservatively on recognized failures without overwriting the original input
- Use matching non-empty ORCA `.gbw` files for retry/resume restart inputs when available
- Record execution status and results alongside the calculation

## 2) Runtime Model

Current intended semantics:

- Public `run-dir` enqueues new work durably
- If an already-completed output is detected, `run-dir` returns completion without relaunching ORCA
- Successful queue submission returns `status: queued`
- Public `run-dir` does not launch ORCA directly for new work
- Background execution is managed by externally supervised queue workers
- The ORCA worker starts queue children by queue identity
  (`--queue-root/--queue-id`), then the child resolves the current queue entry
  and runs through the shared `InternalEngineWorkerAdapter` lifecycle
- ORCA state, retry, report, and notification behavior remain ORCA-domain
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
      xtb_md/             # Standalone xTB-MD engine
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
    orca_auto-queue-worker@.service
    orca_auto-bot@.service
  scripts/*.sh / *.py
  tests/
    integration/
    flow/
    ...
```

## 4) Required Environment

- Linux (WSL2 or native Linux)
- Access to an ORCA Linux binary path such as `/opt/orca/orca`
- ORCA runtime dependencies such as OpenMPI and BLAS/LAPACK
- Python 3.11+
- An input root on a Linux filesystem

## 5) Installation and Initial Setup

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
```

`bootstrap_wsl.sh`:

- Prepares `.venv`
- Installs Python dependencies and the repository itself into `.venv`
- Seeds `config/orca_auto.yaml` if missing

This reference standardizes on `orca_auto ...` for public
commands:

- `queue list`
- `queue cancel`
- `run-dir <path>`
- `init`
- `scaffold <ts_search|conformer_search>`
- `scan-notify`
- `smoke`
Activate `.venv` first, or call `.venv/bin/orca_auto ...` directly.
By default, config is resolved from `ORCA_AUTO_CONFIG`, then `<repo_root>/config/orca_auto.yaml`, then `~/orca_auto/config/orca_auto.yaml`.
Add `--config <path>` only when you want to override default config discovery.

## 6) Configuration File

Configuration file: `<project_root>/config/orca_auto.yaml`

Search order:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

```yaml
runs_root: "/path/to/orca_runs"

resources:
  max_cores_per_task: 8
  max_memory_gb_per_task: 32

scheduler:
  max_active_simulations: 4
  max_active_xtb_md: 1
  admission_root: "/path/to/chem_admission"

workflow:
  paths:
    xtb_executable: "/path/to/xtb"
    crest_executable: "/path/to/crest"

messenger:
  provider: telegram  # telegram | discord
  telegram:
    bot_token: ""
    chat_id: ""
    allowed_user_ids: ["234567890123456789"]
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
  discord:
    bot_token: ""
    channel_ids: ["123456789012345678"]
    default_channel_id: "123456789012345678"
    allowed_user_ids: []

orca:
  runtime:
    default_max_retries: 2
  paths:
    orca_executable: "/path/to/orca/orca"
```

Field descriptions:

- `runs_root`: The single runs root shared by standalone ORCA/xTB-MD jobs and workflow
  workspaces; completed runs stay here under their submitted directory names
- `orca.runtime.default_max_retries`: `0` disables ORCA retries; positive values
  enable the calculation-type retry policy
- `scheduler.max_active_simulations`: Shared total active-run cap across ORCA, standalone xTB-MD, internal xTB stages, and internal CREST stages
- `scheduler.max_active_xtb_md`: Positive standalone xTB-MD subcap; defaults to `1`
- `scheduler.admission_root`: Shared admission root for machine-wide slot
  coordination; defaults to `<runs_root>/.admission`. Scheduler controls belong
  at the top level; engine-scoped values may not diverge because that would
  split the shared admission pool.
- `workflow.paths.xtb_executable`: xTB executable path used by standalone xTB-MD and workflow-managed internal stages
- `workflow.paths.crest_executable`: CREST executable path used by workflow-managed internal stages
- Internal xTB/CREST runtimes are scoped to each workflow
- Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and outputs are stored only under `<runs_root>/<workflow_id>/<NN_engine>` (`01_crest`, `02_xtb`, `03_orca`)
- `orca.paths.orca_executable`: ORCA executable path

Notes:

- `default_max_retries=0` disables ORCA retries; any positive value enables the
  calculation-type retry policy, which caps retries by ORCA route type
- Windows-style paths such as `C:\...`, `C:/...`, and `/mnt/c/...` are not supported in config
- Configured executable paths for ORCA, xTB, and CREST must be absolute Linux
  paths to existing executable files and must not end in `.exe`. If
  `workflow.paths.xtb_executable` or `workflow.paths.crest_executable` is left
  blank, submission resolves it from PATH and binds that executable identity to
  the queued generation.
- Explicit `scheduler`, `resources`, `workflow`, and `workflow.paths` values must
  be mappings. Configured admission roots must be absolute Linux paths, and
  configured scheduler/resource limits must be positive integers. Malformed
  execution controls are rejected instead of being replaced with defaults.

## 7) CLI Usage

All public queue, submission, and scaffold commands should be
documented through `orca_auto ...`.

Public command surface:

- ORCA public commands are exposed through `orca_auto ...`
- Standalone xTB-MD is submitted with `run-dir`; general xTB and CREST work remains workflow-internal

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
orca_auto run-dir '/absolute/path/to/workflow_inputs/reaction_case'
orca_auto run-dir '/absolute/path/to/runs/water_md'
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

- Inspects the target directory and routes it to ORCA, standalone xTB-MD, or workflow handling automatically
- Validates the target directory against the detected run type and configured roots
- Rejects duplicate active queue entries for the same directory
- Writes the queue entry durably before returning
- Leaves actual execution to a worker

ORCA-specific notes:

- Chooses the latest `*.inp` at submission, then creates a visible
  `generation-YYYYMMDD-HHMMSS-<8-hex>/` directly inside the submitted job
  directory. The bound input keeps the selected `.inp` basename, supported
  referenced files keep their source basenames, and raw ORCA outputs are
  written at that same level. Editing a source after successful submission does
  not change the queued calculation.
- A fully closed job directory can be submitted again without `--force`; each
  submission creates a new sibling generation. A pending/running/retrying/
  cancel-pending row or an incomplete terminal replay still blocks another
  generation for that job directory. `--force` does not bypass those safety
  barriers.
- If one input refers to different source paths that have the same basename,
  submission fails closed even when their bytes are identical. Repeated
  references to the same canonical source path remain one dependency and are
  not a basename collision. Sharing only the selected input's stem is allowed
  when ORCA does not produce that dependency name: for example, an SP `h2.inp`
  may reference `h2.xyz`, and both exact basenames are preserved. For Opt,
  OptTS, ScanTS, NEB, and IRC, a same-stem XYZ is normally an ORCA output. The
  one supported collision is a sole main `* xyzfile` geometry: its coordinates
  are inlined into the bound `.inp`, its exact XYZ basename remains visible,
  and ORCA may update that file in place. A same-stem auxiliary NEB Product/TS
  file is still rejected. Frequency-producing routes reserve `<stem>.hess`;
  every route reserves `<stem>.out` and `<stem>.gbw`. Admission also rejects
  the selected `.inp` basename and generation-owned `job_state.json`,
  `job_report.json`, `orca.process.json`, and `.orca.process.lock` as dependency
  basenames. Output-base overrides such as `%base` and NEB restart-GBW basename
  controls are unsupported and fail closed so ORCA cannot write outside the
  generation.
- Queue workers execute by queue id rather than passing a direct
  `reaction_dir` command line. The queue entry still stores `reaction_dir`, and
  downstream ORCA/workflow contracts should keep using that field.
- Standalone ORCA resource metadata comes from the selected input's `%pal`
  and `%maxcore` directives, with config defaults injected only when those
  directives are missing. The shared `--max-cores` and `--max-memory-gb`
  flags do not override standalone ORCA input directives.
- ORCA admission rejects ambiguous duplicate `%pal`/`nprocs`, `%maxcore`,
  `%moinp`, or route `PALn` directives. Resource readers use the largest active
  value before normalization so a later duplicate cannot hide a larger request.
- External ORCA include/program hooks that are not snapshot-bound (for example
  `ExtOpt`/`Prog*`, fragment/QM2 method files, `XTBINPUTSTRING`, and `GCP(FILE)`)
  are unsupported and rejected before local or remote execution.
- Retry inputs and resumed worker-shutdown inputs add `MORead` plus `%moinp`
  when the source input has a matching non-empty `.gbw` checkpoint. Resumed
  inputs are written as `*.resume.inp` so the original user input is not mutated.

Workflow notes:

- Workflow directory names/IDs cannot contain `(` or `)`. Do not rename an
  existing workflow directory; create a new workflow under the new name so the
  persisted ID and artifact paths stay consistent.
- `run-dir` materializes a workflow only when `flow.yaml` is present in the target directory
- If the target already contains `workflow.json` and the workflow failed, `run-dir` restarts failed/cancelled stages in that existing workspace instead of creating a new workflow
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
  atom indices). When the scan completes, one OptTS+Freq child job is chained
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
  fail with `ts_candidates_exhausted`. The scaffold shortcut is
  `orca_auto scaffold scan_ts <path>`.
- Every workflow advance rewrites `workflow_report.html` in the workflow
  workspace: a self-contained visual summary with the stage chain, the
  CREST → (xTB) → ORCA funnel, and a ranked ORCA results table (relative
  energies, imaginary-frequency counts, links to per-job `job_report.html`).
  Failed workflows also show a top-level explanation and a failed-stage table
  sourced from `workflow_error`, engine job reports, and recognized CREST
  safety-termination diagnostics.
- Workflows with ORCA stages also rewrite `workflow_si.md` and `si_data.csv`
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
  the full ensemble first, and its duplicate count is not a statistical weight. The parsed
  thermochemistry temperature is used unless `boltzmann_temperature_k` pins it.
  That optional key must be finite and strictly positive, is persisted in the
  durable request at admission, and must agree with every parsed temperature
  within 0.01 K. It cannot create thermochemistry at a temperature the frequency
  jobs did not use; SI reads the durable request rather than a subsequently
  edited source `flow.yaml`. Missing, non-finite, non-positive, or inconsistent
  data cause populations to be omitted with a note rather than fabricated.
- `si_data.csv` appends `cluster_key`, `rel_E_kcalmol`, `rel_G_kcalmol`,
  `boltzmann_T_K`, and `boltzmann_population` after its existing columns.
  Markdown renders population as a percentage; `boltzmann_population` is the
  corresponding fraction in `[0, 1]`. CSV `rel_E_kcalmol` and
  `rel_G_kcalmol` use the lowest E and G inside that row's population group as
  their local baselines under the shared convention.
- `conformer_screening` accepts an optional `rmsd_dedup:` block that groups
  optimized minima and keeps the lowest-energy representative. Converged
  candidates are eligible when `Nimag` is absent or zero; a known nonzero value
  excludes them. Comparable candidates also need identical selected-atom element
  sequences, formula/electronic state, and exact optimization provenance.
  Both the proper-rotation RMSD and maximum aligned-atom displacement must be
  below `rmsd_threshold_angstrom` (default 0.25), and their effective energies
  must differ by less than `energy_window_kcal` (default 0.1). A complete uniform
  exact-provenance SP refinement supplies that energy; otherwise optimization
  energy is used. A nondegenerate pair whose best unconstrained alignment
  prefers a global reflection is retained separately. This is still a heuristic
  and can merge nearby distinct or local stereochemical minima. All atoms are compared by
  default; `heavy_atoms_only: true` ignores H/D/T and increases that risk. Inspect
  `merged_stage_ids` before treating members as chemically identical. Only when
  enabled, `si_data.csv` appends `rmsd_group`, `degeneracy`, and
  `merged_stage_ids`; population completeness is checked before dedup, and
  `degeneracy` is a workflow duplicate count, not a statistical/symmetry weight.
- `conformer_screening` accepts an optional `interaction_energy:` block that
  reports ΔE_int = E(complex) − Σ E(fragment_i). It requires 2–8 fragments with
  safe single-line labels and integer multiplicities in `[1, 100]`.
  `{atom_indices (0-based), charge, multiplicity, label}` entries must form a
  static, disjoint, exhaustive partition of every input atom. Fragment charges
  must sum to the complex charge, and their spins must be able to couple to the
  complex multiplicity under the generalized angular-momentum coupling manifold.
  Each atom-derived electron count `N_e = ΣZ − charge` must be nonnegative;
  `multiplicity − 1` must be no greater than `N_e` and have the same parity.
  `sp_route_line` (default `! r2scan-3c TightSCF`) must be a pure single-point
  route; job directives for optimization, frequency, gradients, IRC, MD, NEB,
  GOAT, or scans are rejected.
- The complex and each fragment run a fresh single point on the complex-optimized
  geometry. Fan-out uses only valid terminal optimized minima and the RMSD
  representatives. A terminal partial-success ensemble may use its completed,
  converged subset after excluding known saddles. The same eligible set determines
  the representative energy convention, so an unusable/saddle member cannot
  switch the parent. When public dedup reporting is off, the all-atom
  default grouping still bounds fan-out while the SI structure table stays
  undeduplicated. The interaction generation fingerprint includes these RMSD
  settings.
- A resolved result needs exactly one completed current-generation complex SP
  and one completed fragment SP per expected index. Selected input and parsed
  output route/state, executed method/basis/solvation/ORCA version, optimized
  complex geometry, indexed fragment subsets, and energy convention must match.
  Missing, duplicate, running, stale, mixed, wrong-state/wrong-geometry, or
  non-finite data omits ΔE_int rather than using a partial sum.
- `interaction_energy.csv` has one row per complex/fragment pair and these 23
  columns: `parent_stage_id`, `complex_stage_id`, `complex_label`,
  `complex_charge`, `complex_multiplicity`, `complex_formula`, `E_complex_Eh`,
  `method`, `basis_set`, `solvation`, `orca_version`, `route_line`,
  `ghost_counterpoise_applied`, `fragment_label`, `fragment_stage_id`,
  `fragment_atom_indices`, `fragment_formula`, `fragment_charge`,
  `fragment_multiplicity`, `E_fragment_Eh`, `dE_int_Eh`, `dE_int_kcalmol`, and
  `note`. `ghost_counterpoise_applied=false` means no separate Boys–Bernardi
  ghost-atom calculation was run; method-inherent corrections such as r2SCAN-3c
  gCP are unaffected. Spreadsheet-formula-leading text is neutralized.
- The generated CSV uses an adjacent v2 owner marker with a hashed workflow
  identity and current/pending content digests. Digest-bound ownership logic
  recovers interrupted creates, replacements, and deletes. Foreign, malformed, missing,
  or digest-mismatched ownership never authorizes overwrite/deletion; user-edited
  content is preserved. Ownership is preflighted before replacing last-good base
  SI files. Uploaded archives cannot supply the CSV or marker, and
  remote uploads cannot set server-owned `interaction_energy.priority`.
- Restart preserves the interaction route, per-fragment state/resources, and
  generation fingerprint. Interaction and RMSD grouping settings are immutable
  after fan-out, and an original primary stage cannot be reopened while that
  fan-out exists; disabling the feature retires its interaction stages. Restart
  reloads the copied durable input XYZ to revalidate the full partition and each
  fragment electron state before accepting an enabled config.
- SI publication persists pending, attempt count, next-retry time, blocked state,
  generation, and error in workflow/registry metadata. Transient failures retry
  after 30/60/120/240-second exponential delays and block after the fifth failed
  writer attempt; deterministic conflicts block immediately. Pre-writer
  workflow/registry/report checkpoint failures do not consume this writer budget;
  any persisted pending marker remains immediately due for infrastructure
  reconciliation. Registry clear follows workflow-then-registry lock order and
  rechecks authoritative workflow identity/status; publication-pending,
  publication-blocked, final-child-sync-pending, identity-quarantined, or
  authoritatively active records are not cleared as stale. Quarantined durable IDs
  remain in the payload as evidence while the registry uses the trusted workspace
  name as its unique key and records the observed ID in metadata. Fix the cause, then run
  `orca_auto run-dir <workflow_dir> --force` to re-arm a blocked publication.
- Set `runs_root` in `orca_auto.yaml` (or `workflow_root`/`workflow.root` in
  `flow.yaml`) before submitting workflow directories.
- Public workflow `run-dir` reads workflow type and XYZ inputs from `flow.yaml`
  or the standard filenames written by `scaffold`; it accepts only
  `--max-cores` and `--max-memory-gb` as workflow resource overrides.
- `flow.yaml` and internal engine YAML job manifests must be single-link regular
  UTF-8 files no larger than 1 MiB. The bounded loader admits at most 32 alias
  uses, 10,000 parsed/expanded nodes, and 64 nesting levels; recursive/cyclic
  aliases or object graphs fail closed.
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
  unknown option names are rejected instead of ignored. A non-empty legacy xTB
  `namespace` is also rejected; remove it before resubmitting. xTB always emits
  explicit `--chrg` and `--uhf` values plus `--norestart`, so an older restart
  file cannot silently alter a new generation.
- CREST topology overrides can be placed under `crest:` in `flow.yaml`, including `gfn: ff`, `no_preopt: true`, `noreftopo: true`, `notopo: true`, and `nocbonds: true`
- Workflow-level `orca.charge` and `orca.multiplicity` define the electronic
  state for every CREST, xTB, and ORCA stage. Engine-local `charge`/`uhf` values
  may repeat that state, but conflicting or malformed values are rejected. The
  selected xTB/CREST input must contain known elements with atomic number at
  most 86, leave a nonnegative electron count, and use a UHF unpaired-electron
  count in range with electron-count-compatible parity.
- Local geometry inputs are limited to 10,000 atoms. xTB Hessian jobs and ORCA
  frequency/Hessian-producing inputs use a 1,000-atom limit. Discord-uploaded
  workflow XYZ and standalone ORCA geometries use the remote 200-atom limit.
- CREST exit code 0 is accepted only when a retained output contains at least
  one strictly valid, finite XYZ frame. Every valid named retained ensemble is
  preserved: geometries found only in later rotamer outputs remain candidates,
  while cross-file overlaps do not duplicate downstream candidates. Non-finite xTB energies and XYZ
  coordinates are unusable and are never materialized for ORCA.
- CREST receives an absolute immutable input-snapshot path and an explicitly
  bound xTB executable (`-xnam`). orca_auto does not pass `--scratch`, because
  CREST 3.0.2's legacy scratch copier invokes an unsafe shell path. The
  `gfn2//gfnff` composite emits the required `--legacy`; charge and UHF are
  always explicit, including neutral singlet values.
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
  semantics implemented against CREST 3.0.2. `mdlen`/`len` (MD length in ps;
  aliases that must agree)
  and `wscal` are finite positive reals rendered without exponent notation to
  at most six decimal places; values below `0.000001` are rejected.
  `tstep` and `mddump` each require an explicit MD length. Without an expert
  override, `tstep` is at most 5.0 fs for GFN-xTB, 1.5 fs for GFN-FF, and 2.0 fs
  for `gfn2//gfnff`; `shake: 1` tightens that cap to 2.0 fs. Setting
  `allow_high_tstep: true` permits the native 0.001–2500 fs range but does not
  bypass the work budget. `mddump` is an integer in `1..2147483647`.
  With an explicit `mdlen`, the default `max_md_steps` is 10,000,000 aggregate
  steps across CREST's
  estimated trajectory/restart/rotamer multiplicity: base 6 for `nci` or a
  quick mode and 14 otherwise, multiplied by 1 restart for `mquick` or 5
  otherwise, then by 1 for `nci`, a quick mode, or `norotmd` and 2 otherwise.
  Without `mdlen`, CREST's automatic 2.5–500 ps range is admitted at its 500 ps
  worst case with a default 14,000,000-step budget. This admits standard GFN-xTB
  defaults. At the standard non-quick trajectory multiplicity, GFN-FF and
  `gfn2//gfnff` exceed that budget and must provide a bounded `mdlen` or an
  explicit higher `max_md_steps` plus `allow_high_cost_md: true`.
  A larger bound, up to the
  native integer limit, requires `allow_high_cost_md: true`. The default
  `max_dump_frames` is 100,000 estimated aggregate frames (aggregate simulated
  time divided by `mddump`); a larger bound
  requires `allow_high_volume_md: true`. `shake` is `0`, `1`, or `2`. The exact
  `norotmd`, `cross`, and
  `nocross` keys accept YAML booleans or canonical boolean forms
  (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`), and `cross`/`nocross` are
  mutually exclusive. `cross: true` keeps CREST 3.0.2's default GC crossing
  without emitting its broken redundant `--cross` flag; `nocross: true` emits
  `--nocross`. Malformed values fail the job closed rather than reaching CREST.
  Independently of the step bound, atom count multiplied by estimated aggregate
  MD steps must not exceed the absolute local ceiling of 50,000,000,000
  atom-steps.
- xTB ranking admits at most 100 candidate evaluations by default. Local
  reaction-workflow manifests may set `xtb.max_ranking_evaluations` up to the native candidate
  cap of 1,000; values above 100 also require
  `xtb.allow_high_cost_ranking: true`.
- Discord-uploaded workflows may not set `crest.mdlen`, `crest.len`,
  `crest.tstep`, `crest.allow_high_tstep`, `crest.mddump`,
  `crest.max_md_steps`, `crest.allow_high_cost_md`,
  `crest.max_dump_frames`, `crest.allow_high_volume_md`,
  `xtb.max_ranking_evaluations`, or `xtb.allow_high_cost_ranking`. These cost
  and output-volume budgets are server-owned for remote ingress; trusted local
  `run-dir` workflows may use the validated controls above. Remote workflow
  ingress injects `crest.mdlen: 5.0` ps and rejects the request when its estimated
  CREST work exceeds 50,000,000 atom-steps.
- `scaffold ts_search` and `scaffold conformer_search` write `flow.yaml` with `crest_mode: standard` by default; change it to `nci` when needed

There is no public direct-execution mode for new work. `run-dir` is the durable submission path.

#### Immutable execution, provenance, and upgrade boundary

- xTB, CREST, and ORCA bind selected inputs at submission. Each source file is
  limited to 64 MiB. xTB and ORCA additionally cap one generation's aggregate
  bound input at 256 MiB; ORCA accepts at most 128 file-reference directives.
  CREST has the per-file limit but no separate aggregate limit. A downstream
  output XYZ materialization is bounded at 512 MiB.
- xTB/CREST snapshots keep their private per-submission directories under
  `.orca_auto_input_snapshots/`; each namespace is unique and reserved
  exclusively. A public task id alone is not the snapshot-ownership key.
- A new ORCA submission instead creates exactly one visible direct child named
  `generation-YYYYMMDD-HHMMSS-<8-hex>/` in the job directory. Its bound `.inp`
  keeps the source basename. Confined XYZ, GBW, Hessian, point-charge, IRC, and
  NEB dependencies also keep their source basenames, with no `.inputs/` layer.
  ORCA raw outputs are written beside those inputs. New ORCA submissions do not
  create `.orca_auto_orca_executions/` or an ORCA
  `.orca_auto_input_snapshots/` tree. Audit provenance still records source and
  executed paths, SHA-256, and byte size; the readable names do not weaken
  content-identity verification.
- Snapshot and generation trees are retained for queue replay, retry,
  reconciliation, and audit. There is no standalone snapshot-GC command. Never
  edit or delete a generation used by a pending, running, retrying,
  cancel-pending, or repairable terminal row. Reclaim it only with an
  intentionally retired job/workflow after confirming that no queue or recovery
  record still references it.
- xTB/CREST run with a job-local clean `HOME`/`XDG_CONFIG_HOME` and a captured
  `PATH`, `LD_LIBRARY_PATH`, `XTBPATH`, and `XTBHOME`. Executable path, SHA-256,
  and size are verified around execution. Contents reached through shared
  libraries, `XTBPATH`, or `XTBHOME` are not snapshotted, and engine semantic
  versions are not automatically probed. Keep the exact qualified
  distribution and external parameters immutable for the job lifetime, and do
  not treat other processes under the worker UID as hostile isolation tenants.
- Before deploying the ORCA visible-generation format, drain all old-build
  pending and active ORCA rows and finish every incomplete terminal replay and
  snapshot intent. Alternatively cancel/clear affected work and resubmit it
  after the upgrade. Old-format rows are not adopted in place. Existing
  terminal `.orca_auto_orca_executions/` and ORCA
  `.orca_auto_input_snapshots/` history stays where it is; the upgrade does not
  migrate or rename it. The xTB/CREST snapshot layout is unchanged.
- New xTB/CREST terminal outputs carry content identities that are verified
  before downstream parsing. A legacy completed output can receive a marked
  read-time identity backfill, which does not retroactively prove historical
  terminal bytes. If an exact same-generation terminal state/report pair is
  unrecoverable, activity shows `repair_blocked` with its reason instead of
  repeatedly attempting an ambiguous repair.
#### Standalone xTB-MD contract

- `xtb_md_job.yaml` is recognized only for a standalone directory under
  `runs_root`; it does not create or join a workflow. An optimized starting
  geometry is strongly recommended.
- Required fields are `schema_version: 1`, one local-file `input_xyz`, `gfn`
  (`1` or `2`), `ensemble` (`nvt` or `nve`), positive finite `temperature_k`,
  `time_ps`, `step_fs`, and `dump_fs`, plus positive integer
  `walltime_seconds`. Unknown fields are rejected. `time_ps` (after conversion
  to fs) and `dump_fs` must be exact positive integer multiples of `step_fs`.
- Optional fields are `charge`/`uhf` (default `0`),
  `hydrogen_mass_amu` (default `4`), `shake` (`0`, `1`, or `2`; default `2`),
  positive finite `scc_accuracy` (default `2.0`), paired
  `solvent_model`/`solvent`, and `resources.max_cores`/
  `resources.max_memory_gb` within the configured ceilings.
- Server-owned ceilings are 10,000 atoms, 999,999 steps, 100,000,000
  atom-steps, 100,000 frames, 86,400 seconds wall time, 1 GiB output, and
  10,000 output files. These are not manifest override knobs.
- The adapter creates one canonical fresh `$md` input with `$samerand` and
  `restart=false`. It exposes no arbitrary seed, `--omd`, raw xcontrol,
  constraint/metadynamics, workflow, retry, or resume surface. Cancellation
  terminates the active process group; interruption/orphan recovery is terminal
  rather than requeued.
- The adapter currently accepts exactly xTB 6.7.1, the latest stable release
  when this contract was introduced. This does not claim that 6.7.1 is
  issue-free. Exit code 0 and `xtbmdok` are insufficient: known false-success
  markers such as `MD is unstable, emergency exit` and
  `but still taking it as converged!`, or incomplete/non-finite trajectory and
  checkpoint evidence, fail the job closed.
- `job_state.json`, `job_report.json`, and `job_report.md` live at the job root.
  The immutable generated input, logs, `xtb.trj`, `mdrestart`, and `xtbmdok`
  are retained under `.orca_auto_xtb_md_executions/<job_id>/` with content
  identities in the terminal report.

### 7.3 `queue cancel`

```bash
orca_auto queue cancel q_20260403_151220_ab12cd
orca_auto queue cancel /absolute/path/to/orca_runs/Int1_DMSO
```

`queue cancel` accepts workflow ids for whole-workflow cancellation plus queue ids, run ids,
and known path aliases for individual jobs.

### 7.4 `queue list`

```bash
orca_auto queue list
orca_auto queue list --engine orca
orca_auto queue list --engine xtb_md
orca_auto queue list --status pending
orca_auto queue list --engine xtb
```

`queue list` shows workflow and engine activity in one view, but workflow child simulations
are rendered underneath their parent workflow with indentation. The text view prints a table
with `Status`, `Job ID`, `Detail`, and `Elapsed` columns, where the detail field surfaces
workflow or job intent such as `ts_search(nci)`, `IRC`, or `NEB`. By default, only ORCA child
jobs are expanded beneath workflow parents; internal xTB/CREST child jobs stay hidden in the
combined text view to reduce noise, but remain available through `--engine ... --kind job`
filters and `--json`. Top-level ORCA jobs remain top-level entries. The
`active_simulations` line counts only the currently running
simulations that consume the shared `scheduler.max_active_simulations` slots.

On an interactive terminal the text view is styled: a summary band replaces the plain
`active_simulations:` line with per-status counts (running, queued, done, failed,
cancelled), workflow children are drawn with box-drawing tree connectors (`├─`/`└─`)
instead of plain indentation, and each row carries a status-colored left rail.
`queue list --watch` shows a spinner and a clock in its banner. These affordances are
terminal-only: piped output, `--json`, `NO_COLOR`, and `--no-color` keep the plain,
byte-stable table — including the `active_simulations:` line and plain indentation — so
scripts and the messenger `/list` view are unaffected.

The selected bot's list command (Telegram `/list`, Discord `!list`) renders the same table layout and default
workflow-child visibility policy, except it omits the `ID` column so each row fits on a
single line on narrow mobile screens. Its actions message offers per-activity cancel
buttons plus refresh and "clear finished" buttons (the latter equivalent to `/list clear`).

`queue list --watch` continuously refreshes the list until interrupted; `--interval` sets
the refresh seconds (default 2.0). On an interactive terminal the watch view also draws a
live system resource line above the table — CPU utilization, RAM used/total, and load
average with colored block-bar gauges — sampled from Linux `/proc` between refreshes with
no added dependency. It fails closed: on a host without a readable `/proc` (or for any
individual field that cannot be read) the line is omitted, and it never appears in piped or
`--no-color` output. (CPU utilization is a delta measurement, so it first appears on the
second refresh.) Each running job is additionally annotated with its own CPU% and resident
memory, across every engine (ORCA, internal xTB/CREST, standalone xTB-MD): the usage is
attributed from the engine PID/PGID the worker records in the durable admission slot —
validated against its boot id and process start ticks so a recycled id is never
mis-attributed — and aggregated from `/proc` by process group. `queue list clear` prunes
completed, failed, and cancelled entries from the unified list.

### 7.5 CLI Output and Global Flags

- Table output is colorized by status when stdout is a terminal. Color is disabled
  automatically when piped or when `NO_COLOR` is set, and can be forced off with
  `--no-color` (e.g. `orca_auto --no-color queue list`). The `queue cancel`, `run-dir`,
  and `service status` outputs colorize status fields the same way.
- `orca_auto --version` prints the installed version, and running `orca_auto` with no
  command prints help. Errors and recovery hints are written to stderr.
- `orca_auto service status --json` emits machine-readable output for scripting.
- The messenger bot supports cancel (`/cancel` on Telegram, `!cancel` on Discord) with confirmation via native buttons before
  cancelling. In the `/list` actions message the cancel button still routes through that
  confirmation step. At most four cancellable activities are shown so the shared card fits
  Discord's five-row component limit; executing a cancel or clear auto-refreshes the list.
- When `messenger.discord.uploads.enabled` is true, an allowlisted Discord operator
  can attach one `.zip` or `.tar.gz` run-directory to `!run`. Admission and actual
  download bytes are bounded before inspection. Exactly one root `flow.yaml` or
  lower-case `*.inp` is required, server-owned paths and resource ceilings cannot be
  overridden; uploaded workflows also reject every CREST runtime/trajectory budget
  and xTB ranking-cost control listed in §7.2. The durable Queue/Discard action is bound to
  the originating message, attachment, channel, and actor. Extraction is published
  atomically under `runs_root`; uncertain commits are retained and reconciled rather
  than deleted.

### 7.6 `scan-notify`

```bash
orca_auto scan-notify
```

Behavior:

- `scan-notify` runs a one-shot scan of the configured ORCA root and sends
  discovery alerts through the active messenger provider, then exits. It is not a live monitor.

### 7.7 `smoke`

```bash
orca_auto smoke
```

The default fake profile runs 11 retained ORCA, standalone xTB-MD, and workflow
success/fail-closed scenarios without licensed engine binaries. It prints the
batch directory and the offline `review/index.html` and `summary.md` paths. Open
the review index to inspect generated reports, SI files, states, logs, and raw
artifacts; a smoke PASS verifies the declared software contract, not chemical
meaning.

Batches are retained under `<runs_root>/.orca_auto_smoke/batches/`. That tree is
reserved and excluded from production submission/discovery. Remove or archive a
whole reviewed batch rather than selected child files. Real-engine acceptance is
opt-in with `--profile real-orca` or `--profile real-xtb`, an explicit executable
environment variable, and the shared production config; see
[VALIDATION.md](VALIDATION.md) for the exact commands and limitations.

### 7.8 Long-Running Services

Long-running worker and messenger bot processes are managed through `systemd`
only. Public CLI commands do not start those services directly.

Behavior:

- `orca_auto-queue-worker@.service` supervises ORCA and standalone xTB-MD by default
- The same worker service also starts workflow supervision plus the internal CREST and xTB workers under the shared `runs_root`
- ORCA, xTB-MD, xTB, and CREST share the same admission cap; xTB-MD also obeys its subcap. ORCA reserves a slot in
  the parent worker, attaches queue identity metadata after the child starts,
  and lets the ORCA child activate/release that reservation during execution.
- `orca_auto-bot@.service` runs `orca_auto.flow.bot.runner`, which selects the configured
  Telegram or Discord gateway from `orca_auto.yaml`
- Workflow messenger alerts keep per-job ORCA messages, but summarize internal CREST and reaction-path xTB child phases in one message each after those phases finish
- `orca_auto-runtime@.target` starts both services together

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
- [`systemd/orca_auto-queue-worker@.service`](../systemd/orca_auto-queue-worker@.service)
- [`systemd/orca_auto-bot@.service`](../systemd/orca_auto-bot@.service)

Recommended always-on runtime install flow when the selected messenger bot is configured:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

Before enabling the combined runtime target, complete the selected Telegram or Discord
interactive bot settings in `orca_auto.yaml`.

Assumptions of the unified runtime templates:

- Repository path: `/home/<user>/orca_auto`
- Config path: `/home/<user>/orca_auto/config/orca_auto.yaml`

If your paths differ, edit the copied unit before enabling it.

The unified queue-worker service supervises ORCA and also starts workflow
supervision plus the internal CREST and xTB workers. The shared
`scheduler.max_active_simulations` setting still limits the combined number of
active simulations across ORCA and workflow-managed internal engine stages.

If the selected provider is incomplete, `orca_auto systemd install` enables
`orca_auto-queue-worker@$(whoami)` directly. Run the same command again after
completing bot configuration to enable the full runtime target.

Workflow supervision belongs to `orca_auto-queue-worker@.service`.

## 9) Completion Determination Rules

The mode is determined from the input route line (`! ...`).

- TS mode: Contains `OptTS` or `NEB-TS`
- Opt mode: Everything else

TS mode completion:

- `****ORCA TERMINATED NORMALLY****` exists
- Exactly 1 imaginary frequency is present
- If the route contains `IRC`, the IRC marker is also required

Opt mode completion:

- `****ORCA TERMINATED NORMALLY****` exists

## 10) Failure Classification and Automatic Recovery

Representative statuses:

- `completed`
- `error_scf`
- `error_scfgrad_abort`
- `error_multiplicity_impossible`
- `error_disk_io`
- `error_memory`
- `error_geometry` (for example ORCA zero-distance geometry collapse)
- `geom_not_converged`
- `ts_not_found`
- `incomplete`
- `unknown_failure`

Retry policy:

- `Opt`, `Opt+Freq`, `Freq`, and single-point routes: no automatic retry. Failed
  `*.xyz`/`.gbw` artifacts are not treated as useful generic restart evidence.
- Standalone `OptTS`/`NEB-TS`: no automatic retry. Hessian hardening remains an
  explicit input choice rather than an automatic fallback.
- `ScanTS`: retries fire ONLY on calculation failures, from scan artifacts.
  A mid-scan crash (no surface table yet) continues the scan from the last
  numbered point; a zero-distance abort in ORCA's TS-guess refinement (after
  the scan bracketed a maximum) gets one OptTS retry directly from the highest
  surface point (`ScanTS` -> `OptTS`, scan block removed), bypassing the
  refinement. Any failure after a finished scan — including `ts_not_found` —
  ends the run with `scants_recipes_exhausted`: endpoint-extension and
  reverse-scan exploration belongs to the `scan_ts_search` workflow, which is
  the recommended TS-search path. Generic SCF/geometry hardening is not
  applied.

Geometry restart rules:

- Generic geometry/checkpoint restart is not part of normal non-ScanTS retry.
- ScanTS may use numbered scan `*.NNN.xyz` artifacts for continuation retries.
- Fail closed instead of repeating the original geometry unchanged if no
  route-specific rewrite is available.

Principles:

- Original charge and multiplicity are never changed automatically
- Original `.inp` is preserved
- Retry inputs are generated as `<name>.retryNN.inp`

## 11) Output Files

The submitted ORCA job directory keeps the user-authored inputs, `run.lock`, and
the latest public summaries/reports:

- `job_state.json`
- `job_report.json`
- `job_report.md`
- `job_report.html` (Opt, OptTS, NEB-TS, ScanTS, IRC, and relaxed-scan jobs):
  self-contained visual report assembled from common page chrome plus
  calculation components. Depending on the parsed route/output it may include
  the scan energy profile (ScanTS and plain relaxed scans, i.e. `Opt` routes
  with a `%geom Scan` block), CI-NEB path profile plus TS refinement trace
  (NEB-TS), IRC path profile with combined OptTS/Freq sections when present, or
  optimization convergence trace (Opt/OptTS), the retry-recipe chain, and a
  vibrational summary (imaginary modes, dominant atom displacements, and — for
  scans — alignment with the scanned coordinate)
- `si_block.md`: for completed jobs ending on a stationary point (single points
  included, relaxed scans excluded), a copy-paste Supporting Information block
  with the route line and ORCA version, E(el)/ZPE/H/G and the G−E(el)
  correction, Nimag with an imaginary-mode summary, the final coordinates, and
  `⚠` lint lines for reviewer-visible problems; for IRC routes, a
  summary-only validation block without coordinates

Each submission places its bound inputs and raw outputs in one visible direct
child, for example:

```text
TS8(NEB-TS)/
├── nebts.inp
├── input.xyz
├── output.xyz
├── guessTS.xyz
├── job_state.json
├── job_report.json
├── run.lock
└── generation-20260714-224054-959479f2/
    ├── nebts.inp
    ├── input.xyz
    ├── output.xyz
    ├── guessTS.xyz
    ├── nebts.out
    ├── nebts.gbw
    ├── nebts.NEB.log
    ├── job_state.json
    └── job_report.json
```

This example is not an exhaustive file listing. Internal synchronization files
may also remain: `.orca.process.lock` in the generation and/or job root and
`.job_state.mutation.lock` at the job root. While an ORCA process record is
active, `orca.process.json` is present in its generation.

The generation's bound `.inp` has the exact selected source basename, so ORCA
uses the expected output stem rather than adding `.run` or `.bound`. Referenced
inputs likewise retain their original basenames. `job_state.json` and
`job_report.json` in the generation mirror that generation's record; the copies
at the job root are the latest public summary and are updated by later sibling
generations. `run.lock` stays at the job root; the mere presence of its file is
not proof that a process currently owns the lock.

Important `job_state.json` fields:

- `job_id`
- `run_id`
- `reaction_dir`
- `selected_inp`
- `execution_provenance`
- `max_retries`
- `status`
- `attempts[]`
- `final_result`

Important `attempts[]` fields:

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

Important `job_report.json` fields:

- `job_id`
- `run_id`
- `reaction_dir`
- `selected_inp`
- `status`
- `attempt_count`
- `max_retries`
- `attempts[]`
- `final_result`

For snapshot-bound jobs, `selected_inp`/attempt `inp_path` name the exact bound
input in the visible generation. `execution_provenance.source_selected_inp`
records the user-facing source selected at submission; the bound/materialized
and attempt identity records carry their path, SHA-256, and byte size.

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
- `report_md_path`
- `attempt_count`
- `max_retries`
- `attempts`
- `final_result`
- `resource_request`
- `resource_actual`

Compatibility note:

- `reaction_dir` remains the ORCA queue and downstream contract field.
  Shared core helpers may also understand generic `job_dir` metadata for other
  engines, but ORCA producers should not replace `reaction_dir` with `job_dir`.
- Engine workers run only from queue identity. The unified child entrypoint is
  `python -m orca_auto.core.engines.worker_child --engine <orca|xtb_md|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`.
  Legacy ORCA worker-job direct execution by reaction directory is not supported.

## 12) Recommended Workflow

1. Ensure the worker service is active under `systemd`
2. Submit with `run-dir`
3. Confirm `status: queued`
4. Close the submission terminal if desired
5. Monitor with `list` or `journalctl`
6. Review `job_report.md` after completion
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

Focused regression commands used during the monorepo migration:

```bash
pytest tests/flow -q
pytest tests/integration -q
pytest tests/test_run_job.py tests/test_queue_worker.py -q
pytest tests/core/test_engine_child.py tests/core/test_engine_admission.py -q
```

For package-layout and import guidance, see [DEVELOPMENT.md](DEVELOPMENT.md).
