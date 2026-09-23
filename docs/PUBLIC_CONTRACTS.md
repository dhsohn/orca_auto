# Public Contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

This document defines the stable public contracts, CLI interfaces, configuration schema, and output artifacts guaranteed by ORCA_auto.

---

## 1. Contract Principles

- **Semantic Versioning**: All interfaces documented here adhere to semantic versioning. Breaking changes require a major version bump.
- **Additive JSON Extension**: JSON payloads (`--json`, `machine.json`) may receive backward-compatible new fields over time. Integration scripts should ignore unknown fields.
- **Internal Evolution**: Internal module structures, private helper routines, and process plumbing remain free to evolve as long as documented contract behaviors are preserved.

---

## 2. Runtime Environment Contract

| Dimension | Supported Environment | Unsupported / Rejected |
|---|---|---|
| **OS** | Native Linux (Ubuntu 20.04+), WSL2 on Windows | Native Windows, macOS |
| **Python** | Python 3.11+ | Python <= 3.10 |
| **Path Scheme** | Absolute Linux/POSIX paths (`/home/...`) | Windows drive letters (`C:\...`), relative paths |
| **Binaries** | Linux ELF executable binaries | Windows `.exe` binaries |
| **Service Supervisor** | systemd 247+ | Legacy init systems |

---

## 3. Public CLI Interface

The official CLI entrypoint is `orca_auto`. Scripts and automated tools should use `--json`.

| Command | Contract Behavior |
|---|---|
| `orca_auto init` | Interactively generate or update `config/orca_auto.yaml`. |
| `orca_auto run-dir <path>` | Validate input files, enqueue the job (`status: queued`), and return immediately. |
| `orca_auto queue list` | Query queue state and active simulations in table or JSON format. |
| `orca_auto queue list clear` | Prune completed, failed, or cancelled jobs from the active queue list. |
| `orca_auto queue cancel <target>` | Safely cancel queued or active calculations. |
| `orca_auto scaffold conformer_search <path>` | Scaffold standard `flow.yaml` template for conformer screening workflows. |
| `orca_auto index prune` | Clean up tracked location records whose directories no longer exist on disk. |
| `orca_auto service status` | Report systemd runtime targets and worker daemon status/freshness. |
| `orca_auto service restart` | Safely restart worker daemons (refused while calculations are active). |

---

## 4. Configuration Schema (`config/orca_auto.yaml`)

Configuration files accept only recognized keys and fail closed on invalid syntax or unknown fields.

- `runs_root` (required): Absolute Linux path to the top-level calculation directory.
- `scheduler.max_active_simulations`: Global limit on concurrent active simulations (default: 4).
- `scheduler.admission_root`: Path for admission locks and slot tracking (default: `<runs_root>/.admission`).
- `resources.max_cores_per_task`: Default CPU cores per task.
- `resources.max_memory_gb_per_task`: Default RAM (GB) per task.
- `orca.paths.orca_executable`: Absolute path to ORCA binary.
- `orca.runtime.scratch_root`: Private tmpfs RAM scratch workspace (optional).
- `orca.runtime.scratch_min_free_gb`: Minimum free memory required before launching in scratch.
- `messenger.provider`: Notification provider (`discord`).
- `messenger.discord.bot_token` / `default_channel_id`: Discord bot token and notification channel ID.

---

## 5. Queue and Lifecycle Contracts

- **Asynchronous Queueing**: `run-dir` guarantees durable enqueueing before returning. Users can safely disconnect terminal sessions.
- **Generation Isolation**: Every execution creates an isolated subdirectory (`YYYYMMDD-HHMMSS-<hex>`), preserving exact input snapshots and output artifacts.
- **Duplicate Prevention**: Submitting an active job directory is rejected to prevent race conditions. Re-submitting a finished directory spawns a clean new generation.
- **No Uncontrolled Retries**: Calculation failures preserve the root cause and output logs without unwanted automatic reruns.

---

## 6. Output Artifacts and Machine Metadata

Completed calculation generations contain:

### `machine.json` (Public Observation Envelope)
The standard machine-readable contract for automated downstream analysis:
- `contract`: Schema identifier (`factory/machine-observation:1`).
- `operation`: Operation kind (`chemistry/orca-run` or `chemistry/workflow`).
- `lifecycle`: Final outcome (`succeeded`, `failed`, `cancelled`) and wall times.
- `payload.results`: Electronic energies, vibrational modes, and chemical metadata.
- `artifacts`: Manifest of generated files with SHA-256 receipts.

### Supporting Information (`si_block.md`)
- For stationary point calculations, renders a clean Markdown block containing final energies, ZPE/thermal corrections, and Cartesian coordinates.

---

## 7. Workflow Contracts (`flow.yaml`)

- The conformer screening workflow (`conformer_screening`) systematically sequences CREST search, xTB screening, and ORCA DFT optimization.
- Execution checkpoints are tracked in `flow.yaml`, enabling safe resumption from the last completed stage after interruptions.

---

## 8. Non-Contract Surfaces

The following are internal implementation details subject to change without notice:
- Subprocess entrypoints like `orca_auto queue worker`.
- Internal state files like `job_state.json`.
- Terminal ANSI color formatting and line wrapping.
