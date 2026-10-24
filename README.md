<p align="center">
  <img src="https://raw.githubusercontent.com/dhsohn/orca_auto/v10.2.3/docs/images/banner.svg" alt="ORCA_auto — Submit durably. Execute reliably. Recover explicitly." width="680">
</p>

<p align="center">
  <a href="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml"><img src="https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/dhsohn/orca_auto/releases/latest"><img src="https://img.shields.io/github/v/release/dhsohn/orca_auto" alt="Release"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+"></a>
  <a href="https://github.com/dhsohn/orca_auto/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center"><b>English</b> · <a href="https://github.com/dhsohn/orca_auto/blob/main/README.ko.md">한국어</a></p>

ORCA_auto **queues ORCA calculations and keeps them running after you close the terminal**. On Linux and WSL2, a systemd worker runs the jobs while the command line gives you a view of what is waiting, running or finished.

## After you submit a calculation

- **Manage jobs through a queue.** Submit an input directory; the job is saved to disk and the worker starts it when host resources allow.
- **Inspect progress and results.** Use `orca_auto queue list` to check the queue, then feed `--json` output and each calculation's `machine.json` into analysis scripts.
- **Recover according to the cause.** Worker and host interruptions are distinguished from calculation failures. Failed chemistry calculations are not retried automatically.
- **Receive notifications where you work.** Configure optional [Discord](https://github.com/dhsohn/orca_auto/blob/main/docs/DISCORD_SETUP.md) or [Slack](https://github.com/dhsohn/orca_auto/blob/main/docs/SLACK_SETUP.md) notifications.

## Start your first calculation

Requires **Python 3.11+**, **Linux or WSL2**, and a separately installed **ORCA** engine. Background workers run under **systemd**.

```bash
python -m pip install orca_auto==10.2.3
```

Prepare your environment with the [installation guide](https://github.com/dhsohn/orca_auto/blob/main/docs/INSTALLATION.md), then follow the [quickstart](https://github.com/dhsohn/orca_auto/blob/main/docs/QUICKSTART.md): create a configuration, register the worker, submit an input directory and inspect the result.

## Upgrade an existing installation

Start with the guide for your installed version. The linked release notes explain each migration step.

| Installed version | Guide | Main change |
| --- | --- | --- |
| 10.2.x | [10.2.3](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-1023) | Frequency evidence for `%freq` requests; no state migration |
| 10.1.x | [10.2](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-102) | Expanded queue Detail labels; incomplete final coordinates rejected |
| 10.0.x | [10.1](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-101) | The last frequency section determines frequency evidence |
| 9.x | [10.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-100) | Updated meaning of `completed` |
| 7.x–8.x | [9.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-90), then [10.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-100) | Upgrade in stages |
| 6.x or earlier | [7.0](https://github.com/dhsohn/orca_auto/blob/main/docs/RELEASE.md#upgrading-to-70), then 9.0 and 10.0 | Migrate the older stored formats first |

## Companion research tools

[Chemvas](https://github.com/dhsohn/Chemvas) draws molecules and reaction schemes, ORCA_auto runs and manages calculations, and [Chemleaf](https://github.com/dhsohn/Chemleaf) supports research writing. Each is an independent project. To read ORCA_auto results in another program, start with its [public contracts](https://github.com/dhsohn/orca_auto/blob/main/docs/PUBLIC_CONTRACTS.md).

## Documentation

- **Use and operation:** [Command reference](https://github.com/dhsohn/orca_auto/blob/main/docs/REFERENCE.md) · [systemd services](https://github.com/dhsohn/orca_auto/blob/main/systemd/README.md) · [Support](https://github.com/dhsohn/orca_auto/blob/main/SUPPORT.md)
- **Results and design:** [Public contracts](https://github.com/dhsohn/orca_auto/blob/main/docs/PUBLIC_CONTRACTS.md) · [Architecture](https://github.com/dhsohn/orca_auto/blob/main/docs/ARCHITECTURE.md) · [Validation](https://github.com/dhsohn/orca_auto/blob/main/docs/VALIDATION.md) · [Related tools](https://github.com/dhsohn/orca_auto/blob/main/docs/RELATED_WORK.md)
- **Development and project:** [Development guide](https://github.com/dhsohn/orca_auto/blob/main/docs/DEVELOPMENT.md) · [Contributing](https://github.com/dhsohn/orca_auto/blob/main/CONTRIBUTING.md) · [Roadmap](https://github.com/dhsohn/orca_auto/blob/main/ROADMAP.md) · [Changelog](https://github.com/dhsohn/orca_auto/blob/main/CHANGELOG.md)
- **Citation and policies:** [Citation](https://github.com/dhsohn/orca_auto/blob/main/CITATION.cff) · [Security](https://github.com/dhsohn/orca_auto/blob/main/SECURITY.md) · [Code of Conduct](https://github.com/dhsohn/orca_auto/blob/main/CODE_OF_CONDUCT.md) · [License](https://github.com/dhsohn/orca_auto/blob/main/LICENSE)

Report bugs and suggest improvements through [GitHub Issues](https://github.com/dhsohn/orca_auto/issues). The [support guide](https://github.com/dhsohn/orca_auto/blob/main/SUPPORT.md) lists useful details for calculation reports.

## How this was built

I build ORCA_auto for calculations I return to in chemistry research. I set the requirements, [public behavior](https://github.com/dhsohn/orca_auto/blob/main/docs/PUBLIC_CONTRACTS.md) and validation criteria, and use AI coding agents to write the code.

`make check` runs lint, formatting, type and import-boundary checks, bilingual documentation checks and the full test suite. Changes to engine execution also need a run with real ORCA, assessed against the calculation's evidence—such as normal termination, geometry convergence or frequencies—under the [validation criteria](https://github.com/dhsohn/orca_auto/blob/main/docs/VALIDATION.md).
