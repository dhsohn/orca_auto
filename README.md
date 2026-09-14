<p align="center">
  <img src="https://raw.githubusercontent.com/dhsohn/orca_auto/v6.0.0/docs/images/banner.svg" alt="ORCA_auto — Submit durably. Execute reliably. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dhsohn/orca_auto/blob/v6.0.0/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><b>English</b> · <a href="https://github.com/dhsohn/orca_auto/blob/v6.0.0/README.ko.md">한국어</a></p>

ORCA_auto is a **queue-first runner for ORCA** on Linux/WSL.
It provides a **durable execution layer for AI agents** to delegate
quantum-chemistry calculations through a public CLI, track progress, and
inspect recorded outcomes.

## Built for agent-driven calculations

- **Delegate beyond the session.** A successful submission saves the job to
  disk; a supervised worker executes it independently of the submitting agent
  session.
- **Read structured evidence.** Use `orca_auto queue list --json` and
  `orca_auto service status --json` for status checks. Terminal `machine.json`
  records provide calculation outcomes and artifact receipts for downstream tools.
- **Recover explicitly.** Worker or host interruptions follow verified
  recovery paths. Failed ORCA calculations are not automatically retried.

ORCA_auto manages execution, not chemical judgment. You remain responsible for
input design and scientific validation.

## Get started

Python **3.11+**, Linux/WSL2, and a separately installed ORCA engine are
required. Supervised workers use `systemd`.

```bash
python -m pip install orca_auto==6.0.0
```

- **[Installation details](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/INSTALLATION.md)** — install Core from PyPI in a fresh environment.
- **[Set up workers and submit your first job](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/QUICKSTART.md)** — configure a
  source checkout, start services, and inspect the queue.

**Upgrading?** Keep running calculations on their current environment. Switch
only in an idle window; see the [upgrade guide](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELEASE.md#removing-ts-workflows-in-60).

## In the same chemistry ecosystem

[Chemvas](https://github.com/dhsohn/Chemvas) for chemical drawings,
**ORCA_auto** for calculation execution, and
[LLMdocx](https://github.com/dhsohn/LLMdocx) for research documents are
independent, local-first companion tools. Data handoffs require explicit
conversion—not an automatic end-to-end integration.
[How the tools connect →](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELATED_WORK.md#local-first-companion-tools)

## Documentation

[Command reference](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/REFERENCE.md) · [Runtime contracts](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/PUBLIC_CONTRACTS.md) ·
[Architecture](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/ARCHITECTURE.md) · [Services](https://github.com/dhsohn/orca_auto/blob/v6.0.0/systemd/README.md) ·
[Discord notifications](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/DISCORD_SETUP.md)

[Development](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/DEVELOPMENT.md) · [Validation](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/VALIDATION.md) ·
[Roadmap](https://github.com/dhsohn/orca_auto/blob/v6.0.0/ROADMAP.md) · [Changelog](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CHANGELOG.md)

[Citation](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CITATION.cff) · [Contributing](https://github.com/dhsohn/orca_auto/blob/v6.0.0/CONTRIBUTING.md) ·
[Support](https://github.com/dhsohn/orca_auto/blob/v6.0.0/SUPPORT.md) · [Security](https://github.com/dhsohn/orca_auto/blob/v6.0.0/SECURITY.md)
