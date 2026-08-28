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

Within workflow orchestration, inject only the outer persistence, engine,
clock, and event boundaries through `OrchestrationServices`. Import internal
stage, materialization, and lifecycle operations directly. Tests must reject
unknown outer-service overrides and patch the owning module when isolating an
internal operation.

Workflow SI is a flat package of three modules — `collection.py`, `publication.py`,
and `rendering.py`. Import them directly; `flow.workflow.si.__init__` exports
nothing and is not a facade. There is no import-linter contract for this package,
so the following is a convention the reviewer enforces, not the build:

- Dependencies run publication → rendering → collection: publication imports
  both siblings, rendering imports collection, collection imports neither.
  Publication is the only file-writing SI owner; rendering stays text-only.

Workflow HTML reporting also uses direct owners rather than a facade:

- `report_collection.py` reads confined, validated workflow/engine evidence and
  derives the immutable report data consumed by HTML, machine observations,
  notifications, and workflow SI.
- `report_rendering.py` imports collection, renders the page, and atomically
  publishes `workflow_report.html`. Collection never imports rendering.

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
- `cli_systemd_units.py` owns unit-role ordering, systemctl queries, and the
  shared unit status model; it reuses the canonical name formatters.
- `cli_systemd_freshness.py` owns read-only worker/checkout freshness evidence
  and depends only on the unit substrate.
- `cli_systemd_status.py` assembles and renders status; `cli_systemd_restart.py`
  owns restart mutation. The command owners do not import each other, and the
  lower evidence modules never import either command owner.

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
- Keep SI collection/rendering free of publication imports. No import-linter
  contract enforces this (see the Workflow SI note above); it is a
  reviewer-enforced convention — do not bypass it with a forwarding module.

## Engine Workers

xTB, CREST, and ORCA all execute through the common engine runtime. Engine-local
packages should expose an `EngineDefinition`; parent workers use
`EngineQueueWorker`, and children use
`python -m orca_auto.core.engines.worker_child --engine <orca|xtb|crest> --config <path> --queue-root <path> --queue-id <id> --admission-token <token>`.
Build parent-worker infrastructure from `EngineDefinition.build_queue_runtime()`
and use the canonical `core.queue.engine` admission, child, lifecycle, worker
execution, and hook contracts directly. The former generic internal-engine
facade no longer exists. Keep workflow-root discovery, publication fencing, and
live child-PID reconciliation as explicit xTB policy. Keep retry,
crash-generation recovery, publication, terminal replay, and state/report
policy inside `orca_auto.orca`. Do not add a forwarding module when the
canonical runtime already owns the operation.

Keep `orca_auto.orca.queue.worker` as the parent-worker composition root.
Queued-publication repair belongs to `queue.publication_repair`, cancellation
to `queue.cancellation`, terminal reconciliation/replay to `queue.replay`, and
job-index/notification tracking to `queue.worker_tracking`.

ORCA-specific state, retry, input selection, reports, and the downstream
`reaction_dir` contract stay in `orca_auto.orca`. The
direct ORCA worker-job `--reaction-dir` mode is not supported.

## Related Docs

- [REFERENCE.md](REFERENCE.md): runtime and behavior reference
