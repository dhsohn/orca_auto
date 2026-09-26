<p align="center">
  <img src="https://raw.githubusercontent.com/dhsohn/orca_auto/v8.0.1/docs/images/banner.svg" alt="ORCA_auto — Submit durably. Execute reliably. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dhsohn/orca_auto/blob/v8.0.1/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><b>English</b> · <a href="https://github.com/dhsohn/orca_auto/blob/v8.0.1/README.ko.md">한국어</a></p>

ORCA_auto is a **queue-based runner for ORCA** on Linux/WSL.
It provides background execution, queue scheduling, and structured result tracking for ORCA calculations and downstream tools.

## Key Features

- **Background execution.** Submissions are committed to disk and run under systemd workers independently of the submitting terminal session.
- **Structured status & inspection.** Query job progress and system status via `orca_auto queue list --json` and `orca_auto service status --json`. Terminal `machine.json` records provide structured outcomes and artifact receipts for downstream tools and scripts.
- **Explicit recovery.** Worker or host interruptions follow explicit, verified recovery paths without automatic retries of failed chemistry runs.

## Getting started

Python **3.11+**, Linux/WSL2, and a separately installed ORCA engine are
required. Workers are managed with `systemd`.

```bash
python -m pip install orca_auto==8.0.1
```

- **[Installation details](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/INSTALLATION.md)** — install the standalone ORCA package.
- **[Quickstart guide](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/QUICKSTART.md)** — configure settings, start background services, and submit your first calculation.

For upgrades from 7.x, see the [upgrade guide](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/RELEASE.md#upgrading-to-80).
For 6.x or earlier, complete the [7.0 migration](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/RELEASE.md#upgrading-to-70) first, then follow the 8.0 upgrade guide.

## Chemistry ecosystem

- [Chemvas](https://github.com/dhsohn/Chemvas) for drawing chemical structures,
- **ORCA_auto** for calculation execution and queue management, and
- [LLMdocx](https://github.com/dhsohn/LLMdocx) for drafting research documents.

These are independent, local-first companion tools that connect through standard formats (`machine.json`, results bundles, and input files).
[How the tools connect →](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/RELATED_WORK.md#local-first-companion-tools)

## Documentation

[Command reference](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/REFERENCE.md) · [Public contracts](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/PUBLIC_CONTRACTS.md) ·
[Architecture](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/ARCHITECTURE.md) · [systemd services](https://github.com/dhsohn/orca_auto/blob/v8.0.1/systemd/README.md) ·
[Discord notifications](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/DISCORD_SETUP.md)

[Development guide](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/DEVELOPMENT.md) · [Validation](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/VALIDATION.md) ·
[Roadmap](https://github.com/dhsohn/orca_auto/blob/v8.0.1/ROADMAP.md) · [Changelog](https://github.com/dhsohn/orca_auto/blob/v8.0.1/CHANGELOG.md)

[Citation](https://github.com/dhsohn/orca_auto/blob/v8.0.1/CITATION.cff) · [Contributing](https://github.com/dhsohn/orca_auto/blob/v8.0.1/CONTRIBUTING.md) ·
[Support](https://github.com/dhsohn/orca_auto/blob/v8.0.1/SUPPORT.md) · [Security](https://github.com/dhsohn/orca_auto/blob/v8.0.1/SECURITY.md) · [Code of Conduct](https://github.com/dhsohn/orca_auto/blob/v8.0.1/CODE_OF_CONDUCT.md)

## How this was built

I'm a chemist, not a programmer. AI coding agents write the code in this repository.
I decide what ORCA_auto should do, keep its public behavior written down in the
[public contracts](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/PUBLIC_CONTRACTS.md),
and set the checks a change must pass before it merges.

I don't review the code line by line, so a change is accepted on evidence, not on an
agent's report that it works:

- `make check` runs lint, formatting, type checks, import-boundary checks, a bilingual
  documentation check and the full test suite with coverage. CI runs the same gate and
  validates emitted `machine.json` files against the shared
  [machine-contracts](https://github.com/dhsohn/machine-contracts) validator.
- A change to public behavior updates the contract document in the same change.
- A change to how ORCA_auto runs the engine also needs a bounded run with the real
  ORCA engine, judged by the calculation's own evidence such as termination, geometry
  convergence or frequencies ([validation](https://github.com/dhsohn/orca_auto/blob/v8.0.1/docs/VALIDATION.md)).
- High-impact changes, such as recovery, queue state and result correctness, get an
  independent adversarial review from a separate agent.
