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

orca_auto is a **queue-first executor** for ORCA and a **workflow orchestrator**
for multi-stage computational chemistry runs on Linux and WSL.

The core design principle is **durable submission, supervised execution**:

- User commands (`run-dir`) never launch a calculation directly. They validate
  the request and write a durable queue entry, then return.
- Long-running, externally supervised **workers** (under `systemd`) pick up
  queued work and execute it.
- Per-job state and reports are recorded on disk next to the
  calculation.

ORCA is the public, first-class engine with the richest retry/reporting/monitor
surface. General **xTB** and **CREST** calculations remain
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
│   ├── messaging/       # Neutral Doc/port + Discord notification adapter
│   ├── notifications/   # Engine notification functions + delivery
│   ├── commands/        # Shared run-dir / queue command logic
│   ├── paths/           # Path validation + workflow path resolution
│   └── utils/           # Locks, persistence, process tracking, coercion
│
├── orca/                # Canonical ORCA implementation (source of truth)
│   ├── commands/        # Thin CLI adapters: init, run_inp, queue
│   ├── submission.py    # Durable run-dir submission and publication
│   ├── run_context.py   # Submission/execution target resolution
│   ├── execution.py     # Locked ORCA execution and recovery
│   ├── queue/
│   │   ├── worker.py    # Parent-worker composition only
│   │   ├── replay.py    # Reconciliation and durable terminal replay
│   │   ├── cancellation.py
│   │   ├── publication_repair.py
│   │   └── worker_tracking.py
│   ├── runtime/         # Run locks
│   ├── engine.py        # ORCA EngineDefinition wiring
│   ├── attempt/         # Attempt engine, retry, resume, reporting
│   ├── parser/          # ORCA output parsing
│   ├── state*.py        # Per-job state machine + persistence
│   └── ...              # retry policy, completion rules, indexing
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
    └── registry/        # Workflow registry + journal
```

### Import rules (from DEVELOPMENT.md)

- ORCA implementation: `orca_auto.orca.*`
- Shared infrastructure: `orca_auto.core.*`
- Workflow orchestration: `orca_auto.flow.*`
- Internal engines: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

`orca_auto.orca` is the only implementation source of truth for ORCA logic.
There are no top-level alias packages or alternate runtime shims.

Layering is directional and enforced by import-linter (`lint-imports`,
configured in `pyproject.toml`, run by `scripts/check.sh` and CI): `flow` may
import `orca` and `core`; `orca` may import only `core`; `core`
imports none of those domain packages. Engine wiring crosses layers exclusively through lazy string module
paths (`core/engines/registry.py`, `core/queue/worker/admission.py`) — the
deliberate plugin seam, invisible to the import graph on purpose.

Within ORCA, dependencies also point inward: `commands` may call the domain
modules, but submission, execution, worker-child, and queue policy must never
import `orca.commands`. The import-linter contract protects this boundary.

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
  ┌──────────────────────────────┐      ┌──────────────────────────────┐
  │ engine-workers@.target       │ ───▶ │  Queue worker loop            │
  │ └ queue-worker (ORCA)        │      │  core/queue/worker/loop.py    │
  │ runtime@.target              │      └─────────────┬────────────────┘
  └──────────────────────────────┘                    │ reserve admission slot
                                                       │ spawn child by queue id
                                                       ▼
                                        ┌──────────────────────────────┐
                                        │  Worker child entrypoint       │
                                        │  core/engines/worker_child.py  │
                                        │  --engine <orca|xtb|crest>     │
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
  directory, routes it to ORCA or workflow handling, validates against the
  configured roots, rejects duplicate active entries, writes the queue entry,
  and returns `status: queued`. There is no public direct-execution mode for new
  work.
- **Workers run by queue identity.** A worker spawns the unified child with
  `--queue-root/--queue-id` (plus an `--admission-token`); the child resolves the
  current queue entry itself. The `reaction_dir` field is preserved in the
  queue entry as the downstream contract.
- **A queue generation binds its executable inputs at submission.** Workflow xTB
  and CREST keep content-addressed input snapshots in an exclusively reserved,
  unique namespace for each submission. ORCA creates a visible
  generation directly under the submitted job directory, preserves the
  selected `.inp` and dependency basenames, rewrites supported file references
  to those confined flat copies, and writes raw outputs beside them. New ORCA
  generations have no hidden execution parent or nested
  input layer. Workers verify input and executable content identities instead
  of re-reading mutable source files as the execution contract. Recovery stays
  pinned to the original executable identity, and semantic orbital-input
  references are snapshot-bound across top-level and `%scf` syntax. A mutable
  runtime XYZ seed is accepted only when strict finite atom rows preserve the
  identity-bound atom-label sequence.
- **If no worker is running, work stays pending** in `queue.json` until a worker
  returns. Closing the submission terminal after `status: queued` is safe.

---

## 4. The Shared Engine Abstraction

The single most important architectural piece is that **ORCA, xTB, and
CREST all execute through one common engine runtime.** This is what keeps admission,
child-process management, terminal side effects, and orphan recovery uniform.

### EngineDefinition

`core/engines/definitions.py` defines `EngineDefinition`, a frozen dataclass that
bundles everything the shared runtime needs for an engine:

- `load_config` — engine config loader
- `queue_functions` — runtime roots, queue operations, entry lookup, and PID-file name
- `runner_callbacks` — child runner and child-command builder
- `queue_worker_runner` — directly bound parent-worker callable

`EngineDefinition.build_queue_runtime()` is the canonical bridge from that
declaration to `EngineQueueRuntime`: it installs the engine's queue functions,
PID-file name, exact identity predicate, and queue-entry lookup in one place.
All engines use this runtime directly. The former
`core.queue.internal_engine` module/facade/resolver stack has been removed.

Each engine package exposes an `ENGINE_DEFINITION` constant:

| Engine | Module                                  |
|--------|-----------------------------------------|
| orca   | `orca_auto.orca.engine`                 |
| xtb    | `orca_auto.flow.engines.xtb.engine`     |
| crest  | `orca_auto.flow.engines.crest.engine`   |

The shared parent-worker lifecycle lives under `core.queue.engine`; common
worker execution dependencies live in `core.queue.engine.worker_execution`,
and child entrypoints use `core.queue.engine.child` directly. The xTB engine
definition explicitly owns the workflow-aware runtime-root resolver, while
live child-PID slot protection remains an xTB policy. Publication repair is
shared: `flow/engines/queue_runtime_common.py` owns the sweep and the
pre-reservation gate, and both the xTB and CREST workers install it.
Crash-generation rebind, retry, publication repair, durable engine-process
recovery, cancellation, and terminal replay remain ORCA-owned policies. Do not
reintroduce an engine-local or generic forwarding facade around these canonical
owners.

ORCA's parent `queue.worker` is a composition root, not a policy owner. It wires
the shared engine runtime to `publication_repair`, `cancellation`, `replay`, and
`worker_tracking`; changes to those policies belong in their owning modules.

`core/engines/registry.py` resolves an engine id to its `EngineDefinition` by
importing the module named in the catalog entry and reading `ENGINE_DEFINITION`.
The engine-id → module mapping itself lives in `core/engine_catalog.py`, which
is the only place that declares it.

### Unified child entrypoint

All engine work runs through one entrypoint:

```bash
python -m orca_auto.core.engines.worker_child \
  --engine <orca|xtb|crest> \
  --config <path> \
  --queue-root <path> \
  --queue-id <id> \
  --admission-token <token>
```

The parent worker (`EngineQueueWorker`) reserves an admission slot, spawns this
child, and finalizes the terminal queue result after the child exits. ORCA keeps
its richer domain behavior (state machine, retry, reports) inside
`orca_auto.orca`, while its worker-child entrypoint uses the canonical
`core.queue.engine.child` contract directly.

---

## 5. Admission Control (Shared Concurrency Cap)

`core/admission/` implements machine-wide concurrency limiting so that ORCA
and all internal workflow stages compete for one shared pool.

- The cap is `scheduler.max_active_simulations`. It is **shared across ORCA,
  internal xTB stages, and internal CREST stages.**
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
- **Pending-launch recovery:** the engine catalog persists whether an engine
  uses the ORCA launch gate. A dead same-boot owner whose gated slot is still
  pending and has no engine identity can be cleared with an owner-and-policy
  compare-and-swap: the gate proves the engine could not have executed.
  Direct-launch xTB/CREST and legacy records default to non-gated and retain the
  conservative pending fence because an unrecorded process may exist.

This is why the `active_simulations` line in `queue list` counts only runs that
currently consume a shared slot.

---

## 6. ORCA Engine Internals

`orca_auto.orca` is the canonical ORCA implementation and has the deepest domain
logic. Notable pieces:

- **Input selection and binding:** at submission, ORCA selects the most recently
  modified `*.inp`, snapshots it and its supported file dependencies into one
  visible flat generation, and executes only that generation's bound input.
  Two different source paths with the same basename always fail closed, even
  when their contents match.
- **Generation-local evidence:** raw ORCA inputs, outputs, durable state, and
  reports remain in their verified visible generation. The job root carries
  only the live `job_state.json` until terminal cleanup; `run.lock` continues
  to serialize use of the reusable source directory. A fully closed submission can therefore be
  followed by a new sibling generation without overwriting old raw files. An
  invisible filesystem owner token binds state/report publication, historical
  lookup, and cleanup to the originally submitted directory rather than only
  its reusable pathname or inode number.
- **Optional RAM scratch with durable publication:** when
  `orca.runtime.scratch_root` is configured, an attempt stages only its bound
  flat, basename-relative input closure into a private `/dev/shm` workspace.
  Input bytes are captured once before capacity admission, and root/workspace
  directory descriptors remain pinned through execution and publication; ORCA
  enters the workspace through the pinned descriptor rather than reopening its
  pathname.
  A scratch-root lock admits exactly one workspace, and unresolved or stale
  workspaces are preserved and block new launches until an operator inspects
  them or the tmpfs is reset. The shared admission process record stays
  durable outside scratch; queue, run state, and locks remain in durable
  storage. After the process tree exits, surviving regular files are staged
  and committed back to the inode-pinned generation as one journaled file-set
  transaction; a partial replacement rolls the old set back. Runtime state
  names are reserved, while `*.tmp`/`*.tmp.*` files are discarded. Unknown
  non-temporary outputs are retained instead of using a lossy
  scientific-artifact allowlist. Staged inputs are immutable. Completed
  attempts record `scratch_provenance`; an
  exception or worker shutdown after a committed publication records the same
  evidence in `scratch_publications`, separately from immutable
  execution-snapshot provenance. Launch is rejected unless current
  `MemAvailable` can cover the configured task-memory limit, all free space in
  the scratch tmpfs, and the configured host reserve.
  The scratch workspace and journal implementation is owned by
  `core.engine_scratch`; ORCA contributes only its flat input-dependency scanner,
  canonically owned by `orca.input_references`. ORCA input tokenization, shared
  reference models, and edit operations remain owned by `orca.input_blocks`.
  Workflow xTB/CREST uses the same private workspace and transaction, and its
  input snapshots stay durable and absolute. xTB publishes its
  canonical job-type result set and logs; CREST publishes named retained
  ensembles and logs. Large engine work trees are omitted and removed after the
  committed publication. CREST's own `--scratch` copier remains disabled.
  A one-byte launch gate starts in the final process group first. The worker
  durably records that PID/PGID in the shared admission slot before releasing
  the gate to `exec` ORCA, so a hard parent failure before registration cannot
  leave an unowned calculation. If the parent dies earlier while the durable
  record is still identity-less `pending`, that same gate evidence lets orphan
  recovery reclaim the slot without waiting for a reboot.
  A worker/host crash can lose unpublished tmpfs checkpoints; the ordinary
  durable recovery path then resumes from evidence that was already published.
- **Attempt engine** (`attempt/engine.py`, `attempt/retry.py`,
  `attempt/resume.py`): runs an attempt, parses output, classifies the result,
  and decides whether to retry.
- **Output analysis** (`parser/`, `out_analyzer.py`,
  `output_status.py`, `completion_rules.py`): determines completion by mode —
  TS mode (`OptTS`/`NEB-TS`, requires exactly one imaginary frequency in the
  frequency section after the last final single point energy, plus an IRC
  marker when the route has `IRC`) vs Opt mode (normal termination).
- **Calculation-type retry policy** (`retry_policy.py`):
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
  `MORead` + `%moinp` when a matching non-empty `.gbw` checkpoint exists. An
  existing top-level or `%scf` orbital-input declaration is recognized
  semantically, so recovery never injects a second source. Resumed inputs are
  written as `*.resume.inp` so user input is never mutated.
- **State & reports:** `state_reading.py` owns bounded private-state reads,
  verified generation binding, and public-machine validation; `state.py` owns
  state mutation and artifact publication, while `state_machine.py` applies
  transitions. Completion publishes the common `machine.json` last. Opt,
  OptTS, NEB-TS,
  ScanTS, IRC, and relaxed-scan jobs also get `job_report.html` (`report/`), a
  self-contained visual report assembled by `report/composer.py` from common
  page chrome plus calculation components — scan energy profile (ScanTS and
  plain relaxed scans), CI-NEB path profile plus TS refinement trace (NEB-TS),
  IRC path profile with combined OptTS/Freq sections when the route includes
  them, or optimization convergence trace (Opt/OptTS), retry-recipe chain, and
  vibrational summary. Completed jobs ending on a stationary point also get
  `si_block.md` (`report/si.py`), a copy-paste Supporting Information block
  with energies, thermochemistry, Nimag, and coordinates; IRC routes get a
  summary-only validation block without coordinates. The writer refuses the
  block when the output yields no trustworthy final energy or geometry.
- **Index:** `job_locations/` and `core/indexing` maintain a JSONL
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

`flow/templates.py` defines the three workflow templates:

| Template id            | CLI shortcut       | Purpose                              |
|------------------------|--------------------|--------------------------------------|
| `reaction_ts_search`   | `ts_search`        | Reactant×product TS search           |
| `conformer_screening`  | `conformer_search` | Conformer generation + screening     |
| `scan_ts_search`       | `scan_ts`          | Relaxed-scan TS search               |

A workflow is materialized from a `flow.yaml` manifest (`flow/manifest.py`) in
the submitted directory: each run mints a timestamped generation workspace
(`YYYYMMDD-HHMMSS-<8hex>`, also the workflow id) inside the scaffold, matching
the standalone ORCA execution layout. `scaffold` writes a starter `flow.yaml`
plus the standard XYZ filenames.

Manifest admission is bounded before materialization. `core/config/bounded_yaml.py`
is the canonical direct owner of bounded stable regular-file manifest reads, the
unique-key loader, YAML limits, and the shared error taxonomy. Manifest readers
import the bounded loader directly; config policy and config-error consumers
directly reuse only the symbols they need, without a forwarding facade. The
loader caps a job manifest at 1 MiB, 32 YAML aliases, 10,000 parsed/expanded
nodes, and 64 nesting levels, and rejects cyclic/recursive graphs. Central
geometry limits cap local work at 10,000 atoms and xTB/ORCA Hessian-producing
work at 1,000.

Workflow ORCA task roles are revalidated against the materialized input at
creation, restart, actual pre-submission selection, and completed-result
acceptance. Relaxed scans additionally bind one closed scan coordinate to the
selected geometry at each dynamic stage. `flow/orca_stage_validation.py` is
the canonical owner of those checks; materialization and every lifecycle
consumer depend on it directly, without a forwarding facade. The submitter binds equal durable path
copies to its actual selection and validates the final rewritten bytes before
the execution snapshot writes and identifies those same bytes. Candidate
relative energies and interaction RMSD representative choices are published
only when authoritative selected inputs and final outputs prove uniform route,
non-resource active directives, ordered atom labels, identity-bound
non-geometry dependency content, electronic state, and ORCA version provenance.
Geometry coordinates remain candidate-specific and private dependency pathnames
are canonicalized away. HTML and SI consume that same scientific identity;
resource controls do not affect it. `flow/orca_stage_evidence.py` is the shared
authoritative report/state/input/output reader used by report, SI, and
interaction materialization.

Workflow funnel summaries have a neutral direct owner:
`flow/workflow/stage_summary.py` reads task kinds, counts concatenated-XYZ
frames, and derives CREST/xTB stage details. Report collection, workflow SI,
and phase notifications import it directly, so none depends on another
consumer's private helpers. The summary owner is not a second ORCA evidence
source. `flow/orca_stage_evidence.py` owns authoritative completed-stage
provenance, while `flow/workflow/report_energy_evidence.py` owns
generation-confined raw `.engrad` reads plus final-versus-attempt authority and
annotation detection within the output chain used for non-completed report
rows. Report collection retains completed-evidence acceptance, cross-channel
`.engrad`-versus-output precedence, the annotated-output veto of `.engrad`,
science identity, candidate admission, and relative energy ranking. Separately,
`flow/workflow/report_diagnostics.py` owns failed-stage status gating, canonical
state/report resolution, bounded log diagnostics, and safe details links.
Collection imports the direct evidence owners and never imports rendering; the
evidence readers cannot import collection or rendering. Workflow HTML rendering
depends one way on immutable data from `report_collection.py`.

Workflow restart has three explicit owners. `flow/restart/settings.py` resolves
the manifest and durable workflow state while enforcing scientific invariants;
`flow/restart/stage_ops.py` applies the resolved controls to individual stages
and rematerializes their engine inputs; `flow/restart/mutation.py` applies those
stage operations with restart-directory rollback and the durable workflow
commit. The package entry point supplies the independently resolved settings.
Settings resolution and per-stage mutation are independent siblings, so neither
is exposed through a forwarding facade.

### Supporting Information ownership

`flow/workflow/si/` is a flat package of three modules. `collection.py` reads
durable workflow/stage evidence and composes the selection, RMSD,
interaction-energy, and population rules; `rendering.py` produces Markdown text
without writing files; `publication.py` is the only workflow SI writer and owns
atomic replacement and stale-file cleanup.
The advance loop checkpoints publication before
calling the writer and owns durable retry after an interrupted publication.
The modules do not introduce a second numerical or artifact source of
truth; `workflow_si.md` retains its existing contract. The package `__init__`
exports nothing and is not a facade. An import-linter layers contract enforces
publication → rendering → collection; collection cannot import either upper
owner, and rendering cannot import publication (see `docs/DEVELOPMENT.md`).

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

### Orchestration dependency boundaries

The advance loop passes one `OrchestrationServices` value containing four coarse
outer boundaries: workflow persistence, engine gateways, the clock, and events.
Internal stage views, materializers, lifecycle rules, and stage-runtime helpers
use direct imports instead of being routed through a generic service locator.
This keeps the execution order visible in `advance_phases.py` and prevents one
dependency object from recreating the whole orchestration graph.

Tests replace only those four outer boundaries through the strict helper in
`tests/flow/orchestration_services.py`. A test that needs to isolate an internal
operation patches the owning module explicitly. Unknown service names fail
immediately, so a stale fake cannot silently stop exercising production code.
Import-linter also prevents stage views from depending back on orchestration
wiring.

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
The ORCA-only `scan_ts_search` template uses no engine root: its ORCA stages
are workflow-ordered directories directly under the workspace (`01_scan`,
`02_scan_maximum`, ...).

Their terminal control-plane metadata has one durable source: `job_state.json`.
The internal workers, repair path, index, adapters, and workflow report consume
that state directly and create no duplicate JSON or Markdown report. Report-only
jobs require resubmission.

---

## 8. Persistence & State Files

orca_auto keeps all scheduling, ownership, and public artifacts disk-backed.
Optional ORCA tmpfs scratch is an execution workspace, never a state source.
Concurrency safety comes from file locks (`core/utils/lock.py`) around every
durable mutation. The main on-disk artifacts:

| File                        | Owner            | Purpose                                  |
|-----------------------------|------------------|------------------------------------------|
| `queue.json`                | core/queue       | Durable per-engine queue (source of truth)|
| admission slot file         | core/admission   | Active concurrency slots (machine-wide)  |
| `job_state.json`            | orca (state)     | Per-job attempts + status                |
| `machine.json`              | orca/flow       | Public machine observation               |
| `job_report.html`           | orca (reporting) | Human completion report                 |
| job-location index (JSONL)  | core/indexing    | Where each job's outputs currently live  |
| `workflow.json`             | flow             | Durable workflow payload                 |
| `workflow_report.html`      | flow (report rendering) | Live visual workflow summary        |
| `si_block.md`               | orca (report/si) | Per-structure SI block (paper-ready)     |
| `workflow_si.md`            | flow (si)        | Assembled workflow SI (paper-ready)      |
| workflow registry + journal | flow/registry    | Cross-workflow listing + event history   |

Workflow report evidence and presentation have separate owners. The evidence
owner consumes confined durable state and builds report data; the presentation
owner depends on that data and alone publishes `workflow_report.html`. Machine
observation, notification, and SI consumers stay below the presentation layer.

The workflow journal records semantic workflow/stage transitions and worker
lifecycle boundaries; idle polling cycles are not events. The CLI workflow
worker owns `workflow_worker_state.json` and rewrites that advisory snapshot
only when its semantic summary changes or a bounded heartbeat is due (at most
60 seconds and shorter than the lease). Recent bounded journal reads use the
registry-lock append/commit order and read only a confined file suffix; an
explicit unbounded read still scans the complete history.

The queue entry and tracked job-location record each expose a
frozen set of downstream fields (see REFERENCE.md §11.1) so that `flow` can
consume ORCA results without coupling to ORCA internals.

---

## 9. Notifications

orca_auto delivers one-way outbound notifications only: it posts job and workflow
alerts to Discord and never consumes inbound commands.

`core/messaging/` owns a provider-neutral capability boundary: immutable semantic
`Message` documents (`richtext.py`) plus the notification `MessageChannel`
(`channel.py`). Domain notifiers construct documents without wire markup;
`build_channel` (`registry.py`) resolves the configured channel and fails closed on
unsupported providers. `MessengerConfig` owns the adapter config and rejects unknown
providers.

`DiscordBotChannel` (`discord_bot.py`) renders each `Message` into a Discord embed
(`render_discord.py`) and delivers it over the bot-authenticated Discord API; its
shared HTTP retry/backoff helpers live in `discord_http.py`. All transport and
response-read failures are normalized at this adapter boundary, so notification
failure remains advisory to durable publication.

`core/notifications/` holds the engine-specific notification functions
(`engines.py`). Submission, execution, and terminal adapters bind the relevant
queued/started/finished callbacks directly; ORCA retry notifications are bound by
its execution adapter. Workflow alerts keep per-job ORCA messages but summarize
internal CREST and reaction-path xTB child phases into one message each.

The channel is enabled only when its credentials are complete: Discord requires
`messenger.discord.bot_token` plus `messenger.discord.default_channel_id`.

---

## 10. Configuration

Config is a single YAML file resolved in this order:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

`core/config/schema.py` defines the typed config dataclasses (e.g.
`RetryRuntimeConfig`, `CommonResourceConfig`, `MessengerConfig`) with normalizing
constructors. Notable rules:

- **Linux paths only.** Windows drive paths, `/mnt/<drive>/...`, relative
  executable paths, and `.exe` binaries are rejected. Configured ORCA/xTB/CREST
  executables must be absolute Linux paths to existing executable files.
- `scheduler.max_active_simulations` is the shared admission cap.
- `scheduler.admission_root` is the shared slot-coordination root.
- Divergent engine-scoped scheduler values are rejected so every worker
  observes the same admission root and limit.
- `runs_root` is the single runs root for standalone ORCA jobs,
  workflow workspaces, and internal-engine runs.
- `default_max_retries: 0` disables ORCA retries; any positive value enables the
  calculation-type retry policy, whose per-route caps are recorded in
  `job_state.json`/queue metadata.

---

## 11. Process Supervision (systemd)

Long-running processes are managed through `systemd`. The public service
commands operate these units instead of launching unmanaged workers. Units live
under `systemd/`:

| Unit                                  | Role                                            |
|---------------------------------------|-------------------------------------------------|
| `orca_auto-engine-workers@.target`    | Starts the default engine worker unit           |
| `orca_auto-queue-worker@.service`     | Supervises the ORCA worker                      |
| `orca_auto-workflow-worker@.service`  | Opt-in workflow + internal xTB/CREST workers    |
| `orca_auto-runtime@.target`           | Starts the engine workers                        |

`orca_auto systemd install --user <user> --repo <repo>` renders and enables the
units. Literal percent characters in data paths are escaped, while paths whose
quotes, backslashes, or dollar signs would alter unit parsing are rejected. On
WSL, `systemd` must be enabled in `/etc/wsl.conf`.

The default ORCA worker runs under its own service supervisor, so it can fail or
restart independently of the opt-in workflow supervisor. The opt-in
workflow supervisor starts each of its workers in a separate process session and
spaces initial starts by two seconds. A daemon worker that exits three times
within five minutes opens its supervisor circuit instead of restarting forever.
Each engine queue worker still reconciles durable state at
startup, but idle full-state reconciliation is limited to once per minute while
the light queue/status poll remains at its normal interval. The service retries failures
after 30 seconds and permits at most three unit starts per five-minute window;
clean supervisor exits are not restarted.

`cli_workers.py` plans the selected worker commands and checks for an existing
ORCA worker. `cli_worker_supervision.py` directly owns the process model,
sessions, signals, termination escalation, and restart circuit used to run that
plan; the command module does not re-export those private operations.

Workers bind their resolved package source into their own process environment
at startup. Status reads that provenance with PID/start-tick race checks before
performing a fresh HEAD/reflog and package-tree cleanliness inspection for each
worker; process cwd is not treated as import-source evidence.

---

## 12. CLI Surface

The CLI is argparse-based (`cli.py` → `cli_parsers.py` → `cli_handlers.py`), with
status-aware colorized table rendering (`terminal_table.py`, `activity_*.py`,
`cli_style.py`). The public command surface:

- `init` — create/update shared config
- `scaffold <ts_search|conformer_search|scan_ts> <path>` — write workflow scaffolds
- `run-dir <path>` — durable submission (ORCA or workflow, auto-routed)
- `queue list` / `queue cancel` / `queue list clear` — inspect/maintain the queue
- `service status` / `service restart` — runtime status (via systemd)
- `systemd install` — render and enable units

Engine-specific CLI modules are runtime-only worker entrypoints and are not a
place to add user commands.

---

## 13. Quality Gates

`scripts/check.sh` is the shared local + CI entrypoint: it creates/repairs
`.venv`, installs `.[dev]`, then runs `ruff check`, `ruff format --check`,
`mypy`, `lint-imports`, and the coverage-gated pytest suite. CI additionally runs Gitleaks,
ShellCheck, a Python 3.11/3.12/3.13 matrix,
and a wheel smoke that requires the packaged Python-module inventory to exactly match
`src/orca_auto` with one root `py.typed` marker.

Tests are organized as `tests/core/`, `tests/flow/`, `tests/flow/engines/`,
`tests/integration/`, and top-level ORCA regression tests. The project prefers
behavior-asserting tests (payloads, persisted files, CLI output, state
transitions) over internal delegation tests.

---

## 14. Design Principles Summary

- **Durable submission, supervised execution** — the queue is always the source
  of truth; workers are restartable and stateless between jobs.
- **One engine runtime, many engines** — `EngineDefinition` + the unified child
  entrypoint keep ORCA/xTB/CREST lifecycles uniform while preserving ORCA's
  richer domain behavior.
- **Shared admission cap** — a single machine-wide slot pool bounds total
  concurrency across every engine.
- **Frozen downstream contracts** — `flow` consumes ORCA via a documented field
  contract (`reaction_dir`, job-location records), not internals.
- **Disk-backed, lock-guarded state** — every mutation goes through a file lock;
  crashed owners are reconciled rather than leaking slots.
- **Linux/WSL-first, systemd-supervised** — strict Linux path validation and
  `systemd` units for unattended operation.
