# Architecture and Design Principles

**English** | [한국어](ARCHITECTURE.ko.md)

ORCA_auto is a queue runner and execution supervisor designed for durable execution and observable monitoring of standalone ORCA quantum chemistry calculations on Linux and WSL.

---

## 1. Core Principles

1. **Durable Queueing**: Submissions are committed atomically to disk. Calculation state is preserved across terminal disconnects and host reboots.
2. **Generation Isolation**: Resubmitting within a job directory creates a fresh, isolated generation directory instead of overwriting prior attempts.
3. **Explicit Recovery**: Calculation failures are triaged and permanently recorded. ORCA_auto never modifies inputs or blindly retries failed quantum calculations.
4. **Authoritative Source of Truth**: On-disk persistent JSON files (`job_state.json`, `queue.json`) serve as the source of truth. The SQLite activity index is a projection that can be rebuilt deterministically from disk at any time.

---

## 2. Layering and Boundaries

Dependencies flow strictly in one direction: **`CLI / UI` → `orca` (domain) → `core` (infrastructure)**. The domain and core layers never import CLI code.

```mermaid
graph TD
    CLI["CLI Layer (cli*.py, activity/)"]
    ORCA["ORCA Domain (orca/)"]
    CORE["Core Infrastructure (core/)"]

    CLI --> ORCA
    CLI --> CORE
    ORCA --> CORE
```

| Component | Responsibility |
| :--- | :--- |
| **`cli*.py`, `activity/`** | Command parsing, terminal formatting, queue querying, service status, and job cancellation interfaces |
| **`orca/`** | ORCA input (`.inp`) parsing, resource extraction, execution workspace setup, output log analysis, convergence verification, and result reporting (`machine.json`) |
| **`core/`** | Disk queue store, concurrency admission slots, process supervision, systemd integration, and filesystem locks |

> **ORCA_auto 7.0 Structure**: Workflows, conformer scaffolds, and xTB/CREST engines (`flow/`) were retired in 7.0. The engine catalog contains only standalone `orca`, significantly simplifying the runtime architecture.

---

## 3. Submission & Execution Lifecycle

### 1. Submission
- `orca_auto run-dir <PATH>` reads the newest eligible `.inp` and resource directives (`%pal`, `%maxcore`).
- `orca/submission.py` constructs input snapshots, persists the queue entry atomically, and returns immediately.

### 2. Dequeue & Admission
- The background resident worker polls the queue for pending jobs.
- When an eligible job is found, the worker checks available execution slots (`scheduler.max_active_simulations`) and host memory capacity (when RAM Scratch is enabled).
- If resources are sufficient, the worker claims the slot and launches the calculation in an isolated generation workspace. If memory is temporarily constrained, the job is deferred in `waiting for resources` state without failing.

### 3. Supervision & Clean Exit
- The worker tracks child process status and guarantees clean shutdown upon external signals (`SIGTERM`).
- Interrupted or failed executions retain their specific failure causes in both the queue entry and generation state.

### 4. Convergence & Publication
- Upon calculation exit, `orca/out_analyzer.py` verifies termination banners and scans output lines for error or convergence failures (ignoring comments and input echoes).
- A verified observation payload (`machine.json` adhering to the v1 envelope contract) and human-readable HTML/SI reports are published.

### Worker ownership and child execution

`OrcaQueueWorker` owns ORCA cancellation, shutdown, recovery and its replay state.
Its common base owns process supervision, admission and the PID-file lifecycle.
The worker composes its typed dependencies once; tests can substitute process
creation and sleep directly. The parent entry point is
`python -m orca_auto.orca.commands.queue --config …`; the child is
`python -m orca_auto.orca.commands.worker_child --config … --queue-root …
--queue-id … [--admission-token …]`. The parent constructs `EngineQueueRuntime`
directly from the concrete ORCA configuration and queue-entry types, including
the keyword-only `expected_entry` comparison when claiming a selected generation.

Cancellation observations reuse unchanged queue snapshots. Terminal notification
dispatch has a durable claim and bounded background sends; notification delivery
is best effort and does not retain execution admission slots.

The worker CLI loads config, checks the PID file, then constructs and runs the
ORCA worker directly. `EngineQueueRuntime` owns root selection, queue lookup and
admission preview; it has no child-start or terminal policy callbacks.
`OrcaQueueWorker` in `queue/worker.py` owns admission-slot attach, terminal
marking, cancellation, shutdown and orphan reconciliation as methods;
`queue/replay.py` is only the replay engine (work items, strict finish, the
reconcile pipeline and generation owners) and takes its state explicitly, and
`queue/run_state_replay.py` synthesizes terminal `job_state.json` under
`run.lock`. These paths call concrete adapters with the selected entry and task
identity. Terminal replay must finish before admission release. RUNNING-row
reconciliation is worker-owned: a submission never sweeps the queue and recovers
only its own directory's dead row when no worker pid is live.

The ORCA child directly resolves its queue entry, recovers a crashed generation,
waits for parent admission handoff, and runs that generation. The parent retains
final admission-release ownership on success, shutdown and exceptions. Validated
inputs, resources and queue identity form one `RunExecutionContext`, which is
passed directly into execution without reconstructing CLI arguments or installing
empty lifecycle callbacks.

---

## 4. Operational Architecture

- **SQLite Activity Projection**: High-performance querying is provided by a rebuildable SQLite index, avoiding recursive disk scans for routine commands. The projection keys location rows by job id; `job_locations.json` itself is rebuildable from the run states on disk with `index rebuild`, and `--refresh` persists unindexed runs through the same rebuild.
- **Scratch Operator Surface**: `orca_auto scratch list` and `scratch clear` inspect and remove non-live RAM-scratch workspaces; one stale, unverifiable or invalid-manifest workspace otherwise blocks every later scratch launch (fail-closed).
- **Prepared Wheel Runtimes**: For production servers, ORCA_auto can be deployed as an immutable, offline wheel installation to eliminate risks associated with running directly out of mutable development checkouts ([docs/RUNTIME.md](RUNTIME.md)).
- **Historical Data Protection**: Retired workflow directories from previous versions are protected as read-only to ensure historical calculations are preserved without risk of accidental overwrite.
