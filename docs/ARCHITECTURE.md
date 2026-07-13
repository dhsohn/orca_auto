# orca_auto Architecture

**English** | [한국어](ARCHITECTURE.ko.md)

This document describes how orca_auto is structured and how work flows through
the system at runtime. It is aimed at developers and operators who need a mental
model of the package layout, the queue/worker lifecycle, the shared engine
abstraction, and the workflow orchestration layer.

For task-level usage see [README.md](../README.md), [QUICKSTART.md](QUICKSTART.md),
and [REFERENCE.md](REFERENCE.md). For package and import conventions see
[DEVELOPMENT.md](DEVELOPMENT.md).

---

## 1. What orca_auto Is

orca_auto is a **queue-first executor** for ORCA and standalone xTB molecular
dynamics (xTB-MD), and a **workflow orchestrator** for multi-stage
computational chemistry runs on Linux and WSL.

The core design principle is **durable submission, supervised execution**:

- User commands (`run-dir`) never launch a calculation directly. They validate
  the request and write a durable queue entry, then return.
- Long-running, externally supervised **workers** (under `systemd`) pick up
  queued work and execute it.
- Per-job state and reports are recorded on disk next to the
  calculation.

ORCA is the public, first-class engine with the richest retry/reporting/monitor
surface. **xTB-MD** is an independent first-class standalone engine with a
strict single-attempt contract. General **xTB** and **CREST** calculations remain
internal **workflow stages** rather than standalone public commands.

---

## 2. Layered Package Structure

All code lives under `src/orca_auto`. There are five main areas:

```text
src/orca_auto/
├── cli*.py / activity*.py / terminal_table.py / systemd_plan.py
│       # User-facing CLI surface (argparse, handlers, rendering)
│
├── core/                # Shared chemistry-platform infrastructure
│   ├── engines/         # Engine abstraction + unified worker/child runtime
│   ├── queue/           # Durable queue, worker loop, child execution, lifecycle
│   ├── admission/       # Machine-wide concurrency slot reservation
│   ├── indexing/        # Job-location index (where each job's outputs live)
│   ├── state/           # Shared engine state helpers
│   ├── config/          # Config schema + loading
│   ├── messaging/       # Neutral Doc/port + Telegram/Discord adapters
│   ├── notifications/   # Low-level Telegram transport + engine hooks
│   ├── commands/        # Shared run-dir / queue command logic
│   ├── paths/           # Path validation + workflow path resolution
│   └── utils/           # Locks, persistence, process tracking, coercion
│
├── orca/                # Canonical ORCA implementation (source of truth)
│   ├── commands/        # init, run_inp, queue, monitor
│   ├── runtime/         # Run locks
│   ├── engine.py        # ORCA EngineDefinition wiring
│   ├── attempt/         # Attempt engine, retry, resume, reporting
│   ├── parser/          # ORCA output parsing
│   ├── state*.py        # Per-job state machine + persistence
│   └── ...              # retry recipes, completion rules, indexing
│
├── xtb_md/              # Standalone xTB-MD manifest, runner, validation, state
│
└── flow/                # Workflow orchestration package
    ├── orchestration/   # advance_workflow loop, phases, stage runtime
    ├── engines/
    │   ├── xtb/         # Internal xTB workflow-stage engine
    │   └── crest/       # Internal CREST workflow-stage engine
    ├── adapters/        # Engine ↔ ORCA contract adapters
    ├── submitters/      # ORCA / internal-engine submission builders
    ├── templates.py     # Workflow template registry
    ├── manifest.py      # flow.yaml parsing
    ├── registry/        # Workflow registry + journal
    ├── bot/             # Provider-neutral bot application + gateway adapters
    └── telegram/        # Legacy-compatible Telegram facade/transport helpers
```

### Import rules (from DEVELOPMENT.md)

- ORCA implementation: `orca_auto.orca.*`
- Standalone xTB-MD implementation: `orca_auto.xtb_md.*`
- Shared infrastructure: `orca_auto.core.*`
- Workflow orchestration: `orca_auto.flow.*`
- Internal engines: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

`orca_auto.orca` is the only implementation source of truth for ORCA logic.
There are no top-level alias packages or alternate runtime shims.

Layering is directional and enforced by import-linter (`lint-imports`,
configured in `pyproject.toml`, run by `scripts/check.sh` and CI): `flow` may
import `orca` and `core`; `orca` and `xtb_md` may import only `core`; `core`
imports none of those domain packages. Engine wiring crosses layers exclusively through lazy string module
paths (`core/engines/registry.py`, `core/queue/worker/admission.py`) — the
deliberate plugin seam, invisible to the import graph on purpose.

---

## 3. Runtime Model: Submit → Queue → Worker → Child

The central control flow is the same for every engine. Submission is decoupled
from execution by a durable, on-disk queue.

```text
  ┌────────────┐   run-dir / scaffold       ┌──────────────────────────┐
  │   User     │ ─────────────────────────▶ │  CLI (orca_auto ...)      │
  │  (CLI)     │                            │  cli.py → cli_handlers     │
  └────────────┘                            └─────────────┬────────────┘
                                                          │ validate + route
                                                          ▼
                                            ┌──────────────────────────┐
                                            │  Durable queue (queue.json)│
                                            │  core/queue/store.py       │
                                            └─────────────┬────────────┘
                                                          │ (worker polls)
            systemd supervises                            ▼
  ┌────────────────────────┐            ┌──────────────────────────────┐
  │ orca_auto-queue-worker  │ ─────────▶ │  Queue worker loop            │
  │ orca_auto-bot           │            │  core/queue/worker/loop.py    │
  │ orca_auto-runtime@.target│           └─────────────┬────────────────┘
  └────────────────────────┘                          │ reserve admission slot
                                                       │ spawn child by queue id
                                                       ▼
                                        ┌──────────────────────────────┐
                                        │  Worker child entrypoint       │
                                        │  core/engines/worker_child.py  │
                                        │  --engine <orca|xtb_md|xtb|crest>│
                                        │  --queue-root --queue-id       │
                                        │  --admission-token             │
                                        └─────────────┬────────────────┘
                                                      │ resolve EngineDefinition
                                                      ▼
                                        ┌──────────────────────────────┐
                                        │  Engine execution + lifecycle  │
                                        │  execute → validate → finalize │
                                        │  report → notify               │
                                        └────────────────────────────────┘
```

Key properties:

- **`run-dir` is the only durable submission path.** It inspects the target
  directory, routes it to ORCA, standalone xTB-MD, or workflow handling, validates against the
  configured roots, rejects duplicate active entries, writes the queue entry,
  and returns `status: queued`. There is no public direct-execution mode for new
  work.
- **Workers run by queue identity.** A worker spawns the unified child with
  `--queue-root/--queue-id` (plus an `--admission-token`); the child resolves the
  current queue entry itself. The legacy ORCA `--reaction-dir` direct mode is not
  supported. The `reaction_dir` field is still preserved in the queue entry as
  the downstream contract.
- **A queue generation binds its executable inputs at submission.** Standalone
  xTB-MD and workflow xTB/CREST use content-addressed input snapshots in an exclusively reserved, unique
  namespace for each submission; ORCA builds a private generation tree and
  rewrites supported file references to confined copies. Workers verify those
  input and executable identities instead of re-reading mutable source files as
  the execution contract.
- **If no worker is running, work stays pending** in `queue.json` until a worker
  returns. Closing the submission terminal after `status: queued` is safe.

---

## 4. The Shared Engine Abstraction

The single most important architectural piece is that **ORCA, xTB-MD, xTB, and
CREST all execute through one common engine runtime.** This is what keeps admission,
child-process management, terminal side effects, and orphan recovery uniform.

### EngineDefinition

`core/engines/definitions.py` defines `EngineDefinition`, a frozen dataclass that
bundles everything the shared runtime needs for an engine:

- `load_config` — engine config loader
- `run_worker_child_job` — the child job runner
- `queue_worker_module` / `build_worker_child_command` — parent-worker wiring
- `runtime_roots_for_cfg`, `queue_functions` — queue discovery
- `artifact_adapter` — build/load payloads + report markdown
- `notification_hooks` — started / finished / retry callbacks
- `context_builder`, `runner_callbacks` — DI seams for execution

Each engine package exposes an `ENGINE_DEFINITION` constant:

| Engine | Module                                  |
|--------|-----------------------------------------|
| orca   | `orca_auto.orca.engine`                 |
| xtb_md | `orca_auto.xtb_md.engine`               |
| xtb    | `orca_auto.flow.engines.xtb.engine`     |
| crest  | `orca_auto.flow.engines.crest.engine`   |

`core/engines/registry.py` resolves an engine id to its `EngineDefinition` by
importing the module and reading `ENGINE_DEFINITION`. This registry is the only
place that knows the engine-id → module mapping.

### Unified child entrypoint

All engine work runs through one entrypoint:

```bash
python -m orca_auto.core.engines.worker_child \
  --engine <orca|xtb_md|xtb|crest> \
  --config <path> \
  --queue-root <path> \
  --queue-id <id> \
  --admission-token <token>
```

The parent worker (`EngineQueueWorker`) reserves an admission slot, spawns this
child, and finalizes the terminal queue result after the child exits. ORCA keeps
its richer domain behavior (state machine, retry, reports) inside
`orca_auto.orca`, but the *lifecycle scaffolding* around it is shared.

---

## 5. Admission Control (Shared Concurrency Cap)

`core/admission/` implements machine-wide concurrency limiting so that ORCA,
standalone xTB-MD, and all internal workflow stages compete for one shared pool.

- The cap is `scheduler.max_active_simulations`. It is **shared across ORCA,
  standalone xTB-MD, internal xTB stages, and internal CREST stages.** A second
  same-lock check applies the positive standalone xTB-MD subcap
  `scheduler.max_active_xtb_md` (default `1`).
- Slots are persisted as records in an admission file under a shared
  `admission_root` (defaults to `<runs_root>/.admission`), guarded by a file
  lock (`admission_lock`).
- `AdmissionStore` (in `store.py`) is the persistence facade for one admission
  root. Module-level functions (`reserve_slot`, `activate_reserved_slot`,
  `release_slot`, `update_slot_metadata`, `reconcile_stale_slots`) remain the
  public API.
- **Reservation lifecycle:** a worker reserves a slot (`reserve_slot`), the child
  attaches queue-identity metadata and activates it
  (`activate_reserved_slot`), and the slot is released on terminal exit
  (`release_slot`).
- **Liveness / stale recovery:** each slot records `owner_pid` and the process
  start ticks. `_slot_owner_alive` validates the PID is still the same process,
  so crashed owners' slots are reclaimed by `reconcile_stale_slots` rather than
  leaking capacity.

This is why the `active_simulations` line in `queue list` counts only runs that
currently consume a shared slot.

### Standalone xTB-MD boundary

`orca_auto.xtb_md` depends on shared `core` infrastructure but not on ORCA or
`flow`. Submission snapshots the strict `xtb_md_job.yaml`, its one XYZ geometry,
the generated canonical `$md` input, and the xTB executable/version identity.
The worker executes exactly one fresh attempt in
`.orca_auto_xtb_md_executions/<job_id>/`; retry, checkpoint resume, and workflow
handoff are deliberately absent. Cancellation terminates the process group, and
worker shutdown/crash/orphan recovery records a terminal non-retry result.

Exit code 0 is only one piece of evidence. Terminal validation also requires a
fresh `xtbmdok`, complete finite `xtb.trj` and `mdrestart` artifacts bound to the
submitted atom/step budget, bounded output, and no known xTB false-success
marker. The public state/report files are written at the job root; immutable
raw outputs remain in the private execution tree for audit.

---

## 6. ORCA Engine Internals

`orca_auto.orca` is the canonical ORCA implementation and has the deepest domain
logic. Notable pieces:

- **Input selection and binding:** at submission, ORCA selects the most recently
  modified `*.inp`, snapshots it and its supported file dependencies, and
  executes only the private bound input for that queue generation.
- **Attempt engine** (`attempt/engine.py`, `attempt/retry.py`,
  `attempt/resume.py`): runs an attempt, parses output, classifies the result,
  and decides whether to retry.
- **Output analysis** (`parser/`, `out_analyzer.py`,
  `output_status.py`, `completion_rules.py`): determines completion by mode —
  TS mode (`OptTS`/`NEB-TS`, requires exactly one imaginary frequency, plus an
  IRC marker when the route has `IRC`) vs Opt mode (normal termination).
- **Calculation-type retry policy** (`retry_policy.py`, `retry_recipes.py`):
  retry counts and rewrites are fixed by ORCA route type, not by the raw user
  retry count. Generic `TightSCF`/`SlowConv` escalation is not applied. Generic
  `Opt`/`Opt+Freq`/`Freq`/single-point routes do not get automatic retries;
  failed `.xyz`/`.gbw` artifacts are not reused as a generic rerun strategy.
  Standalone `OptTS`/`NEB-TS` also has no automatic retry; Hessian hardening is
  left to explicit user input. `ScanTS` retries fire only on calculation
  failures, from scan artifacts: a mid-scan crash continues from the last
  numbered scan point, and a zero-distance abort in ORCA's TS-guess refinement
  gets one OptTS retry from the highest surface point. Failures after a
  finished scan (including `ts_not_found`) end the run with
  `scants_recipes_exhausted` — endpoint extension and reverse-scan exploration
  belongs to the `scan_ts_search` workflow. If no
  route-specific rewrite is available, retry fails closed rather than repeating
  the identical input (`scants_recipes_exhausted` for an exhausted ScanTS
  recipe chain). Charge
  and multiplicity are
  **never** auto-changed; the original `.inp` is preserved; retries are written
  as `<name>.retryNN.inp`.
- **Restart/resume:** for retry/resume it generates a restart input with
  `MORead` + `%moinp` when a matching non-empty `.gbw` checkpoint exists; resumed
  inputs are written as `*.resume.inp` so user input is never mutated.
- **State & reports:** `state.py`/`state_machine.py` persist `job_state.json`;
  completion writes `job_report.json` and `job_report.md`; Opt, OptTS, NEB-TS,
  ScanTS, IRC, and relaxed-scan jobs also get `job_report.html` (`report/`), a
  self-contained visual report assembled by `report/composer.py` from common
  page chrome plus calculation components — scan energy profile (ScanTS and
  plain relaxed scans), CI-NEB path profile plus TS refinement trace (NEB-TS),
  IRC path profile with combined OptTS/Freq sections when the route includes
  them, or optimization convergence trace (Opt/OptTS), retry-recipe chain, and
  vibrational summary. Completed jobs ending on a stationary point also get
  `si_block.md` (`report/si.py`), a copy-paste Supporting Information block
  with energies, thermochemistry, Nimag, and coordinates; IRC routes get a
  summary-only validation block without coordinates.
- **Index:** `dft_index*.py` and `core/indexing` maintain a JSONL
  job-location index for discovery.

The fields ORCA exposes downstream (the "contract freeze") are documented in
[REFERENCE.md](REFERENCE.md) §11.1 — `reaction_dir` remains the ORCA
queue and downstream contract field.

---

## 7. Workflow Orchestration (`flow/`)

The `flow` package turns a single user submission into a multi-stage,
multi-engine pipeline. It is what lets a reaction-path or conformer job fan out
into internal xTB/CREST stages and then batch ORCA child jobs.

### Templates

`flow/templates.py` defines the two workflow templates:

| Template id            | CLI shortcut       | Purpose                              |
|------------------------|--------------------|--------------------------------------|
| `reaction_ts_search`   | `ts_search`        | Reactant×product TS search           |
| `conformer_screening`  | `conformer_search` | Conformer generation + screening     |

A workflow is materialized from a `flow.yaml` manifest (`flow/manifest.py`) in
the submitted directory. `scaffold` writes a starter `flow.yaml` plus the
standard XYZ filenames.

Manifest admission is bounded before materialization: the shared loader caps a
job manifest at 1 MiB, 32 YAML aliases, 10,000 parsed/expanded nodes, and 64
nesting levels, and rejects cyclic/recursive graphs. Central geometry limits cap
local work at 10,000 atoms, xTB/ORCA Hessian-producing work at 1,000, and
remote-upload work at 200.

### The advance loop

`flow/orchestration/advance.py` exposes `advance_workflow(...)`, the heart of
orchestration. Each invocation:

1. Resolves the workflow workspace and acquires a per-workflow lock.
2. Loads the durable `workflow.json` payload.
3. Runs through ordered **phases** (`advance_phases` / `_run_advance_phase`),
   which materialize stage jobs, submit ready ones, and sync the status of
   in-flight stages.
4. Finalizes and writes back the payload, then syncs the workflow registry.

This is a **cyclic, idempotent advance**: each cycle moves the workflow as far
forward as dependencies allow. Stage runtime details live under
`flow/orchestration/stage_runtime/` (per-engine submission, inputs, retry, sync,
handoff).

### Example: reaction TS search

`reaction_ts_search` orders selected reactant×product CREST pairs
deterministically, materializes at most the configured total xTB-stage cap,
waits for that xTB phase to reach terminal states, and then batches matching
ORCA OptTS child jobs from retained `ts_guess` artifacts up to the configured
total ORCA-stage cap.

### Example: conformer screening

`conformer_screening` starts with one CREST child job, then hands off up to 20
retained conformers to ORCA child jobs on the next workflow cycle.

### Internal-engine scoping

Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and outputs
live **only** under `<runs root>/<workflow_id>/<NN_engine>` (`01_crest`,
`02_xtb`, `03_orca`). They are not
part of the public CLI surface; users submit them through workflow `run-dir`.

---

## 8. Persistence & State Files

orca_auto is disk-backed throughout. Concurrency safety comes from file locks
(`core/utils/lock.py`) around every mutation. The main on-disk artifacts:

| File                        | Owner            | Purpose                                  |
|-----------------------------|------------------|------------------------------------------|
| `queue.json`                | core/queue       | Durable per-engine queue (source of truth)|
| admission slot file         | core/admission   | Active concurrency slots (machine-wide)  |
| `job_state.json`            | orca (state)     | Per-job attempts + status                |
| `job_report.json` / `.md`   | orca (reporting) | Human/machine completion report          |
| `.orca_auto_xtb_md_executions/<job_id>/` | xtb_md | Immutable MD execution outputs       |
| job-location index (JSONL)  | core/indexing    | Where each job's outputs currently live  |
| `workflow.json`             | flow             | Durable workflow payload                 |
| `workflow_report.html`      | flow (report)    | Live visual workflow summary             |
| `si_block.md`               | orca (report/si) | Per-structure SI block (paper-ready)     |
| `workflow_si.md` / `si_data.csv` | flow (si)   | Assembled workflow SI + machine-readable data |
| workflow registry + journal | flow/registry    | Cross-workflow listing + event history   |

The queue entry and tracked job-location record each expose a
frozen set of downstream fields (see REFERENCE.md §11.1) so that `flow` can
consume ORCA results without coupling to ORCA internals.

---

## 9. Notifications

`core/messaging/` owns two provider-neutral capability boundaries: immutable semantic
`Message` documents plus the notification `MessageChannel`, and normalized
command/action models plus `InteractiveMessenger`. Domain notifiers construct documents
without wire markup, while the interactive application receives only normalized values.
`MessengerConfig` owns both adapter configs and rejects unknown providers.

`core/notifications/` retains the low-level Telegram Bot API transport reused by
`TelegramChannel`, plus the engine notification hook layer (`engine_notifier.py`,
`engine_delivery.py`). `DiscordBotChannel` sends bot-authenticated notifications;
its shared HTTP retry/backoff helpers live in `discord_http.py`.
Each `EngineDefinition` can register `job_started` / `job_finished` / `retry` hooks.

`flow/bot/application.py` owns provider-neutral `/list`, `/cancel`, and `/help`
behavior. Native Telegram polling and Discord gateway adapters translate provider
events at the edge. Destructive actions use short, expiring, one-time opaque IDs bound
to the requesting provider, channel, and actor instead of embedding raw queue IDs in
button payloads. Discord `!run` additionally reserves a durable upload session before
the CDN download. Its confirmation action, archive digest, atomic publication path,
and downstream queue/workflow receipt survive gateway restarts; an indeterminate
commit is preserved for reconciliation. Workflow alerts keep per-job ORCA messages but summarize
internal CREST and reaction-path xTB child phases into one message each.

The `ActionStore` port defines one-time resolution and originator/operator audience
policies. Its current in-memory implementation serves list/cancel cards created by
the gateway in response to commands. Execution-authorizing upload confirmations use
the separate durable upload-session store so a restart cannot lose identity,
single-consumer, or commit-state guarantees. Notification messages do not yet carry actions.
When notification-origin controls are added, the worker-side sender and gateway must
share a durable `ActionStore` implementation so bindings survive the process boundary;
that extension belongs behind the same neutral card/action contracts.

The selected adapter is enabled only when its credentials are complete. Telegram
requires `messenger.telegram.bot_token` plus `chat_id`. Interactive Discord requires
`bot_token`, channel IDs, and an operator allowlist; bot token + default channel is also the canonical
notification path.

---

## 10. Configuration

Config is a single YAML file resolved in this order:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

`core/config/schema.py` defines the typed config dataclasses (e.g.
`RetryRuntimeConfig`, `CommonResourceConfig`, `TelegramConfig`) with normalizing
constructors. Notable rules:

- **Linux paths only.** Windows drive paths, `/mnt/<drive>/...`, relative
  executable paths, and `.exe` binaries are rejected. Configured ORCA/xTB/CREST
  executables must be absolute Linux paths to existing executable files.
- `scheduler.max_active_simulations` is the shared admission cap.
- `scheduler.max_active_xtb_md` is the standalone xTB-MD subcap and defaults to `1`.
- `scheduler.admission_root` is the shared slot-coordination root.
- Divergent engine-scoped scheduler values are rejected so every worker
  observes the same admission root and limit.
- `runs_root` is the single runs root for standalone ORCA/xTB-MD jobs,
  workflow workspaces, and internal-engine runs.
- `default_max_retries: 0` disables ORCA retries; any positive value enables the
  calculation-type retry policy, whose per-route caps are recorded in
  `job_state.json`/queue metadata.

---

## 11. Process Supervision (systemd)

Long-running services are managed through `systemd` only — they are not part of
the public CLI. Units live under `systemd/`:

| Unit                                  | Role                                            |
|---------------------------------------|-------------------------------------------------|
| `orca_auto-queue-worker@.service`     | Supervises ORCA/xTB-MD plus workflow + internal xTB/CREST workers |
| `orca_auto-bot@.service`              | Selected provider-neutral messenger bot        |
| `orca_auto-runtime@.target`           | Starts both together                            |

`orca_auto systemd install --user <user> --repo <repo>` renders and enables the
units. If the selected provider lacks interactive bot settings, only the queue worker is
enabled; rerun after completing them to enable the full target. On WSL, `systemd`
must be enabled in `/etc/wsl.conf`.

---

## 12. CLI Surface

The CLI is argparse-based (`cli.py` → `cli_parsers.py` → `cli_handlers.py`), with
status-aware colorized table rendering (`terminal_table.py`, `activity_*.py`,
`cli_style.py`). The public command surface:

- `init` — create/update shared config
- `scaffold <ts_search|conformer_search> <path>` — write workflow scaffolds
- `run-dir <path>` — durable submission (ORCA, standalone xTB-MD, or workflow, auto-routed)
- `queue list` / `queue cancel` / `queue list clear` — inspect/maintain the queue
- `service status` / `service restart` — runtime status (via systemd)
- `scan-notify` — one-shot discovery scan + active-messenger alerts
- `systemd install` — render and enable units

Engine-specific CLI modules are runtime-only worker entrypoints and are not a
place to add user commands.

---

## 13. Quality Gates

`scripts/check.sh` is the shared local + CI entrypoint: it creates/repairs
`.venv`, installs `.[dev]`, then runs `ruff check`, `ruff format --check`,
`mypy`, `lint-imports`, and the coverage-gated pytest suite. CI additionally runs Gitleaks,
ShellCheck, rendered systemd unit verification, a Python 3.11/3.12/3.13 matrix,
and a wheel typed-metadata smoke test.

Tests are organized as `tests/core/`, `tests/xtb_md/`, `tests/flow/`, `tests/flow/engines/`,
`tests/integration/`, and top-level ORCA regression tests. The project prefers
behavior-asserting tests (payloads, persisted files, CLI output, state
transitions) over internal delegation tests.

---

## 14. Design Principles Summary

- **Durable submission, supervised execution** — the queue is always the source
  of truth; workers are restartable and stateless between jobs.
- **One engine runtime, many engines** — `EngineDefinition` + the unified child
  entrypoint keep ORCA/xTB-MD/xTB/CREST lifecycles uniform while preserving ORCA's
  richer domain behavior.
- **Shared admission cap** — a single machine-wide slot pool bounds total
  concurrency across every engine.
- **Frozen downstream contracts** — `flow` consumes ORCA via a documented field
  contract (`reaction_dir`, job-location records), not internals.
- **Disk-backed, lock-guarded state** — every mutation goes through a file lock;
  crashed owners are reconciled rather than leaking slots.
- **Linux/WSL-first, systemd-supervised** — strict Linux path validation and
  `systemd` units for unattended operation.
