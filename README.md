# orca_auto

[![CI](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/dhsohn/orca_auto/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/dhsohn/orca_auto)](https://github.com/dhsohn/orca_auto/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform: Linux | WSL](https://img.shields.io/badge/platform-Linux%20%7C%20WSL-lightgrey.svg)](docs/REFERENCE.md#4-required-environment)
[![Typed: py.typed](https://img.shields.io/badge/typed-py.typed-informational.svg)](src/orca_auto/py.typed)

**English** | [한국어](README.ko.md)

orca_auto is a queue-first interface for standalone ORCA, standalone xTB molecular dynamics (xTB-MD), and workflow orchestration on Linux and WSL. It submits work durably, runs it under supervised workers, and records per-job state and reports.

## Statement of need

Computational chemistry projects often outgrow one-shot engine commands and ad hoc shell loops. Users need durable submission, supervised execution, explicit recovery semantics, consistent job reports, and an explicit record of which calculation failed and what next action is safe.

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
- xTB installed at an absolute Linux path if you use standalone xTB-MD or workflow stages that depend on it
- CREST installed at an absolute Linux path if you use workflow stages that depend on it

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
  max_active_xtb_md: 1

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
- Configured ORCA/xTB/CREST executable paths must point to existing executable Linux binaries. Leave `workflow.paths.xtb_executable` or `workflow.paths.crest_executable` blank only when you intentionally want PATH lookup at submission; the resolved executable identity is then bound to the queued generation. The same canonical `workflow.paths.xtb_executable` is used by standalone xTB-MD and workflow xTB stages.
- `default_max_retries: 0` disables ORCA retries; any positive value enables the
  calculation-type retry policy, capped by ORCA route type.
- `scheduler.max_active_simulations` is the shared cap across ORCA, standalone xTB-MD, internal xTB workflow stages, and internal CREST workflow stages. `scheduler.max_active_xtb_md` is a positive standalone xTB-MD subcap and defaults to `1`.
- Everything lives under the single runs root (`runs_root`): standalone ORCA/xTB-MD jobs and workflow workspaces sit side by side in it, and the shared admission directory defaults to `<runs_root>/.admission`.
- Workflow-managed xTB/CREST job dirs, per-workflow queues/indexes, and outputs live only under `<runs_root>/<workflow_id>/<NN_engine>` (`01_crest`, `02_xtb`, `03_orca`).
- Discord interactivity requires a separate orca_auto Discord application/bot with
  Message Content Intent enabled. Do not share the `ollama_bot` token between two
  gateway processes.
- Follow [docs/DISCORD_SETUP.md](docs/DISCORD_SETUP.md) for the bot invite, channel IDs,
  permissions, service startup, and command verification.
- The full template lives at [config/orca_auto.yaml.example](config/orca_auto.yaml.example).

## Standalone xTB-MD

Put one optimized starting geometry (strongly recommended) and exactly one
`xtb_md_job.yaml` in a job directory under `runs_root`. For example:

```yaml
schema_version: 1
input_xyz: start.xyz
gfn: 2
charge: 0
uhf: 0
ensemble: nvt       # nvt | nve
temperature_k: 298.15
time_ps: 1.0
walltime_seconds: 3600
step_fs: 2.0
dump_fs: 50.0
hydrogen_mass_amu: 4
shake: 2
scc_accuracy: 2.0
# solvent_model: alpb  # optional; gbsa | alpb, paired with solvent
# solvent: water
resources:
  max_cores: 4
  max_memory_gb: 8
```

Submit and inspect it through the same queue-first surface:

```bash
orca_auto run-dir '/home/user/runs/water_md'
orca_auto queue list --engine xtb_md
orca_auto queue cancel q_20260713_160000_ab12cd
orca_auto queue list clear
```

`queue list --engine xtb_md` filters the unified activity view. `queue cancel`
accepts the displayed activity/queue id and known path aliases. `queue list
clear` is intentionally unfiltered and prunes terminal entries across all
activity sources, not only xTB-MD.

The required manifest fields are `schema_version`, `input_xyz`, `gfn`,
`ensemble`, `temperature_k`, `time_ps`, `walltime_seconds`, `step_fs`, and
`dump_fs`. Unknown fields fail closed. `charge` and `uhf` default to `0`;
`hydrogen_mass_amu`, `shake`, and `scc_accuracy` default to `4`, `2`, and `2.0`.
The optional `resources` mapping can request values only at or below the
configured per-task ceilings. `time_ps` and `dump_fs` must each be an exact positive multiple of
`step_fs` after converting picoseconds to femtoseconds.

The standalone adapter supports NVT and NVE only. It does not use a workflow,
retry or resume a generation, or expose an arbitrary random seed, `--omd`, raw
xcontrol, constraints, or metadynamics. It generates one canonical fresh-run
`$md` input with the fixed `$samerand` sequence. A cancellation terminates the
active process group and reaches a terminal state; service interruption or an
orphaned generation fails terminally instead of being requeued.

Server-owned ceilings are 10,000 atoms, 999,999 MD steps, 100,000,000
atom-steps, 100,000 trajectory frames, 86,400 seconds wall time, 1 GiB retained
output, and 10,000 output files. A successful job writes `job_state.json`,
`job_report.json`, and `job_report.md` at the job root. Its immutable execution
tree and validated `xtb.trj`, `mdrestart`, `xtbmdok`, and logs are retained under
`.orca_auto_xtb_md_executions/<job_id>/`.

Standalone xTB-MD currently accepts exactly xTB 6.7.1, which was the latest
stable release when this contract was added. This is not a claim that the
upstream release is issue-free: exit code 0 and `xtbmdok` are insufficient on
their own, and the adapter fails closed on known false-success markers such as
`MD is unstable, emergency exit` and `but still taking it as converged!`, as
well as incomplete or invalid trajectory/checkpoint evidence.

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
orca_auto run-dir '/home/user/runs/water_md'

# inspect and maintain
orca_auto queue list --engine orca
orca_auto queue list --engine xtb_md
orca_auto queue list clear      # prune completed/failed/cancelled
orca_auto queue cancel <target>
orca_auto service status
orca_auto service restart
orca_auto scan-notify
orca_auto bot run              # foreground Telegram/Discord gateway
```

`queue list` prints a compact, terminal-width-aware table; workflow children are grouped
under their parent. On an interactive terminal it adds a styled summary band (per-status
counts), box-drawing tree connectors for workflow children, and a status-colored left
rail; the `--watch` view adds a spinner, a clock, a live system CPU/RAM/load gauge, and
per-running-job CPU/RAM for every engine (sampled from `/proc`, no new dependency). Piped,
`--json`, and `--no-color` output stays plain and byte-stable for scripts. The selected bot mirrors the same application surface
(Telegram `/list`; Discord `!list`, with matching cancel/help commands) and uses
provider-native buttons. Discord can optionally accept a compressed run-dir
(`.zip`/`.tar.gz`) attached to `!run`: the archive is safe-extracted under `runs_root`
and queued after an explicit Confirm button. Upload reservations, confirmations,
and commit receipts are durable and idempotent; an uncertain queue result is preserved
for reconciliation rather than retried or deleted. This is disabled by default and
gated to allowlisted operators — see the `messenger.discord.uploads` block in
[config/orca_auto.yaml.example](config/orca_auto.yaml.example). For the full command
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
- ORCA selects the most recently modified `.inp` at submission and binds that
  input plus supported file dependencies into a visible
  `<job_dir>/generation-YYYYMMDD-HHMMSS-<8-hex>/` directory. The bound input and
  each dependency keep their original basenames, and raw ORCA files appear at
  that same level; new ORCA submissions do not add hidden execution or nested
  input directories. Editing the source afterward does not change the queued
  generation. References to different source paths that share a basename are
  rejected even when their bytes are identical. A main same-stem `* xyzfile`
  geometry is inlined into the bound input so Opt-like
  routes can keep and later update that exact XYZ filename without a hash or
  rename; same-stem auxiliary NEB Product/TS files remain ambiguous and are
  rejected.
- A fully closed ORCA job directory can be submitted again and receives a new
  sibling generation. Active work and incomplete terminal publication still
  block another submission for the same directory.
- `flow.yaml`, `xtb_md_job.yaml`, and internal engine job manifests are limited to 1 MiB, 32 YAML
  aliases, 10,000 parsed/expanded nodes, and 64 nesting levels; cyclic/recursive
  YAML graphs fail closed. Local geometries are limited to 10,000 atoms, reduced
  to 1,000 for xTB/ORCA Hessian-producing jobs and 200 for Discord-uploaded work.
- When retrying or resuming an interrupted ORCA run, orca_auto uses a matching
  non-empty `.gbw` file by generating a restart input with `MORead` and `%moinp`.
- The ORCA job root keeps `run.lock` and the latest public state/report files.
  `job_state.json` and `job_report.json` are also mirrored into the visible
  generation they describe. Standalone xTB-MD keeps its existing artifact
  layout.
- Use the `systemd` assets in [systemd/README.md](systemd/README.md) for unattended WSL or Linux execution.

## Testing

```bash
make test
```

`make test` runs `scripts/check.sh`, which creates or repairs `.venv`,
installs `.[dev]`, then runs `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, and
the coverage-gated pytest suite. Pass pytest selectors directly to the script when you want a narrower
loop, for example `bash scripts/check.sh tests/flow -q`.

Run the retained fake-engine smoke suite after each behavioral patch. From the
source checkout backing the installed command, the default fake profile discovers
the shared config and uses its `runs_root`:

```bash
orca_auto smoke
```

Use `--runs-root /absolute/path/to/runs` for an isolated fake batch or
`--config /absolute/path/to/orca_auto.yaml` for a non-default config. The retained
`scripts/smoke.sh` wrapper accepts the same options and pins the current worktree,
which is useful in CI and parallel checkouts.

Each batch is kept under `<runs_root>/.orca_auto_smoke/`, including actual
fake-engine outputs, `batch.json`/`case.json`, a Markdown summary, and the
offline `review/index.html` artifact index. Its Open buttons use bounded,
Windows-friendly byte copies under a short `review/g-*/open/` path; the original
runtime tree remains authoritative, and `artifacts.json` maps every copy back to
its full source path and matching SHA-256. Workflow-report bundles retain their
confined relative links to child job reports. A deliberately failing simulation
case passes only when the observed terminal failure matches its declared
expectation; harness failures, skips, terminal mismatches, missing required
artifacts, source drift, or an incomplete source identity still fail the batch.
See [docs/VALIDATION.md](docs/VALIDATION.md) for the review policy, bounded-copy
limits, and separate opt-in real-ORCA and real-xTB boundaries.

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
