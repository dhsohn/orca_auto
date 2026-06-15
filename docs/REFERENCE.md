# orca_auto Detailed Reference

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

## 1) Project Purpose

- Work only within the configured `allowed_root`
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
- ORCA state, retry, report, notification, and auto-organize behavior remain
  ORCA-domain behavior; parent queue finalization still records the terminal
  queue result after the child exits
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
      xtb/                # xTB engine package
      crest/              # CREST engine package
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
- `organize orca`
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
resources:
  max_cores_per_task: 8
  max_memory_gb_per_task: 32

behavior:
  # ORCA-only; internal xTB/CREST stages do not organize.
  auto_organize_on_terminal: false

scheduler:
  max_active_simulations: 4
  admission_root: "/path/to/chem_admission"

workflow:
  root: "/path/to/workflow_root"
  paths:
    xtb_executable: "/path/to/xtb"
    crest_executable: "/path/to/crest"

telegram:
  bot_token: ""
  chat_id: ""
  timeout_seconds: 5.0
  max_attempts: 2
  retry_backoff_seconds: 0.5

orca:
  runtime:
    allowed_root: "/path/to/orca_runs"
    organized_root: "/path/to/orca_outputs"
    default_max_retries: 2
  paths:
    orca_executable: "/path/to/orca/orca"
```

Field descriptions for the `orca` section:

- `orca.runtime.allowed_root`: Root directory permitted for execution
- `orca.runtime.organized_root`: Root for organized outputs
- `orca.runtime.default_max_retries`: Maximum retry count after the initial attempt
- `scheduler.max_active_simulations`: Shared total active-run cap across ORCA, internal xTB stages, and internal CREST stages
- `scheduler.admission_root`: Shared admission root for machine-wide slot coordination
- `workflow.root`: Workflow root for workflow creation, activity inspection, and the integrated workflow worker
- `workflow.paths.xtb_executable`: xTB executable path used by workflow-managed internal stages
- `workflow.paths.crest_executable`: CREST executable path used by workflow-managed internal stages
- Internal xTB/CREST runtimes are scoped to each workflow
- Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and outputs are stored only under `workflow.root/<workflow_id>/internal/<engine>/{runs,outputs}`
- `orca.paths.orca_executable`: ORCA executable path

Notes:

- `default_max_retries=2` means `1 initial + 2 retries = 3 total attempts`
- Windows-style paths such as `C:\...` and `/mnt/c/...` are not supported in config

## 7) CLI Usage

All public queue, submission, scaffold, and organization commands should be
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

- `run-dir` materializes a workflow only when `flow.yaml` is present in the target directory
- If the target already contains `workflow.json` and the workflow failed, `run-dir` restarts failed/cancelled stages in that existing workspace instead of creating a new workflow
- If a directory mixes raw ORCA `*.inp` files with scaffold-style filenames but does not include `flow.yaml`, `run-dir` prefers ORCA direct submission
- reaction-path and conformer workflows create and submit xTB/CREST stages internally
- `reaction_ts_search` expands all selected reactant x product CREST pairs into xTB child jobs, waits for the full xTB phase to reach terminal states, and then batches any matching ORCA OptTS child jobs from the retained `ts_guess` artifacts
- `conformer_screening` starts with one CREST child job and then hands off up to 20 retained conformers to ORCA child jobs in the next workflow cycle. The scaffold shortcut is `orca_auto scaffold conformer_search <path>`.
- Set `workflow.root` in `orca_auto.yaml`, `workflow_root`/`workflow.root` in
  `flow.yaml`, or pass `--workflow-root` before using workflow commands.
- Public `run-dir` accepts `--workflow-type`, `--reactant-xyz`,
  `--product-xyz`, `--input-xyz`, `--max-cores`, and `--max-memory-gb` for
  workflow overrides. Other workflow settings come from `flow.yaml` and
  `orca_auto.yaml`.
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

### 7.5 `organize`

```bash
orca_auto organize orca --root '/absolute/path/to/orca_runs'
orca_auto organize orca --root '/absolute/path/to/orca_runs' --apply
```

Options:

- `organize orca --reaction-dir <dir>`: Organize one ORCA job directory
- `organize orca --root <dir>`: Scan from the configured ORCA root
- `organize orca --rebuild-index`: Rebuild the ORCA JSONL index
- `--apply`: Perform actual moves; otherwise the command is a dry run

### 7.6 `scan-notify`

```bash
orca_auto scan-notify
```

Behavior:

- `scan-notify` runs a one-shot scan of the configured ORCA root and sends
  Telegram discovery alerts, then exits. It is not a live monitor.

### 7.7 Long-Running Services

Long-running worker and Telegram bot processes are managed through `systemd`
only. Public CLI commands do not start those services directly.

Behavior:

- `orca_auto-queue-worker@.service` supervises ORCA by default
- If `workflow.root` is set, the same worker service also starts workflow supervision plus the internal CREST and xTB workers
- ORCA, xTB, and CREST share the same admission cap. ORCA reserves a slot in
  the parent worker, attaches queue identity metadata after the child starts,
  and lets the ORCA child activate/release that reservation during execution.
- `orca_auto-bot@.service` starts the unified Telegram bot using `telegram.bot_token` and `telegram.chat_id` from `orca_auto.yaml`
- Workflow Telegram alerts keep per-job ORCA messages, but summarize internal CREST and reaction-path xTB child phases in one message each after those phases finish
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

- [`systemd/orca_auto-runtime@.target`](/home/daehyupsohn/orca_auto/systemd/orca_auto-runtime@.target)
- [`systemd/orca_auto-queue-worker@.service`](/home/daehyupsohn/orca_auto/systemd/orca_auto-queue-worker@.service)
- [`systemd/orca_auto-bot@.service`](/home/daehyupsohn/orca_auto/systemd/orca_auto-bot@.service)

Recommended always-on runtime install flow when Telegram is configured:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
journalctl -u "orca_auto-queue-worker@$(whoami)" -f
journalctl -u "orca_auto-bot@$(whoami)" -f
```

Before enabling the combined runtime target:

- Set `telegram.bot_token` and `telegram.chat_id` in `orca_auto.yaml`
- Set `workflow.root` in `orca_auto.yaml` if you also want workflow supervision

Assumptions of the unified runtime templates:

- Repository path: `/home/<user>/orca_auto`
- Config path: `/home/<user>/orca_auto/config/orca_auto.yaml`

If your paths differ, edit the copied unit before enabling it.

The unified queue-worker service supervises ORCA by default. When `workflow.root` is
configured, it also starts workflow supervision plus the internal CREST and
xTB workers. The shared `scheduler.max_active_simulations` setting still limits
the combined number of active simulations across ORCA and workflow-managed
internal engine stages.

If Telegram is not configured yet, `orca_auto systemd install` enables
`orca_auto-queue-worker@$(whoami)` directly. Run the same command again after
setting `telegram.bot_token` and `telegram.chat_id` to enable the full runtime
target.

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
- `ts_not_found`
- `incomplete`
- `unknown_failure`

Retry modification order:

1. Add `TightSCF SlowConv` plus `%scf MaxIter 300`
2. Add `%geom Calc_Hess true`, `Recalc_Hess 5`, `MaxIter 300`
3. Reuse the last conservative retry recipe if more retries are allowed

Geometry restart rules:

- Prefer the previous attempt's matching `*.xyz`
- Fall back to the most recent `*_trj.xyz` or `.xyz`
- Keep the original geometry block if no restart geometry is available

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
- `organized_ref.json` after organize leaves a stub in the original run directory

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
- `organized_output_dir`
- `latest_known_path`
- `resource_request`
- `resource_actual`

Organize stub fields currently consumed downstream from `organized_ref.json`:

- `job_id`
- `run_id`
- `original_run_dir`
- `organized_output_dir`
- `selected_inp`
- `selected_input_xyz`
- `status`
- `job_type`
- `molecule_key`
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
- `organized_output_dir`
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
- Cause: the job directory path is outside `allowed_root`
- Action: Check `allowed_root` in `config/orca_auto.yaml`

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
