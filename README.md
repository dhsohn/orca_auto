# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)

orca_auto is a queue-first interface for ORCA and workflow orchestration on Linux and WSL. xTB and CREST remain part of the runtime, but they are now used internally for workflow stages rather than as standalone public surfaces. It submits work durably, runs it under supervised workers, records per-job state and reports, and organizes completed outputs.

## Docs

- Quickstart: [docs/QUICKSTART.md](docs/QUICKSTART.md)
- Runtime and command reference: [docs/REFERENCE.md](docs/REFERENCE.md)
- WSL and `systemd` runtime setup: [systemd/README.md](systemd/README.md)
- Package layout and development notes: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)

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
orca_auto queue list clear
orca_auto queue cancel <target>
orca_auto service status
orca_auto service restart
orca_auto organize orca --root '/home/user/orca_runs' --apply
orca_auto scan-notify
```

`queue list` prints a compact table with `Status`, `Name`, `Detail`, `ID`, and `Elapsed`
columns that adapt to the terminal width (long values are truncated with `...`).
Workflow child simulations stay grouped under their parent workflow with indentation.
By default, only ORCA child jobs are expanded in the combined text view; internal xTB/CREST
workflow children stay hidden unless you ask for them with filters or `--json`.
Use `orca_auto queue list clear` to prune completed, failed, and cancelled entries from
the unified list. The Telegram bot uses the same `/list` table layout (minus the `ID`
column, so each row fits on one line on mobile) and supports the same cleanup via
`/list clear`.
The `active_simulations` line counts only simulations that currently consume the shared
`scheduler.max_active_simulations` slots.

CLI table output is colorized by status when stdout is a terminal; color is disabled
automatically when piped or when `NO_COLOR` is set, and can be forced off with
`--no-color` (e.g. `orca_auto --no-color queue list`). `orca_auto --version` prints the
installed version, and running `orca_auto` with no command prints help. Errors and
recovery hints are written to stderr. The `queue cancel`, `run-dir`, and `service status`
outputs colorize status fields the same way.

`orca_auto queue list --watch` continuously refreshes the list until interrupted
(`--interval` sets the refresh seconds, default 2.0). `orca_auto service status --json`
emits machine-readable output for scripting.

The Telegram bot supports `/cancel <target>` with confirmation via inline buttons before cancelling.
`/list` follows the table with an actions message carrying a per-activity cancel button
for each active item plus refresh and "clear finished" buttons, so cancellation, refresh,
and pruning completed/failed/cancelled entries (the same as `/list clear`) are each one tap
(the cancel button still routes through the confirmation step); when more than eight
activities are cancellable the actions message notes how many are shown, and executing a
cancel or clear auto-refreshes the list.

Long-running services are managed through `systemd` only. After `orca_auto.yaml`
is configured, enable the combined runtime target once and let `systemd` start
both the worker and the bot automatically:

```bash
cd <repo_root>
orca_auto systemd install --user "$(whoami)" --repo "$(pwd)"
orca_auto service status
```

If Telegram is not configured yet, the installer enables only the queue worker.
Run the same command again after setting `telegram.bot_token` and
`telegram.chat_id` to enable the full runtime target.

Restart examples:

```bash
orca_auto service restart
```

If you edited files under `systemd/`, run `sudo systemctl daemon-reload` before restarting.

If you want only the worker managed automatically, enable
`orca_auto-queue-worker@$(whoami)` instead.

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
