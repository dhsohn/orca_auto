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

- **`cli*.py`, `activity/`**: CLI entry points, formatting, and high-level queue queries.
- **`orca/`**: ORCA domain logic (input parsing, execution snapshot creation, output parsing, convergence checks, and report publication).
- **`core/`**: Shared infrastructure (disk queue storage, concurrency slot admission, process supervision, and systemd integration).

> **Import Direction Rule**:
> Code dependencies flow strictly in one direction: **`orca` → `core`**. Domain and core modules must never import from the outer CLI modules. This is enforced by `import-linter` in CI.

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

The `machine.json` conformance tests use the `machine-contracts` commit pinned
in `.github/workflows/ci.yml`. Clone `https://github.com/dhsohn/machine-contracts.git`
to `~/machine_contracts`, or set `FACTORY_MACHINE_CONTRACT_REPO` to an existing
clone containing that commit. The tests read the pinned commit, not the clone's
working tree, and fail if the clone, commit or `jsonschema` dependency is missing.
CI and release checks provide the clone; `make check` installs `jsonschema` with
the development dependencies. Fetch the clone when advancing the CI pin.

- **Unit & Integration Tests**: Tests use lightweight fake ORCA binaries and isolated temporary fixtures (`tmp_path`). A licensed ORCA installation is not required to run the test suite.
- **Shared Fixtures**: `tests/conftest.py` provides the fake ORCA executable, `AppConfig`/`orca_auto.yaml`, queue-entry and run-state fixtures and the plain builders behind them; new tests take these instead of rebuilding them.
- **Markers**: `os.fsync`/`os.fdatasync` are no-ops in every test unless it is marked `@pytest.mark.real_fsync`; `@pytest.mark.slow` marks the tests that stage the package in an isolated interpreter.
- **Docs Parity**: `make check` runs `scripts/check_docs_parity.py`, which fails when an `X.md`/`X.ko.md` pair drifts in heading levels, tables, fenced code blocks or relative links; prose may differ.
- **Real-Engine Acceptance**: If you modify engine execution or scientific output parsing behavior, record a bounded real-engine run according to [VALIDATION.md](VALIDATION.md).

---

## 4. Release and Runtime References

- For versioning and publication details, see [RELEASE.md](RELEASE.md).
- For immutable wheel runtime deployments, see [RUNTIME.md](RUNTIME.md).
