# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dhsohn/orca_auto)](https://github.com/dhsohn/orca_auto/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | WSL](https://img.shields.io/badge/platform-Linux%20%7C%20WSL-lightgrey.svg)](docs/REFERENCE.md#4-required-environment)
[![Typed: py.typed](https://img.shields.io/badge/typed-py.typed-informational.svg)](src/orca_auto/py.typed)

**English** | [한국어](README.ko.md)

orca_auto is a queue-first runner for **standalone ORCA and CREST→xTB→ORCA
workflows** on Linux/WSL. It submits work durably,
runs it under supervised `systemd` workers, and records per-job state, recovery,
and reports — so you always know which calculation failed and what is safe to do next.

## Statement of need

Computational chemistry outgrows one-shot engine commands and ad-hoc shell loops:
you need durable submission, supervised execution, explicit recovery, and an
auditable record of failures. orca_auto covers the CLI / queue / report / retry
contracts for repeated ORCA calculations, transition-state searches, and
reaction or conformer workflows — without adopting a general workflow platform,
and without replacing chemical judgment or ORCA input design.

## Quickstart (standalone ORCA)

```bash
# 1. install
bash scripts/bootstrap_wsl.sh && source .venv/bin/activate

# 2. configure — set runs_root and orca.paths.orca_executable
orca_auto init

# 3. start supervised workers (once)
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"

# 4. drop an ORCA .inp into a job dir under runs_root, then submit
orca_auto run-dir '/home/you/runs/my_rxn'

# 5. watch it
orca_auto queue list --engine orca
```

Config keys, path rules, and the config search order →
[docs/QUICKSTART.md](docs/QUICKSTART.md) and [docs/REFERENCE.md](docs/REFERENCE.md).

## What it runs

| Capability | Use it for | Details |
|---|---|---|
| **standalone ORCA** | durable submit/recovery of single ORCA jobs, TS searches | [REFERENCE](docs/REFERENCE.md) |
| **workflow** | CREST→xTB→ORCA conformer / reaction pipelines | [ARCHITECTURE](docs/ARCHITECTURE.md) |
| **messenger** | remote submit/inspect + notifications (Telegram or Discord) | [DISCORD_SETUP](docs/DISCORD_SETUP.md) |

## Services, testing, and full docs

- Supervised runtime (`systemd`, WSL/Linux) → [systemd/README.md](systemd/README.md)
- `make test` runs ruff, mypy, import-linter, and the coverage-gated pytest suite.
  Real-engine ORCA runs and validation
  boundaries are recorded in → [docs/VALIDATION.md](docs/VALIDATION.md)
- Docs index: [ARCHITECTURE](docs/ARCHITECTURE.md) · [REFERENCE](docs/REFERENCE.md) ·
  [PUBLIC_CONTRACTS](docs/PUBLIC_CONTRACTS.md) · [RELATED_WORK](docs/RELATED_WORK.md) ·
  [DEVELOPMENT](docs/DEVELOPMENT.md) · [ROADMAP](ROADMAP.md)
- [Citation](CITATION.cff) · [Support](SUPPORT.md) · [Security](SECURITY.md)
