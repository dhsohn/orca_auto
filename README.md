<p align="center">
  <img src="docs/images/banner.svg" alt="ORCA_auto — Submit durably. Execute reliably. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dhsohn/orca_auto/blob/v7.0.1/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><b>English</b> · <a href="https://github.com/dhsohn/orca_auto/blob/v7.0.1/README.ko.md">한국어</a></p>

ORCA_auto is a **queue-based runner for ORCA** on Linux/WSL.
It provides reliable background execution, queue scheduling, and structured result tracking for ORCA calculations and downstream tools.

## Key Features

- **Reliable background execution.** Submissions are saved safely to disk and executed by systemd workers independently of the submitting terminal session.
- **Structured status & inspection.** Query job progress and system status via `orca_auto queue list --json` and `orca_auto service status --json`. Terminal `machine.json` records provide structured outcomes and artifact receipts for downstream tools and scripts.
- **Predictable recovery.** Worker or host interruptions follow explicit, verified recovery paths without unwanted automatic retries of failed chemistry runs.

## Getting started

Python **3.11+**, Linux/WSL2, and a separately installed ORCA engine are
required. Workers are managed with `systemd`.

```bash
python -m pip install orca_auto==7.0.1
```

- **[Installation details](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/INSTALLATION.md)** — install the standalone ORCA package.
- **[Quickstart guide](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/QUICKSTART.md)** — configure settings, start background services, and submit your first calculation.

For upgrades from 6.x, see the [upgrade guide](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/RELEASE.md#upgrading-to-70).

## Chemistry ecosystem

- [Chemvas](https://github.com/dhsohn/Chemvas) for drawing chemical structures,
- **ORCA_auto** for calculation execution and queue management, and
- [LLMdocx](https://github.com/dhsohn/LLMdocx) for drafting research documents.

These are independent, local-first companion tools that connect through standard formats (`machine.json`, results bundles, and input files).
[How the tools connect →](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/RELATED_WORK.md#local-first-companion-tools)

## Documentation

[Command reference](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/REFERENCE.md) · [Public contracts](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/PUBLIC_CONTRACTS.md) ·
[Architecture](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/ARCHITECTURE.md) · [systemd services](https://github.com/dhsohn/orca_auto/blob/v7.0.1/systemd/README.md) ·
[Discord notifications](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/DISCORD_SETUP.md)

[Development guide](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/DEVELOPMENT.md) · [Validation](https://github.com/dhsohn/orca_auto/blob/v7.0.1/docs/VALIDATION.md) ·
[Roadmap](https://github.com/dhsohn/orca_auto/blob/v7.0.1/ROADMAP.md) · [Changelog](https://github.com/dhsohn/orca_auto/blob/v7.0.1/CHANGELOG.md)

[Citation](https://github.com/dhsohn/orca_auto/blob/v7.0.1/CITATION.cff) · [Contributing](https://github.com/dhsohn/orca_auto/blob/v7.0.1/CONTRIBUTING.md) ·
[Support](https://github.com/dhsohn/orca_auto/blob/v7.0.1/SUPPORT.md) · [Security](https://github.com/dhsohn/orca_auto/blob/v7.0.1/SECURITY.md) · [Code of Conduct](https://github.com/dhsohn/orca_auto/blob/main/CODE_OF_CONDUCT.md)
