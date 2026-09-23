# ORCA_auto Development Guide

**English** | [한국어](DEVELOPMENT.ko.md)

This repository contains two coordinated distributions:
- Core distribution: `src/orca_auto`
- Workflows extension: `extensions/workflows/src/orca_auto/flow`

Both packages install under the shared `orca_auto.*` namespace.

---

## 1. Package Layers and Import Rules

- ORCA Engine Implementation: `orca_auto.orca.*`
- Shared Platform Infrastructure: `orca_auto.core.*`
- Workflow Orchestration: `orca_auto.flow.*`
- Auxiliary Stage Engines: `orca_auto.flow.engines.xtb.*`, `orca_auto.flow.engines.crest.*`

### Architecture Constraints
- **Unidirectional Layering**: Enforces `flow` → `orca` → `core` strictly via `python scripts/check_imports.py` (checked in CI via `import-linter`).
- **CLI Isolation**: Top-level CLI modules (`cli*.py`, `activity/`, `terminal_table.py`) compose domain packages. Domain packages never import from top-level CLI modules.
- **Dynamic Engine Dispatch**: Cross-engine resolution uses string-based module resolution via `core/engine_catalog.py` to prevent static cycle coupling.

---

## 2. Development Setup

Install both packages in editable mode with development dependencies:

```bash
python -m pip install -e '.[dev]' -e ./extensions/workflows
```

Core-only mode supports all standalone ORCA execution, queues, and systemd workers. The workflows extension is required for conformer screening and multi-stage runs.

---

## 3. Directory Layout

```text
<repo_root>/
├── src/
│   └── orca_auto/
│       ├── core/          # Shared queue, admission, process infrastructure
│       └── orca/          # Canonical ORCA engine implementation
├── extensions/
│   └── workflows/
│       ├── pyproject.toml
│       └── src/orca_auto/flow/  # Conformer screening and workflow engine
├── tests/
│   ├── core/
│   ├── flow/
│   └── integration/
└── docs/
```

---

## 4. Testing and Quality Gates

Run full test and lint verification mirroring CI:

```bash
# Complete verification (Ruff, Mypy, Import-Linter, Pytest)
make test

# Focused test runs
bash scripts/check.sh tests/flow -q
bash scripts/check.sh tests/integration -q

# Clean temporary test artifacts
bash scripts/clean_artifacts.sh
```

### Quality Tooling
- `ruff check` and `ruff format`: Lints and formats code (standard line length: 100).
- `mypy`: Strict typing enforced across the `orca_auto` package.
- `import-linter`: Enforces module boundaries and dependency contracts.

---

## 5. Related Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): System architecture and design principles
- [PUBLIC_CONTRACTS.md](PUBLIC_CONTRACTS.md): Stable interfaces and contract guarantees
- [REFERENCE.md](REFERENCE.md): CLI and configuration reference
