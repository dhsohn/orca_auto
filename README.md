# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)

**English** | [한국어](README(ko).md)

orca_auto is a queue-first interface for ORCA and workflow orchestration on Linux and WSL. xTB and CREST remain part of the runtime, but they are now used internally for workflow stages rather than as standalone public surfaces. It submits work durably, runs it under supervised workers, records per-job state and reports, and organizes completed outputs.

## Docs

- Architecture overview: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ([한국어](docs/ARCHITECTURE(ko).md))
- Quickstart: [docs/QUICKSTART.md](docs/QUICKSTART.md) ([한국어](docs/QUICKSTART(ko).md))
- Runtime and command reference: [docs/REFERENCE.md](docs/REFERENCE.md) ([한국어](docs/REFERENCE(ko).md))
- WSL and `systemd` runtime setup: [systemd/README.md](systemd/README.md) ([한국어](systemd/README(ko).md))
- Package layout and development notes: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) ([한국어](docs/DEVELOPMENT(ko).md))

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
scheduler:
  max_active_simulations: 4

workflow:
  root: /home/user/workflow_runs
  paths:
    xtb_executable: /home/user/bin/xtb-dist/bin/xtb
    crest_executable: /home/user/bin/crest/crest

telegram:
  bot_token: ""
  chat_id: ""
  timeout_seconds: 5.0
  max_attempts: 2
  retry_backoff_seconds: 0.5

orca:
  runtime:
    allowed_root: /home/user/orca_runs
    organized_root: /home/user/orca_outputs
    default_max_retries: 2
  paths:
    orca_executable: /home/user/opt/orca/orca
```

Notes:

- Use Linux paths only; Windows drive paths, `/mnt/<drive>/...`, relative executable paths, and `.exe` binaries are rejected.
- Configured ORCA/xTB/CREST executable paths must point to existing executable Linux binaries. Leave `workflow.paths.xtb_executable` or `workflow.paths.crest_executable` blank only when you intentionally want PATH lookup at runtime.
- `default_max_retries: 2` means `1 initial + 2 retries = 3` total attempts.
- `scheduler.max_active_simulations` is the shared cap across ORCA, internal xTB workflow stages, and internal CREST workflow stages.
- `workflow.root` is the workflow root used by the unified CLI and workflow worker.
- Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and organized outputs live only under `workflow.root/<workflow_id>/internal/<engine>/{runs,outputs}`.
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
orca_auto organize orca --root '/home/user/orca_runs' --apply
orca_auto scan-notify
```

`queue list` prints a compact, terminal-width-aware table; workflow children are grouped
and indented under their parent. The Telegram bot mirrors the same surface (`/list`,
`/cancel`) with inline buttons. For the full command reference — table columns, the
`--watch`/`--json`/`--no-color` flags, color and exit behavior, and the Telegram bot —
see [docs/REFERENCE.md](docs/REFERENCE.md) §7.

## Services

Long-running services (the queue worker and Telegram bot) are managed through `systemd`
only. After `orca_auto.yaml` is configured, enable the combined runtime target once and
let `systemd` keep both running:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
orca_auto service restart
```

If Telegram is not configured yet, the installer enables only the queue worker; run the
same command again after setting `telegram.bot_token` and `telegram.chat_id` to enable the
full runtime target. If you edited files under `systemd/`, run
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
installs `.[dev]`, then runs `ruff check`, `ruff format --check`, `mypy`, and
the coverage-gated pytest suite. Pass pytest selectors directly to the script when you want a narrower
loop, for example `bash scripts/check.sh tests/flow -q`.

CI also runs Gitleaks secret scanning, ShellCheck for `scripts/*.sh`, rendered
systemd unit verification, the Python 3.11/3.12/3.13 check matrix, and a wheel
smoke test that confirms typed-package metadata. These checks exercise the
queue, workflow, parser, notification, and fake-engine integration paths without
requiring a licensed ORCA binary. They do not prove that a local ORCA/OpenMPI
installation is valid, that your site scheduler allows the requested resources,
or that Telegram credentials and network delivery work in your deployment.

To clear local Python/test/tool caches after a large refactor:

```bash
bash scripts/clean_artifacts.sh
```
