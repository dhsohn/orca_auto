<p align="center">
  <img src="docs/images/banner.svg" alt="ORCA_auto — Submit durably. Supervise every run. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><b>English</b> · <a href="README.ko.md">한국어</a></p>

ORCA_auto runs **ORCA calculations and CREST→xTB→ORCA workflows** on Linux/WSL.
Submit work through the CLI, follow it in a durable queue, and inspect the
recorded state, recovery decisions, and calculation reports. ORCA input design
and chemical judgment stay with you.

## Part of a local-first chemistry workflow

ORCA_auto is a companion to [Chemvas](https://github.com/dhsohn/Chemvas) and
[LLMdocx](https://github.com/dhsohn/LLMdocx): three independent tools for
drawing chemistry, running calculations, and working with research documents.

| Stage | Tool | Role |
| --- | --- | --- |
| Design | [Chemvas](https://github.com/dhsohn/Chemvas) | Editable chemical drawings, atom mapping, and calculation handoffs. |
| Execute | **ORCA_auto** | Durable calculation queues, supervised workers, explicit recovery, and reports. |
| Write | [LLMdocx](https://github.com/dhsohn/LLMdocx) | A local document workspace with executable blocks and calculation-result imports. |

The tools share a versioned `machine.json` observation envelope while keeping
their own input and output contracts. Connecting them requires explicit
conversion: Chemvas handoffs must become ORCA inputs or `flow.yaml` workflows,
and calculation results must be packaged in
[LLMdocx's results-bundle format](https://github.com/dhsohn/LLMdocx/blob/main/docs/RESULTS_BUNDLE_V1.md).
These conversions are not built into ORCA_auto.

## Submit durably. Supervise every run. Recover explicitly.

- **Submit durably.** A successful submission records queued work. You can close
  the submitting terminal while the worker handles execution.
- **Supervise every run.** `systemd` workers run standalone ORCA jobs and
  multi-stage reaction or conformer workflows, with queue and service status
  available from the CLI.
- **Recover explicitly.** Worker or host interruptions use verified recovery
  paths. ORCA calculation failures are terminal after one attempt; failure
  reasons remain available for inspection and deliberate resubmission.

[Public contracts](docs/PUBLIC_CONTRACTS.md) describe recovery and reporting
boundaries; [project scope](docs/RELATED_WORK.md) explains when this runtime fits.

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
| **messenger** | one-way Discord job/workflow notifications | [DISCORD_SETUP](docs/DISCORD_SETUP.md) |

## Services, testing, and full docs

- Supervised runtime (`systemd`, WSL/Linux) → [systemd/README.md](systemd/README.md)
- `make check` runs Ruff, format checks, mypy, import-linter, and the coverage-gated pytest suite.
  Real-engine ORCA runs and validation
  boundaries are recorded in → [docs/VALIDATION.md](docs/VALIDATION.md)
- Docs index: [ARCHITECTURE](docs/ARCHITECTURE.md) · [REFERENCE](docs/REFERENCE.md) ·
  [PUBLIC_CONTRACTS](docs/PUBLIC_CONTRACTS.md) · [RELATED_WORK](docs/RELATED_WORK.md) ·
  [DEVELOPMENT](docs/DEVELOPMENT.md) · [ROADMAP](ROADMAP.md)
- [Citation](CITATION.cff) · [Support](SUPPORT.md) · [Security](SECURITY.md)
