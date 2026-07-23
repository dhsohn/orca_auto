# Validation and testing

orca_auto validation is split into two honest layers:

1. CI and fake-engine checks that can run publicly without licensed chemistry
   binaries.
2. Opt-in manual acceptance checks that use a real
   ORCA/xTB/CREST deployment when a change depends on engine runtime semantics.

This split is intentional. The public test suite should prove the queue,
configuration, parser, retry-policy, reporting, packaging, and fake-engine
integration contracts without requiring private credentials or licensed binaries.
Real-engine checks should be recorded explicitly when they are needed.

## What CI proves

The GitHub Actions workflow runs multiple independent checks:

- Gitleaks secret scanning.
- ShellCheck for repository shell scripts.
- Rendered systemd unit verification.
- Python 3.11, 3.12, and 3.13 checks through `scripts/check.sh`.
- Ruff, Ruff format check, mypy, and coverage-gated pytest.
- Wheel smoke check that requires the packaged Python-module inventory to
  exactly match `src/orca_auto` and verifies the single root typing marker.

The pytest suite exercises ORCA logic with unit tests,
sanitized fixtures, and fake-engine integration paths. These checks cover durable queue behavior,
state/report writing, parser behavior, retry policy, notification formatting,
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
```

For focused changes, pass pytest selectors through the shared script:

```bash
bash scripts/check.sh tests/test_scants_support.py -q
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
asserts that queue status, `job_state.json`, and `job_report.json` reach a
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
- retry/resume policy;
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

At the time of writing, real-engine re-validation is paused: the workstation is
held for a hardware power/thermal issue that is under separate investigation. The
public CI and fake-engine suites do not depend on that hardware and continue to
run.

## Fixture and artifact policy

- Keep fixtures minimal, sanitized, and deterministic.
- Prefer output snippets that exercise a parser or classifier over full raw
  output files.
- Do not commit credentials, private paths, messenger bot tokens, channel IDs, or private
  research data.
- When a fixture represents a failure mode, document the expected classifier,
  retry decision, and safe next action.

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
