# ORCA_auto Architecture

**English** | [한국어](ARCHITECTURE.ko.md)

This document describes the system architecture, package structure, runtime lifecycle, and core subsystems of ORCA_auto.

---

## 1. Core Design Principles

- **Queue-First Asynchronous Execution**: The CLI (`run-dir`) validates input files, writes a durable queue entry (`queue.json`), and returns immediately.
- **Process Supervision**: Background `systemd` worker daemons poll the queue and execute calculations sequentially.
- **File-Based Observability**: Job status, execution logs, and structured outputs are persisted alongside the calculation directory in `machine.json` and Markdown reports.

---

## 2. Package Architecture and Dependency Rules

```text
orca_auto/
├── cli*.py             # Top-level CLI entrypoints and argument parsing
├── core/               # Shared infrastructure (queues, admission, process tracking)
├── orca/               # Canonical ORCA engine implementation (submission, parsing, states)
└── flow/               # Workflow extension (extensions/workflows: conformer search, etc.)
```

- **Unidirectional Layering**: Enforces `flow` → `orca` → `core` strictly via `import-linter`.
- **CLI Isolation**: Core domain packages (`core`, `orca`, `flow`) do not import from top-level CLI modules.
- **Dynamic Engine Loading**: Cross-engine dispatch uses string-based module resolution via `core/engine_catalog.py` rather than static imports.

---

## 3. Runtime Control Flow

```text
[ User / CLI ]
      │  orca_auto run-dir <path>
      ▼
[ Validation & Enqueue ] ──▶ queue.json (Durable disk storage)
                                 │
[ systemd Worker Daemon ] ◀──────┘ (Polling)
      │
      ├─▶ Reserve admission slot (concurrency guard)
      ├─▶ Spawn child process (worker_child)
      │      └─▶ Execute ORCA and monitor output
      ├─▶ Release slot and finalize queue outcome
      └─▶ Dispatch notifications & write machine.json
```

1. **Submission (`run-dir`)**: Scans for the latest `.inp` file, validates dependencies, binds an isolated execution generation, and writes to `queue.json`.
2. **Worker Polling (`EngineQueueWorker`)**: The background daemon claims the next eligible queue entry and checks admission limits.
3. **Child Execution (`worker_child`)**: Runs the engine inside an isolated subprocess to prevent parent daemon pollution.
4. **Finalization**: Verifies produced artifacts, writes `machine.json`, and updates the queue state.

---

## 4. Key Subsystems

### Admission Control (`core/admission/`)
- Limits total concurrent simulations across engines via `scheduler.max_active_simulations`.
- Uses disk-based slot records protected by file locks (`admission_lock`).
- Reconciles stale slots automatically by checking PID liveness.

### Queue & State Lifecycle (`core/queue/`)
- Tracks job states (`queued`, `running`, `completed`, `failed`, `cancelled`) in `queue.json`.
- Prevents concurrent duplicate runs on active directories while allowing clean re-submissions into new generations.

### ORCA Engine Runtime (`orca/`)
- **Input Snapshotting**: Copies inputs and dependencies into isolated generation directories before execution.
- **Convergence Verification (`output_status.py`)**: Parses ORCA output markers to accurately determine success or failure reasons.
- **Checkpoint Resumption**: Reuses existing valid `.gbw` binary files when restarting interrupted runs.

### Workflow Orchestration (`flow/`)
- Automates multi-stage calculations (e.g., CREST conformer generation followed by ORCA DFT optimization).
- Manages state transitions and resume checkpoints through `flow.yaml`.

---

## 5. Summary of Core Modules

| Module Path | Responsibility |
|---|---|
| `core/engines/definitions.py` | Unified `EngineDefinition` interface for engines |
| `core/admission/store.py` | Slot reservation, concurrency limiting, and stale recovery |
| `core/queue/store.py` | Disk queue (`queue.json`) persistence and transactions |
| `orca/submission.py` | Input validation, snapshot binding, and queue submission |
| `orca/output_status.py` | Output convergence analysis and status classification |
| `orca/state.py` | Job state persistence and `machine.json` publication |
| `flow/orchestration/` | Multi-stage workflow lifecycle and journal tracking |
