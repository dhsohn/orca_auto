# Validation and testing

ORCA_auto validation is split into two honest layers:

1. CI and fake-engine checks that can run publicly without licensed chemistry
   binaries.
2. Opt-in manual acceptance checks that use a real
   ORCA/xTB/CREST deployment when a change depends on engine runtime semantics.

This split is intentional. The public test suite should prove the queue,
configuration, parser, execution-policy, reporting, packaging, and fake-engine
integration contracts without requiring private credentials or licensed binaries.
Real-engine checks should be recorded explicitly when they are needed.

## What CI proves

The GitHub Actions workflow runs multiple independent checks:

- Gitleaks secret scanning.
- ShellCheck for repository shell scripts.
- Python 3.11, 3.12, and 3.13 checks through `scripts/check.sh`.
- Ruff, Ruff format check, mypy, and coverage-gated pytest.
- Distribution checks for separate core and workflows builds, their exact source
  inventories, and isolated core-only/workflow-enabled installation profiles.

The pytest suite exercises ORCA logic with unit tests,
sanitized fixtures, and fake-engine integration paths. These checks cover durable queue behavior,
state/report writing, parser behavior, execution policy, notification formatting,
workflow handoff contracts, and CLI surfaces.

## What CI does not prove

CI does not prove:

- that a local ORCA, xTB, CREST, OpenMPI, or site scheduler installation is valid;
- that requested memory/core settings are accepted by a particular cluster or
  workstation policy;
- that messenger credentials are configured or network delivery succeeds;
- that a chemically meaningful calculation converges;
- that private research structures or proprietary outputs are safe to publish.

Do not describe a PR as real-ORCA validated unless a real ORCA binary was used
and the command/result is recorded in the PR.

## Standard local checks

For normal code changes:

```bash
bash scripts/check.sh
make check-packages
```

For focused changes, pass pytest selectors through the shared script:

```bash
bash scripts/check.sh tests/test_single_attempt_contract.py -q
bash scripts/check.sh tests/flow -q
```

For docs/template-only changes, a targeted check is acceptable if the PR states
that no runtime behavior changed. For example:

```bash
python - <<'PY'
from pathlib import Path
import yaml
for path in Path('.github/ISSUE_TEMPLATE').glob('*.yml'):
    yaml.safe_load(path.read_text(encoding='utf-8'))
print('issue templates parse')
PY
```

## Executable fake ORCA smoke

The fake ORCA smoke exercises the public CLI submission path and a queue worker
lifecycle without requiring ORCA:

```bash
bash examples/fake_orca_smoke/run.sh
```

The script creates a temporary runtime root, writes a fake ORCA executable,
submits a minimal input with `orca_auto run-dir`, runs one worker poll, and
asserts that queue status, internal `job_state.json`, and public `machine.json` reach a
completed state.

This check is appropriate for docs/release-hygiene PRs and for queue/reporting
changes that do not require true ORCA numerical behavior.

## Additional manual real-ORCA acceptance

Real ORCA/OpenMPI compatibility and calculation-specific output interpretation
remain explicit maintenance acceptance steps that only a real ORCA binary can
satisfy. A passing fake-engine check (the executable fake ORCA smoke above)
exercises the queue and reporting plumbing only; it is not real-ORCA validation,
and one tiny single point is not evidence for calculation types it did not run.

Use a real ORCA acceptance check when a PR changes one of these areas:

- ORCA process invocation;
- input selection or resource directive rewriting;
- execution/recovery policy;
- output analyzer classification;
- report fields derived from real ORCA output;
- systemd/runtime behavior that cannot be represented by fake-engine tests.

A minimal acceptance record should include:

```text
## Verification

- Real ORCA acceptance: passed
  - ORCA version:
  - OS/runtime:
  - command:
  - calculation type:
  - generated state/report files:
  - observed terminal marker:
```

Use small, non-confidential systems. Prefer sanitized or public fixtures, and do
not commit proprietary structures or large raw outputs unless a separate issue
justifies them.

### Optimization verdict and evidence-extraction acceptance (2026-09-05)

ORCA 6.1.1 on Linux/WSL2, one core and 1 GB per job, isolated temporary
queues/admission; no production worker restart:

```bash
ORCA_REAL_EXECUTABLE=/path/to/orca timeout --signal=TERM --kill-after=10s 180s \
  .venv/bin/python -m pytest -q --no-cov \
  tests/integration/test_orca_worker_smoke.py -k water_optimization_acceptance
```

Both opt-in cases passed:

- Public H2O, `HF STO-3G Opt Freq TightSCF`, `MaxIter 30`: completed with a
  finite electronic/Gibbs energy, nine vibrational frequencies and zero
  imaginary modes. Parser, progress, HTML and structure evidence agree.
- The same geometry, `HF STO-3G Opt TightSCF`, `MaxIter 1`: normally terminated
  but `geometry_not_converged`, failed queue/state and no SI block. Parser and
  progress/report cards do not claim convergence.
- Both released admission slots. Their generation `machine.json` files passed
  the CI-pinned common validator with artifact-byte verification and producer
  `orca_auto` / payload `chemistry/results-bundle` v1 pins. Success was
  `succeeded/complete/ready`; non-convergence was `failed/complete/blocked`.

Separately, the structure/frequency extraction retained 15 moved definitions'
ASTs apart from declared symbol/docstring/lint ownership changes. Two baseline
generations and the post-move generation of eight synthetic report cases
produced identical HTML/SI bytes (15 files; plain scan has no SI block).
Independent review also compared 144 SI lint/provenance combinations byte-for-byte.
These extraction comparisons do not claim real-engine acceptance for TS, NEB,
IRC, or scan calculations.

### Combined TS/frequency/IRC evidence check (2026-09-12)

ORCA 6.1.1 on Linux/WSL2, public planar NH3, `HF STO-3G TightSCF
OptTS Freq IRC`, initial `Calc_Hess true`, default optimization coordinates,
one core and a 1 GB job budget, isolated temporary queues/admission:

```bash
ORCA_REAL_EXECUTABLE=/path/to/orca timeout --signal=TERM --kill-after=10s 180s \
  .venv/bin/python -m pytest -q --no-cov \
  tests/integration/test_orca_worker_smoke.py -k ammonia_ts_irc
```

The observed runs at IRC `PrintLevel 1` and `PrintLevel 2` both completed:
the final TS frequency analysis had one imaginary mode, both IRC directions
reported convergence, and the analyzer and IRC report retained Nimag = 1.
The frequency/mode coordinates remained those of the planar TS. The generated
`machine.json` packages passed the CI-pinned common validator, including artifact
receipts and producer/payload pins, with `succeeded/complete/ready` decisions.
Admission slots were released; no production worker was restarted.

These runs did **not** reproduce the proposed loss of TS frequency evidence due
to a later IRC `FINAL SINGLE POINT ENERGY` line: neither output contained such
a line after the final frequency section. The verbose case also exercised the
analyzer's full-file TS scan. No analyzer rule was changed. The existing guard
against accepting an initial/recalculated optimization Hessian as final TS
verification remains in place.

This is evidence for the two tested ORCA 6.1.1 output variants, not every
method, version, or combined route. A synthetic post-frequency energy line can
invalidate the selected section; an actual affected combined output is needed
before changing that boundary. An initial Cartesian-optimization probe was
rejected by ORCA before calculation; the accepted inputs use the default
optimization coordinates. IRC convergence alone does not prove that its
endpoints are verified minima.

## Idle-only service restart acceptance (2026-09-12)

The focused, engine-free regression suite is:

```bash
bash scripts/check.sh tests/test_cli_systemd_restart.py \
  tests/test_cli_systemd_restart_guard.py --no-cov
```

It covers installed versus caller configuration, active/reserved and unresolved
slots, invalid evidence, explicit force bypass, sudo authentication ordering,
restart failures, and a separate process attempting admission while the entire
restart sequence holds the shared lock.

A disposable Linux/WSL2 user-manager service additionally exercised the actual
systemd 249 restart boundary. It ran this checkout's real queue worker with an
isolated configuration/admission directory and a deliberately waiting **fake**
ORCA executable on a public H2 input. The test routed service-unit selection and
systemctl calls to that single temporary user unit; it did not exercise a
production system target, sudo authentication, or a real ORCA calculation.

- While the fake engine held an admission slot, default restart returned
  non-zero without `reset-failed` or `restart` calls. Both worker and engine PIDs
  remained alive and unchanged.
- After releasing the fake engine and confirming completed queue state and an
  empty admission pool, default restart succeeded and changed the worker PID.
- The resulting exact `machine.json` and artifact receipts passed the
  CI-pinned common validator, with producer `orca_auto` and payload
  `chemistry/results-bundle` v1 checked separately. Its three decisions were
  `succeeded`, `complete`, and `ready`; these are fake-engine pipeline evidence,
  not scientific acceptance.
- The disposable unit was stopped afterward. The production system worker and
  its running calculation were not restarted, reconfigured, or source-synced.

## First-stage runtime boundary acceptance (2026-09-12, before the distribution split)

The focused development-profile checks are:

```bash
.venv/bin/python -m pytest -q --no-cov tests/test_core_without_workflows.py \
  tests/test_activity_extension_boundary.py tests/test_workflow_worker_boundary.py
```

The subprocess acceptance copies the package without `flow/` and provides only
its declared YAML dependency and package metadata. Fresh isolated interpreters
exercise help/version, ORCA submission/list/cancel/clear, the default worker
plan, and the real queue worker with a deliberately fake ORCA executable.
The engine descendant itself asserts that its imported package is the staged
copy and that no workflow package can be found. This guards against an editable
installation silently supplying the excluded source.

Explicit workflow commands/filters and retained workflow state are refused
without mutations. Additional regressions cover malformed/dangling registry
evidence, scaffold/workspace/stage evidence, unreadable scans, separate engine
config roots, and retained xTB/CREST admission identities. Completed terminal
rows can be cleared; cancelled rows awaiting terminal replay remain protected.
An installed extension with an import failure is not classified as absent.

The actual fake-child output passed the common validator at the CI pin
`bc252035d01edddf1314e6641689c6d5cb88af92`, with `--machine` artifact byte/hash
verification, producer `orca_auto`, and payload `chemistry/results-bundle` v1.
Its independent lifecycle/delivery/handoff decisions were
`succeeded` / `complete` / `ready`. This proves a core-only pipeline, not a
scientific ORCA result, a separately built core wheel, or deployment acceptance.

Independent AST comparison preserved the moved ORCA cancellation, result
envelope, activity-record and engine-path functions. The preceding termination
and safe-restart fixes were carried forward unchanged in their runtime owners.
No scientific execution/interpretation policy changed in this boundary
extraction; the earlier real-engine acceptance remains separately recorded.

## Recorded real-engine runs

These are real-engine runs performed on the maintainer workstation (Linux/WSL2).
They are a maintenance record for the runtime and recovery contracts, not a
benchmark claim or evidence of chemical validity, and they use small
non-confidential systems.

ORCA 6.1.1:

- H2 single point followed by a cooldown Freq/CP-SCF pass, 1 core: both stages
  passed with clean terminal states and no leftover queue, process, or admission
  rows afterward.
- A small reaction-intermediate single point:
  - direct (worker-off) 1 core `26m46s`, 2 core `14m34s`;
  - the same input under the supervised worker at 2 cores completed in `14m59s`
    (`+2.8%` over the direct run) with energy and SCF values identical to the
    direct run and zero restart, retry, or duplicate events;
  - the same input with RAM scratch at 4 cores completed in `9m19s` (`1.675x`
    over the 2-core run), with the total energy within `8.1e-9` Eh.
- Idle five-worker supervisor with no calculation queued: steady CPU near `1.55%`
  of one core and about `160-165 MB` resident, with no fan spin-up.

Real-engine re-validation is an opt-in maintenance check, separate from the
public CI and fake-engine suites. Record the exact runtime, calculation type,
and observed result for each acceptance run; historical workstation incidents
are not evidence of a current execution restriction.

### 4.0.0 removal acceptance (2026-09-05)

ORCA 6.1.1 on Linux/WSL2, one core per job, isolated temporary queues:

- H2 `HF STO-3G SP TightSCF`: the opt-in
  `test_real_orca_h2_single_point_acceptance_when_configured` test passed,
  including normal termination, state, report, SI output and released admission.
- H2O `HF STO-3G Opt TightSCF`, `%geom Scan` bond 0–1 from 0.8 to 1.1 Å in
  four points: completed normally with four finite surface energies and a
  relaxed-scan HTML report.
- H2O `HF STO-3G SP TightSCF` with `%scf MaxIter 1`: intentionally failed
  SCF convergence and ended after one attempt with `scf_not_converged`.
  No retry input was generated; the submitted input remained unchanged.

The CI-pinned common validator accepted all three generated `machine.json`
files and their artifact receipts. These checks cover the stated runtime paths,
not untested calculation types or a scientific TS-search acceptance.

## Fixture and artifact policy

- Keep fixtures minimal, sanitized, and deterministic.
- Prefer output snippets that exercise a parser or classifier over full raw
  output files.
- Do not commit credentials, private paths, messenger bot tokens, channel IDs, or private
  research data.
- When a fixture represents a failure mode, document the expected classifier,
  failure decision, and safe next action.

## PR validation reporting

Every PR should report verification in the same Motivation -> Changes ->
Verification style used by the pull request template. If a check is intentionally
not run, say why.

Examples:

```text
- `bash scripts/check.sh` — passed
- `bash examples/fake_orca_smoke/run.sh` — passed
- Manual ORCA acceptance — not run; docs-only change
```
