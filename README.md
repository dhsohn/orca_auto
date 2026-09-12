<p align="center">
  <img src="docs/images/banner.svg" alt="ORCA_auto — Submit. Monitor. Recover." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><b>English</b> · <a href="README.ko.md">한국어</a></p>

ORCA_auto **runs and monitors your ORCA calculations** on Linux/WSL.
Submit calculations to a durable queue, follow their progress, and inspect
recorded results and recovery decisions. Input design and chemical judgment
stay with you.

## Submit. Monitor. Recover.

- **Submit and close the terminal.** A successful submission saves the job to
  disk; a supervised worker handles execution.
- **See what happened.** Check queue status from the CLI and inspect saved
  calculation reports and failure reasons.
- **Recover with evidence.** Worker or host interruptions follow verified
  recovery paths. Failed ORCA calculations are not automatically retried.

## Get started

Python **3.11+**, Linux/WSL2, and a separately installed ORCA engine are
required. Supervised workers use `systemd`.

- **[Install ORCA_auto](docs/INSTALLATION.md)** — install the core package from
  the GitHub release assets.
- **[Set up workers and submit your first job](docs/QUICKSTART.md)** — configure a
  source checkout, start services, and inspect the queue.

**Upgrading?** Keep running calculations on their current environment. Switch
only in an idle window; see the [upgrade guide](docs/RELEASE.md#moving-from-the-monolithic-4x-installation).

## In the same chemistry ecosystem

[Chemvas](https://github.com/dhsohn/Chemvas) for chemical drawings,
**ORCA_auto** for calculation execution, and
[LLMdocx](https://github.com/dhsohn/LLMdocx) for research documents are
independent, local-first companion tools. Data handoffs require explicit
conversion—not an automatic end-to-end integration.
[How the tools connect →](docs/RELATED_WORK.md#local-first-companion-tools)

## Documentation

[Command reference](docs/REFERENCE.md) · [Runtime contracts](docs/PUBLIC_CONTRACTS.md) ·
[Architecture](docs/ARCHITECTURE.md) · [Services](systemd/README.md) ·
[Discord notifications](docs/DISCORD_SETUP.md)

[Development](docs/DEVELOPMENT.md) · [Validation](docs/VALIDATION.md) ·
[Roadmap](ROADMAP.md) · [Changelog](CHANGELOG.md)

[Citation](CITATION.cff) · [Contributing](CONTRIBUTING.md) ·
[Support](SUPPORT.md) · [Security](SECURITY.md)
