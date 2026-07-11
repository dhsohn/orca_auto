# orca_auto Detailed Reference

**English** | [한국어](REFERENCE.ko.md)

orca_auto is a queue-first executor for ORCA and workflow orchestration. ORCA
uses the shared internal-engine queue lifecycle for worker admission, child
entry execution, terminal side effects, and orphan recovery while preserving
its public ORCA queue contract. xTB and CREST run as internal workflow-stage
engines. This reference standardizes the shared public CLI and keeps the deeper
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
- Select the most recently modified `*.inp` in the target directory
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
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
  discord:
    webhook_url: ""

orca:
  runtime:
    default_max_retries: 2
  paths:
    orca_executable: "/path/to/orca/orca"
```

Field descriptions:

- `runs_root`: The single runs root shared by standalone ORCA jobs and workflow
  workspaces; completed runs stay here under their submitted directory names
- `orca.runtime.default_max_retries`: `0` disables ORCA retries; positive values
  enable the calculation-type retry policy
- `scheduler.max_active_simulations`: Shared total active-run cap across ORCA, internal xTB stages, and internal CREST stages
- `scheduler.admission_root`: Shared admission root for machine-wide slot
  coordination; defaults to `<runs_root>/.admission`. Scheduler controls belong
  at the top level; engine-scoped values may not diverge because that would
  split the shared admission pool.
- `workflow.paths.xtb_executable`: xTB executable path used by workflow-managed internal stages
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
  blank, the workflow runner falls back to PATH lookup at execution time.

## 7) CLI Usage

All public queue, submission, and scaffold commands should be
documented through `orca_auto ...`.

Public command surface:

- ORCA public commands are exposed through `orca_auto ...`
- xTB and CREST run as internal workflow/runtime engines; submit their work through workflow `run-dir` requests

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

- Chooses the latest `*.inp` when execution actually starts
- Queue workers execute by queue id rather than passing a direct
  `reaction_dir` command line. The queue entry still stores `reaction_dir`, and
  downstream ORCA/workflow contracts should keep using that field.
- `--force` re-runs even if completed output already exists
- Standalone ORCA resource metadata comes from the selected input's `%pal`
  and `%maxcore` directives, with config defaults injected only when those
  directives are missing. The shared `--max-cores` and `--max-memory-gb`
  flags do not override standalone ORCA input directives.
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
- `reaction_ts_search` expands all selected reactant x product CREST pairs into xTB child jobs, waits for the full xTB phase to reach terminal states, and then batches any matching ORCA OptTS child jobs from the retained `ts_guess` artifacts
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
  energy table (ΔE/ΔG), and each completed structure's SI block. When an
  opt+freq structure has a single-point stage on the identical geometry, the
  table adds the composite G = E(SP) + [G − E(el)](opt level). `si_data.csv`
  carries the same numbers for data-availability requirements.
- Set `runs_root` in `orca_auto.yaml` (or `workflow_root`/`workflow.root` in
  `flow.yaml`) before submitting workflow directories.
- Public workflow `run-dir` reads workflow type and XYZ inputs from `flow.yaml`
  or the standard filenames written by `scaffold`; it accepts only
  `--max-cores` and `--max-memory-gb` as workflow resource overrides.
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
- CREST topology overrides can be placed under `crest:` in `flow.yaml`, including `gfn: ff`, `no_preopt: true`, `noreftopo: true`, `notopo: true`, and `nocbonds: true`
- `scaffold ts_search` and `scaffold conformer_search` write `flow.yaml` with `crest_mode: standard` by default; change it to `nci` when needed

There is no public direct-execution mode for new work. `run-dir` is the durable submission path.

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
The integrated Telegram bot `/list` command renders the same table layout and default
workflow-child visibility policy, except it omits the `ID` column so each row fits on a
single line on narrow mobile screens. Its actions message offers per-activity cancel
buttons plus refresh and "clear finished" buttons (the latter equivalent to `/list clear`).

`queue list --watch` continuously refreshes the list until interrupted; `--interval` sets
the refresh seconds (default 2.0). `queue list clear` prunes completed, failed, and
cancelled entries from the unified list.

### 7.5 CLI Output and Global Flags

- Table output is colorized by status when stdout is a terminal. Color is disabled
  automatically when piped or when `NO_COLOR` is set, and can be forced off with
  `--no-color` (e.g. `orca_auto --no-color queue list`). The `queue cancel`, `run-dir`,
  and `service status` outputs colorize status fields the same way.
- `orca_auto --version` prints the installed version, and running `orca_auto` with no
  command prints help. Errors and recovery hints are written to stderr.
- `orca_auto service status --json` emits machine-readable output for scripting.
- The Telegram bot supports `/cancel <target>` with confirmation via inline buttons before
  cancelling. In the `/list` actions message the cancel button still routes through that
  confirmation step; when more than eight activities are cancellable the message notes how
  many are shown, and executing a cancel or clear auto-refreshes the list.

### 7.6 `scan-notify`

```bash
orca_auto scan-notify
```

Behavior:

- `scan-notify` runs a one-shot scan of the configured ORCA root and sends
  discovery alerts through the active messenger provider, then exits. It is not a live monitor.

### 7.7 Long-Running Services

Long-running worker and Telegram bot processes are managed through `systemd`
only. Public CLI commands do not start those services directly.

Behavior:

- `orca_auto-queue-worker@.service` supervises ORCA by default
- The same worker service also starts workflow supervision plus the internal CREST and xTB workers under the shared `runs_root`
- ORCA, xTB, and CREST share the same admission cap. ORCA reserves a slot in
  the parent worker, attaches queue identity metadata after the child starts,
  and lets the ORCA child activate/release that reservation during execution.
- `orca_auto-bot@.service` starts the unified Telegram bot using
  `messenger.telegram.bot_token` and `messenger.telegram.chat_id` from `orca_auto.yaml`
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

Recommended always-on runtime install flow when Telegram is configured:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

Before enabling the combined runtime target:

- Set `messenger.telegram.bot_token` and `messenger.telegram.chat_id` in `orca_auto.yaml`

Assumptions of the unified runtime templates:

- Repository path: `/home/<user>/orca_auto`
- Config path: `/home/<user>/orca_auto/config/orca_auto.yaml`

If your paths differ, edit the copied unit before enabling it.

The unified queue-worker service supervises ORCA and also starts workflow
supervision plus the internal CREST and xTB workers. The shared
`scheduler.max_active_simulations` setting still limits the combined number of
active simulations across ORCA and workflow-managed internal engine stages.

If Telegram is not configured yet, `orca_auto systemd install` enables
`orca_auto-queue-worker@$(whoami)` directly. Run the same command again after
setting `messenger.telegram.bot_token` and `messenger.telegram.chat_id` to enable the full
runtime target.

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

Generated in the job directory:

- `<stem>.out`, `<stem>.retryNN.out`
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

Important `job_state.json` fields:

- `job_id`
- `run_id`
- `reaction_dir`
- `selected_inp`
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
  `python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`.
  Legacy ORCA worker-job direct execution by reaction directory is not supported.

## 12) Recommended Workflow

1. Ensure the worker service is active under `systemd`
2. Submit with `run-dir`
3. Confirm `status: queued`
4. Close the submission terminal if desired
5. Monitor with `list` or `journalctl`
6. Review `job_report.md` after completion
7. Use `--force` only when a deliberate rerun is needed

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
