# orca_auto Development Notes

**English** | [한국어](DEVELOPMENT.ko.md)

This repository now uses a monorepo-style package layout under `src/orca_auto`.

## Canonical Import Rules

- ORCA implementation: `orca_auto.orca.*`
- Shared infrastructure: `orca_auto.core.*`
- Workflow orchestration: `orca_auto.flow.*`
- Engine packages: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

New code, tests, and docs should import from `orca_auto.*`.

The domain packages form enforced layers — `flow` → `orca` → `core` — checked
by import-linter (`lint-imports`, configured in `pyproject.toml` and run by
`scripts/check.sh`, so also by CI). Higher layers may import lower ones; the
reverse fails the build. Cross-layer engine wiring goes through the lazy
string module registries (`core/engines/registry.py`,
`core/queue/worker/admission.py`) instead of imports.

The top-level CLI modules (`cli*.py`, `activity_*.py`, `terminal_table.py`,
`systemd_plan.py`) are the outermost layer: they compose the domain packages,
and a second import-linter contract forbids `core`, `orca` and `flow` from
importing any of them. What command adapters inside the domain packages share
with the CLI lives in `core` instead — `core/terminal.py` owns ANSI styling and
the `error:`/`hint:` output format, and `core/config/discovery.py` owns shared
config and workflow-root resolution from parsed arguments.

Within workflow orchestration, inject only the outer persistence, engine,
clock, and event boundaries through `OrchestrationServices`. Import internal
stage, materialization, and lifecycle operations directly. Tests must reject
unknown outer-service overrides and patch the owning module when isolating an
internal operation.

ORCA structure evidence is independent of human report presentation:

- `orca/evidence.py` owns `OrcaStructureEvidence`, final-output selection,
  cached parsing, route classification, and completed-structure collection.
- `orca/frequencies.py` owns vibrational parsing and mode summaries.
- Workflow stage receipt/provenance verification remains in
  `flow/orca_stage_evidence.py`; selection imports the scientific record
  directly, without passing through the SI renderer.
- `orca/report/si.py` derives SI lint warnings and renders/publishes Markdown;
  `orca/report/frequencies.py` renders vibrational HTML only. Neither is a
  compatibility facade for the read owners. Import-linter enforces these
  evidence/presentation boundaries. Existing RMSD/interaction-energy modules
  are outside this extraction.

`orca/output_status.py` owns optimization verdict markers and the last-line
verdict rule, shared by output analysis, parsed results, and progress cards.
Internal engine submission outcomes are selected by control flow, never by
searching human diagnostic text for status words.

Workflow SI is a flat package of three modules — `collection.py`, `publication.py`,
and `rendering.py`. Import them directly; `flow.workflow.si.__init__` exports
nothing and is not a facade. An import-linter layers contract enforces the
dependency direction:

- Dependencies run publication → rendering → collection: publication imports
  both siblings, rendering imports collection, collection imports neither.
  Publication is the only file-writing SI owner; rendering stays text-only.

Workflow HTML reporting also uses direct owners rather than a facade:

- `report_diagnostics.py` owns failed-stage status gating, canonical verified
  state/report resolution, reason precedence, bounded log tails, and safe
  details links.
- `report_energy_evidence.py` owns size-bounded `.engrad` reads and confined,
  backward-windowed ORCA output-chain scans, including recorded-final authority
  over earlier attempts and annotation detection within that chain. It stays
  below collection and does not decide candidate admission, cross-channel
  `.engrad`-versus-output precedence, or relative-energy comparability.
- `report_collection.py` imports both evidence owners and derives the immutable
  report data consumed by HTML and machine observations. Completed evidence
  acceptance, cross-channel energy-source policy and its annotated-output veto,
  science identity, and ranking remain in collection.
- `report_rendering.py` imports collection, renders the page, and atomically
  publishes `workflow_report.html`. Diagnostics never imports collection or
  rendering, and collection never imports rendering.
- `stage_summary.py` owns workflow task-kind reads, concatenated-XYZ frame
  counting, and CREST/xTB stage details shared by report collection, workflow
  SI, and phase notifications. Those consumers import the owner directly;
  `report_collection.py` does not re-export stage-summary helpers.

ORCA durable-state access has separate read and publication owners:

- `orca/state_reading.py` owns artifact names and paths, normalized state
  interpretation, bounded reads, verified generation binding, and public machine
  observation validation.
- `orca/state.py` owns state mutation and artifact publication and depends one
  way on the read owner for the shared generation and lifecycle gates.

Callers import the concrete owner directly. `state.py` does not re-export read
helpers as a compatibility facade, and `state_reading.py` must not import state
mutation or report-publication modules.

ORCA external file-reference discovery has one direct owner:

- `orca/input_references.py` owns the occurrence cap, supported and rejected
  directive sets, NEB reference context, and the shared scanner.
- `orca/input_blocks.py` owns tokenization, the shared reference model, semantic
  `MOInp`/`MORead` parsing, block/route/geometry syntax, and input edits.

Dependencies run from `input_references` to `input_blocks`. Execution binding,
scratch staging, conformer selection, and restart rematerialization import the
scanner owner directly; `input_blocks` must not re-export scanner policy as a
compatibility facade.

Workflow ORCA stage validation likewise has one direct owner:

- `orca_stage_validation.py` owns durable task-kind, route-field, route-role,
  selected-input, and relaxed-scan validation.
- `_orca_stage_materialization.py` owns rendering, payload assembly, and
  confined geometry/Hessian/input writes, and depends one way on validation.

Creation, restart, submission, stage-runtime, and evidence consumers import the
validation owner directly. Validation must not import materialization, and the
materializer must not re-export validation functions as a compatibility facade.

The systemd CLI also uses direct owners with a one-way dependency graph:

- `systemd_plan.py` owns canonical unit-name formatting and install planning.
- `cli_systemd_units.py` owns unit-role ordering, systemctl invocation, the
  shared command runner, target-user resolution, the boot-selection mode
  cascade, and the shared unit status model; it reuses the canonical name
  formatters. Every systemctl invocation in the family goes through its
  runners, while each caller keeps its own error policy.
- `cli_systemd_freshness.py` owns read-only worker/checkout freshness evidence
  and depends only on the unit substrate.
- `cli_systemd_status.py` assembles and renders status; `cli_systemd_restart.py`
  owns restart mutation; `cli_systemd_apply.py` owns install-plan application.
  The command owners do not import each other, and the lower evidence modules
  never import any command owner.

Import these owners directly in code and tests. Do not add a status facade or
re-export freshness, unit, or restart operations through a sibling module.

Foreground queue-worker wiring follows the same direct-owner rule:

- `cli_workers.py` owns app selection, command/spec assembly, existing-ORCA-worker
  conflict checks, and the command adapter.
- `cli_worker_supervision.py` owns the `WorkerSpec` process model, subprocess
  sessions, signal handling, termination escalation, and the restart circuit.

Workflow restart also separates resolution from mutation:

- `flow/restart/settings.py` resolves `flow.yaml` and durable workflow state into
  validated effective restart settings, including science-invariance checks.
- `flow/restart/stage_ops.py` applies those settings to stage/task/enqueue payloads,
  rematerializes engine inputs, and resets restartable stages.
- `flow/restart/mutation.py` applies stage operations across the workflow and
  owns the restart-directory transaction and durable workflow commit; the
  package entry point supplies settings resolved by its independent sibling.

Import and patch the concrete owner. Do not forward private supervision or
restart-mutation symbols through the assembly or settings modules. Restart
settings and stage mutation remain independent siblings composed only at the
package workflow boundary.

## Current Package Layout

```text
<repo_root>/
├── src/
│   └── orca_auto/
│       ├── core/
│       ├── flow/
│       │   └── engines/
│       │       ├── xtb/
│       │       └── crest/
│       └── orca/
├── tests/
│   ├── core/
│   ├── flow/
│   ├── integration/
│   └── flow/engines/
└── docs/
```

## Canonical CLI Form

User-facing docs should standardize on these command forms:

- `orca_auto queue ...`
- `orca_auto run-dir <path>`
- `orca_auto init`
- `orca_auto scaffold <ts_search|conformer_search|scan_ts> <path>`

Long-running services are not part of the public CLI surface. Users should run
them only through the `systemd/` units.

Engine-specific CLI modules are runtime-only worker entrypoints. Do not add new
user-facing commands there.

Flow internals are not public CLI modules. Keep examples on `orca_auto ...`
and avoid module-level `python -m` examples for flow internals.

## Practical Import Map

Use these patterns in new code:

```python
from orca_auto.cli import main
from orca_auto.orca.commands.run_inp import cmd_run_inp
from orca_auto.core.engines import EngineDefinition, EngineQueueWorker

from orca_auto.core.queue import enqueue
from orca_auto.core.admission import reserve_slot
from orca_auto.core.indexing import get_job_location
```

Keep imports under `orca_auto.*`; avoid top-level aliases or alternate shims.

## Test Layout

- `tests/flow/`: flow unit and contract tests
- `tests/flow/engines/`: internal xTB/CREST engine tests
- `tests/integration/`: in-repo integration smoke tests
- `tests/core/`: shared infrastructure tests
- top-level `tests/test_*.py`: ORCA-focused regression tests

Common commands:

```bash
make test
bash scripts/check.sh tests/flow -q
bash scripts/check.sh tests/integration -q
make structural-tests
bash scripts/clean_artifacts.sh
```

## Quality Gates

- `scripts/check.sh` is the shared local and CI entrypoint. It creates or
  repairs `.venv`, installs `.[dev]`, then runs `ruff check`,
  `ruff format --check`, `mypy`, `lint-imports`, and pytest with the coverage gate.
- Ruff explicitly enables import sorting (`I`) and Bugbear (`B`) alongside the
  default Pyflakes/pycodestyle safety rules.
- `ruff format` is the canonical formatter and is gated via
  `ruff format --check`. Line length (`line-length = 100`) is shaped by the
  formatter, so `E501` is intentionally left out of the lint `select`.
- Mypy remains broadly non-strict at `[tool.mypy]`; strict-style options are
  intentionally scoped to override-listed modules that have already been
  hardened. Expand that override list only when the full check still passes, and
  move strict options to `[tool.mypy]` only after the full `src` + `tests` tree
  passes the equivalent strict flags.

## Test Coupling Policy

Prefer tests that assert observable behavior: returned payloads, persisted
files, CLI output, state transitions, process commands, and public facade
contracts. Internal delegation tests such as `delegates_to`, `uses_*_helper`,
`forwards_*`, and `reexports_*` should be kept only when they protect an
intentional public facade or plugin boundary.

Use `make structural-tests` before large refactors to list likely
implementation-coupled tests. Treat it as an audit report, not a failure gate.

## Package Policy

- `orca_auto.orca` is the only implementation source of truth
- All supported package imports live under `src/orca_auto`
- If a new feature requires code changes in ORCA logic, make them under `src/orca_auto/orca`
- Shared engine definitions, queue workers, child entrypoints, artifacts, and
  registry helpers live under `orca_auto.core.engines`
- Internal xTB/CREST implementations live under `orca_auto.flow.engines`
- Keep top-level alias packages, console-script aliases, and alternate runtime
  readers out of the codebase
- Keep `orca_auto.orca.commands` as an adapter layer. Domain execution,
  submission, worker-child, and queue modules must not import it.
- Keep SI collection/rendering free of publication imports. The Workflow SI
  layers contract enforces this direction; do not bypass it with a forwarding
  module.

## Engine Workers

xTB, CREST, and ORCA all execute through the common engine runtime. Engine-local
packages should expose an `EngineDefinition`; parent workers use
`EngineQueueWorker`, and children use
`python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`.
Build parent-worker infrastructure from `EngineDefinition.build_queue_runtime()`
and use the canonical `core.queue.engine` admission, child, lifecycle, worker
execution, and hook contracts directly. The former generic internal-engine
facade no longer exists. Keep workflow-root discovery, publication fencing, and
live child-PID reconciliation as explicit xTB policy. Keep crash-generation recovery, publication, terminal replay, and state/report
policy inside `orca_auto.orca`. Do not add a forwarding module when the
canonical runtime already owns the operation.

Keep `orca_auto.orca.queue.worker` as the parent-worker composition root.
Queued-publication repair belongs to `queue.publication_repair`, cancellation
to `queue.cancellation`, terminal reconciliation/replay to `queue.replay`, and
job-index/notification tracking to `queue.worker_tracking`.

ORCA-specific state, input selection, reports, and the downstream
`reaction_dir` contract stay in `orca_auto.orca`. The
direct ORCA worker-job `--reaction-dir` mode is not supported.

## Related Docs

- [REFERENCE.md](REFERENCE.md): runtime and behavior reference
