# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | WSL](https://img.shields.io/badge/platform-Linux%20%7C%20WSL-lightgrey.svg)](docs/REFERENCE.md#4-required-environment)
[![Typed: py.typed](https://img.shields.io/badge/typed-py.typed-informational.svg)](src/orca_auto/py.typed)

**English** | [한국어](README.ko.md)

orca_auto is a queue-first interface for ORCA and workflow orchestration on Linux and WSL. It submits work durably, runs it under supervised workers, and records per-job state and reports.

## Statement of need

Computational chemistry projects often outgrow one-shot ORCA commands and ad hoc shell loops. Users need durable submission, supervised execution, restart-aware queue state, consistent job reports, and an explicit record of which calculation failed and what next action is safe. ORCA remains the electronic-structure engine; orca_auto adds the Linux/WSL runtime and observability layer around ORCA-centered workflows.

The project is intended for researchers running repeated ORCA calculations, transition-state searches, and reaction or conformer workflows who want auditable state and recovery behavior without adopting a general workflow platform. It focuses on CLI, configuration, queue, report, and retry contracts rather than replacing chemical judgment, site scheduler policy, or ORCA input design. See [docs/RELATED_WORK.md](docs/RELATED_WORK.md) for scope and ecosystem positioning.

## Docs

- Architecture overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ([한국어](docs/ARCHITECTURE.ko.md))
- Quickstart: [docs/QUICKSTART.md](docs/QUICKSTART.md) ([한국어](docs/QUICKSTART.ko.md))
- Runtime and command reference: [docs/REFERENCE.md](docs/REFERENCE.md) ([한국어](docs/REFERENCE.ko.md))
- Supported public contracts: [docs/PUBLIC_CONTRACTS.md](docs/PUBLIC_CONTRACTS.md) ([한국어](docs/PUBLIC_CONTRACTS.ko.md))
- Roadmap: [ROADMAP.md](ROADMAP.md)
- WSL and `systemd` runtime setup: [systemd/README.md](systemd/README.md) ([한국어](systemd/README.ko.md))
- Package layout and development notes: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) ([한국어](docs/DEVELOPMENT.ko.md))
- Related work and project scope: [docs/RELATED_WORK.md](docs/RELATED_WORK.md)
- Validation and testing boundaries: [docs/VALIDATION.md](docs/VALIDATION.md)
- Release process: [docs/RELEASE.md](docs/RELEASE.md)
- Contributing and PR workflow: [CONTRIBUTING.md](CONTRIBUTING.md)
- Release history: [CHANGELOG.md](CHANGELOG.md)
- Executable fake ORCA smoke example: [examples/fake_orca_smoke/README.md](examples/fake_orca_smoke/README.md)

## Install

Requirements:

- Python 3.11+
- Linux or WSL2
- ORCA installed at an absolute Linux path if you use ORCA
- xTB and CREST installed at absolute Linux paths if you use workflow stages that depend on them

Setup:

```bash
cd <repo_root>
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
```

`bootstrap_wsl.sh` creates `.venv`, installs the Python package/CLI, and seeds
`config/orca_auto.yaml` from the example template when needed. It does not
install or start the systemd runtime units; do that after configuration with
`orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"`.
If you do not activate the virtual environment, you can still run the installed CLI directly as `.venv/bin/orca_auto ...`.

## Configure

Create or update `orca_auto.yaml`:

```bash
orca_auto init
```

Config search order:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

Minimal example:

```yaml
runs_root: /home/user/runs

scheduler:
  max_active_simulations: 4

workflow:
  paths:
    xtb_executable: /home/user/bin/xtb-dist/bin/xtb
    crest_executable: /home/user/bin/crest/crest

messenger:
  provider: telegram  # telegram | discord
  telegram:
    bot_token: ""
    chat_id: ""
    allowed_user_ids: [] # required for group-chat controls
    timeout_seconds: 5.0
    max_attempts: 2
    retry_backoff_seconds: 0.5
  discord:
    bot_token: ""
    channel_ids: ["123456789012345678"]       # inbound command allowlist
    default_channel_id: "123456789012345678" # notifications and card actions
    allowed_user_ids: ["234567890123456789"]  # required operator allowlist

orca:
  runtime:
    default_max_retries: 2
  paths:
    orca_executable: /home/user/opt/orca/orca
```

Notes:

- Use Linux paths only; Windows drive paths, `/mnt/<drive>/...`, relative executable paths, and `.exe` binaries are rejected.
- Configured ORCA/xTB/CREST executable paths must point to existing executable Linux binaries. Leave `workflow.paths.xtb_executable` or `workflow.paths.crest_executable` blank only when you intentionally want PATH lookup at runtime.
- `default_max_retries: 0` disables ORCA retries; any positive value enables the
  calculation-type retry policy, capped by ORCA route type.
- `scheduler.max_active_simulations` is the shared cap across ORCA, internal xTB workflow stages, and internal CREST workflow stages.
- Everything lives under the single runs root (`runs_root`): standalone ORCA jobs and workflow workspaces sit side by side in it, and the shared admission directory defaults to `<runs_root>/.admission`.
- Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and outputs live only under `<runs_root>/<workflow_id>/<NN_engine>` (`01_crest`, `02_xtb`, `03_orca`).
- Discord interactivity requires a separate orca_auto Discord application/bot with
  Message Content Intent enabled. Do not share the `ollama_bot` token between two
  gateway processes.
- Follow [docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md) for the bot invite, channel IDs,
  permissions, service startup, and command verification.
- The full template lives at [config/orca_auto.yaml.example](config/orca_auto.yaml.example).

## User Commands

User-facing submission, inspection, and maintenance commands use `orca_auto ...`.

```bash
# create/update shared config
orca_auto init

# create raw input scaffolds when they help
orca_auto scaffold ts_search '/home/user/workflow_inputs/rxn_001'
orca_auto scaffold conformer_search '/home/user/workflow_inputs/conf_001'

# submit work
orca_auto run-dir '/home/user/orca_runs/sample_rxn'
orca_auto run-dir '/home/user/workflow_inputs/reaction_case'

# inspect and maintain
orca_auto queue list --engine orca
orca_auto queue list clear      # prune completed/failed/cancelled
orca_auto queue cancel <target>
orca_auto service status
orca_auto service restart
orca_auto scan-notify
orca_auto bot run              # foreground Telegram/Discord gateway
```

`queue list` prints a compact, terminal-width-aware table; workflow children are grouped
and indented under their parent. The selected bot mirrors the same application surface
(Telegram `/list`; Discord `!list`, with matching cancel/help commands) and uses
provider-native buttons. For the full command
reference — table columns, the `--watch`/`--json`/`--no-color` flags, color and exit
behavior, and the messenger bot —
see [docs/REFERENCE.md](docs/REFERENCE.md) §7.

## Services

Long-running services (the queue worker and selected messenger bot) are managed through `systemd`
only. After `orca_auto.yaml` is configured, enable the combined runtime target once and
let `systemd` keep both running:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
orca_auto service restart
```

If the selected provider is not configured for interactive operation, the installer
enables only the queue worker. Telegram needs a token and chat ID; Discord needs a bot
token, an inbound command channel, and an operator user ID. After completing the provider
config, rerun the same command to enable the full runtime target. If you edited files under
`systemd/`, run
`sudo systemctl daemon-reload` before restarting. See
[systemd/README.md](systemd/README.md) for the full runtime setup.

## Runtime Notes

- `run-dir` enqueues work durably; workers perform execution.
- ORCA workers launch queue children by queue identity, so the durable
  `queue.json` entry remains the source of truth while the public
  `reaction_dir` contract is preserved.
- If no worker is running, queued jobs remain pending until one returns.
- ORCA selects the most recently modified `.inp` when execution starts.
- When retrying or resuming an interrupted ORCA run, orca_auto uses a matching
  non-empty `.gbw` file by generating a restart input with `MORead` and `%moinp`.
- Completed ORCA runs write state and report files such as `job_state.json`, `job_report.json`, and `job_report.md`.
- Use the `systemd` assets in [systemd/README.md](systemd/README.md) for unattended WSL or Linux execution.

## Testing

```bash
make test
```

`make test` runs `scripts/check.sh`, which creates or repairs `.venv`,
installs `.[dev]`, then runs `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, and
the coverage-gated pytest suite. Pass pytest selectors directly to the script when you want a narrower
loop, for example `bash scripts/check.sh tests/flow -q`.

CI also runs Gitleaks secret scanning, ShellCheck for `scripts/*.sh`, rendered
systemd unit verification, the Python 3.11/3.12/3.13 check matrix, and a wheel
smoke test that confirms typed-package metadata. These checks exercise the
queue, workflow, parser, notification, and fake-engine integration paths without
requiring a licensed ORCA binary. They do not prove that a local ORCA/OpenMPI
installation is valid, that your site scheduler allows the requested resources,
or that messenger credentials and network delivery work in your deployment.

To clear local Python/test/tool caches after a large refactor:

```bash
bash scripts/clean_artifacts.sh
```

## Citation, support, and security

- Citation metadata: [CITATION.cff](CITATION.cff)
- Support policy and issue triage boundaries: [SUPPORT.md](SUPPORT.md)
- Security reporting and sensitive-data guidelines: [SECURITY.md](SECURITY.md)
