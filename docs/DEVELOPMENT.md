# Developer Guide

**English** | [한국어](DEVELOPMENT.ko.md)

This guide covers setting up a local development environment, testing, and contributing to ORCA_auto.

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

`src/orca_auto` is the single source root. Following the retirement of workflows in 7.0, all modules reside under one unified package:

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

- **Unit & Integration Tests**: Tests use lightweight fake ORCA binaries and isolated temporary fixtures (`tmp_path`). You do not need a commercial ORCA installation to run the test suite.
- **Real-Engine Acceptance**: If you modify engine execution or scientific output parsing behavior, record a bounded real-engine run according to [VALIDATION.md](VALIDATION.md).

---

## 4. Release and Runtime References

- For versioning and publication details, see [RELEASE.md](RELEASE.md).
- For immutable wheel runtime deployments, see [RUNTIME.md](RUNTIME.md).
