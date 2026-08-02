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
and `rendering.py`. Import them directly; `flow.workflow.si.__init__` is empty and
is not a facade. There is no import-linter contract for this package, so the
following is a convention the reviewer enforces, not the build:

- Dependencies run publication → collection → rendering. Publication is the only
  file-writing SI owner; rendering stays text-only.

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
- Keep SI evidence/science/rendering free of publication imports. This direction
  is enforced in `pyproject.toml`; do not bypass it with a forwarding module.

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
