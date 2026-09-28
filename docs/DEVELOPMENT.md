# Developer Guide

**English** | [한국어](DEVELOPMENT.ko.md)

Local development environment setup, testing, and contribution workflow for ORCA_auto.

---

## 1. Development Environment Setup

Create an isolated Python 3.11+ virtual environment and install development dependencies:

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install package in editable mode with development tools
pip install --upgrade pip
pip install -e '.[dev]'
```

---

## 2. Codebase Layout and Architecture Boundaries

`src/orca_auto` is the single source root, structured into three distinct layers:

- **`cli*.py`, `activity/`, `activity_*.py`, `terminal*.py`**: CLI entry points, formatting, and high-level queue queries. The systemd commands are top-level modules too: `cli_systemd_*.py` over `systemd_plan.py`.
- **`orca/`**: ORCA domain logic (input parsing, execution snapshot creation, output parsing, convergence checks, and report publication).
- **`core/`**: Shared infrastructure (disk queue storage, admission slots, process supervision, RAM scratch, configuration and filesystem locks).

> **Import Direction Rule**:
> Code dependencies flow strictly in one direction: **`orca` → `core`**. Domain and core modules must never import from the outer CLI modules. This is enforced by `import-linter` (`[tool.importlinter]` in `pyproject.toml`, run by `scripts/check_imports.py` in `make check` and CI).

Section 3 of [ARCHITECTURE](ARCHITECTURE.md) is the ownership map: every durable file with its one writer and its readers, each action's call path, the invariants with the tests that enforce them, and the glossary. Read it before a change that writes a durable file or moves a step of an action.

---

## 3. Testing and Verification

Before opening a pull request, run the full verification gate locally:

```bash
# Run linters (Ruff), type checking (mypy), import checks, and full pytest suite
make check

# Verify package build, sdist, and isolated wheel installation
make check-packages

# Run end-to-end smoke tests using the bundled fake ORCA engine
bash examples/fake_orca_smoke/run.sh
```

`make check` creates or repairs the repository `.venv` itself and runs from a
minimal `PATH` such as `/usr/bin:/bin`. An existing usable `.venv` needs no
other interpreter. To create one, it takes the first Python 3.11+ with the
`venv` module from `python`, `python3.13`, `python3.12`, `python3.11` and
`python3` on `PATH`, then from `~/.local/bin`, `~/miniconda3/bin`,
`~/anaconda3/bin`, `/usr/local/bin`, `/opt/homebrew/bin` and `/opt/conda/bin`.
An older interpreter is skipped, and the gate fails listing what it rejected
when none qualifies. Set `PYTHON_BIN=/path/to/python3.11` to choose the
interpreter explicitly. Before linting, the gate also checks that `orca_auto`
is imported from this checkout's `src/` and stops otherwise, for example when
`PYTHONPATH` points at another tree; unset it or recreate `.venv`.

The `machine.json` conformance tests use the `machine-contracts` commit pinned
in `.github/workflows/ci.yml`. Clone `https://github.com/dhsohn/machine-contracts.git`
to `~/machine_contracts`, or set `FACTORY_MACHINE_CONTRACT_REPO` to an existing
clone containing that commit. The tests read the pinned commit, not the clone's
working tree, and fail if the clone, commit or `jsonschema` dependency is missing.
CI and release checks provide the clone; `make check` installs `jsonschema` with
the development dependencies. Fetch the clone when advancing the CI pin.

- **Unit & Integration Tests**: Tests use lightweight fake ORCA binaries and isolated temporary fixtures (`tmp_path`). A licensed ORCA installation is not required to run the test suite.
- **Test Layout**: Tests mirror `src/orca_auto`. A module's tests are named after it and sit in the directory of its package: `orca_auto/core/<pkg>/` in `tests/core/<pkg>/`, `orca_auto/orca/<pkg>/` in `tests/orca/<pkg>/`, a module directly under `core/` or `orca/` in `tests/core/` or `tests/orca/`, `orca_auto/activity/` in `tests/activity/`, and the top-level CLI and presentation modules in `tests/cli/` (the systemd commands in `tests/cli/systemd/`). For example `orca_auto/orca/queue/adapter.py` is tested by `tests/orca/queue/test_adapter.py`; the queue worker's tests are split by concern into `tests/orca/queue/test_worker_*.py`. `tests/integration/` holds flows that cross layers, `tests/tooling/` the checks of the repository scripts, git hooks, packaging and release metadata, and `tests/contracts/` the goldens and rule pins. Helper modules shared across directories (`conftest.py`, `*_helpers.py`) stay at the `tests/` root; fixtures shared by one directory live in its `conftest.py`. Test directories have no `__init__.py`: `--import-mode=importlib` (in `pytest.ini`) lets two directories hold test files with the same name, such as `tests/core/queue/test_store.py` and `tests/core/admission/test_store.py`.
- **Shared Fixtures**: `tests/conftest.py` provides the fake ORCA executable, `AppConfig`/`orca_auto.yaml`, queue-entry and run-state fixtures and the plain builders behind them; new tests take these instead of rebuilding them. Every test runs with config discovery isolated (`ORCA_AUTO_CONFIG` cleared, `HOME` moved to an empty directory), so no test or child it starts reads a live `~/orca_auto` config. `fake_shm` moves the `/dev/shm` confinement of RAM scratch onto `tmp_path`, and `claim_next_entry` claims a row through the worker's own `dequeue_next_entry`. `make_run_context` builds a `RunExecutionContext` without a submitted snapshot for tests that replace the runner, its `run` or its launch; `bound_run_context` builds the worker child's context for an input bound the way `run-dir` binds it; and `make_orca_runner` builds an `OrcaRunner` that pins its executable by content, with snapshot verification and admission callbacks that do nothing. `tests/orca_output_helpers.py` holds the synthetic ORCA inputs and outputs (optimization, NEB-TS, relaxed scan, IRC, single point and SI) that the report tests share.
- **Markers**: `os.fsync`/`os.fdatasync` are no-ops in every test unless it is marked `@pytest.mark.real_fsync`; `@pytest.mark.slow` marks the tests that stage the package in an isolated interpreter.
- **Contract Goldens**: `tests/contracts` pins every public on-disk file, the `--json` and plain-text CLI documents, the argparse surface, the rendered systemd units, the published reports of every report kind (`job_report.html` and `si_block.md` bytes, `machine.json` and `execution_provenance.json` key trees under `golden/reports/`), the notification messages sent by the worker parent and by its children, and the order of durable writes and notification dispatches across both (`effect_log.py`, active in children through `tests/contracts/sitecustomize` only while `ORCA_AUTO_TEST_EFFECT_LOG` is set; there it also replaces every outbound channel with a recording one). Scenarios run real worker children against a fake ORCA and compare normalized output with `tests/contracts/golden/`. `ORCA_AUTO_REGEN_GOLDENS=1` rewrites the goldens instead of comparing; it is off by default.
- **Rule Pins**: `tests/contracts/test_rule_pins_*.py` record the outcome of each rule that is consolidated into one owner, so any outcome a consolidation changes shows up as a table diff: queue generation identity and writer fences, requeue fields, process-owner liveness, `/proc/<pid>/stat` parsing, admission root and limit resolution, the child's admission slot outcome, terminal replay supersession and the output analyzer verdicts over `tests/contracts/pins/out_corpus`. The tables live in `tests/contracts/pins/` and regenerate with the same variable.
- **Ownership Guards**: `tests/core/queue/test_ownership_guards.py` walks the package's AST and fails when a writer of a durable file (`queue.json`, `job_state.json`, `admission_slots.json`, `job_locations.json`, `machine.json` and the reports, the PID file, the snapshot intents) or a notification dispatch is reached from outside its listed owners. A change that adds or moves a writer updates the guard's owner table, with the reason for any new owner, and the ARCHITECTURE ownership map in the same commit.
- **Docs Parity**: `make check` runs `scripts/check_docs_parity.py`, which fails when an `X.md`/`X.ko.md` pair drifts in heading levels, tables, fenced code blocks or relative links; prose may differ.
- **Real-Engine Acceptance**: If you modify engine execution or scientific output parsing behavior, record a bounded real-engine run according to [VALIDATION.md](VALIDATION.md). The real-ORCA cases in `tests/integration/test_orca_worker_smoke.py` run only when `ORCA_REAL_EXECUTABLE` names an ORCA executable and are skipped otherwise.

---

## 4. Moving Code and Golden Fixtures

A refactor keeps behavior, and the contract goldens are the proof:

- A move never shares a commit with a logic edit.
- No forwarding module or alias is left at an old path; every import is updated.
- A move PR attaches `git diff -M --stat` and an AST-equality report of the moved top-level definitions.
- A golden diff in a refactor PR means the PR changed behavior and blocks the merge. Only a PR that deliberately changes a public contract regenerates goldens, and it justifies each changed file.
- Rule pins follow the same rule. When a consolidation moves a function that a pin calls, the pin changes only the import line that names the new owner; its table and corpus files stay unchanged.

```bash
ORCA_AUTO_REGEN_GOLDENS=1 .venv/bin/python -m pytest tests/contracts -q
git diff --stat tests/contracts/golden tests/contracts/pins
```

---

## 5. Release and Runtime References

- For versioning and publication details, see [RELEASE.md](RELEASE.md).
- For immutable wheel runtime deployments, see [RUNTIME.md](RUNTIME.md).
