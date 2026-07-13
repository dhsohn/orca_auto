# Validation and testing

orca_auto validation is split into two honest layers:

1. CI and fake-engine checks that can run publicly without licensed chemistry
   binaries.
2. Manual acceptance checks that use a real ORCA/xTB/CREST deployment when a
   change depends on engine runtime semantics.

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
- Wheel smoke check that verifies typed-package metadata.

The pytest suite exercises ORCA and standalone xTB-MD logic with unit tests,
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
bash scripts/check.sh tests/xtb_md -q
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

## Manual real-ORCA acceptance

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

## Standalone xTB-MD acceptance

Fake-engine checks must cover strict manifest admission, immutable snapshots,
NVT/NVE command generation, cancellation/process-group termination, no
retry/resume, resource/output/time ceilings, and rejection of return-code-zero
false success, stale, truncated, wrong-atom, or non-finite artifacts.

Changes to the standalone MD invocation or terminal validator also require a
small sanitized real-xTB NVT and NVE acceptance. Record the exact xTB version
and executable identity, manifest, generated `$md` input, queue terminal state,
`xtbmdok`, trajectory frame/atom counts, `mdrestart` validation, and output
identities. The supported adapter version is currently xTB 6.7.1; describe it as
the latest stable version selected for this adapter, not as issue-free. Confirm
that a fixture containing a known false-success marker fails closed.

## Fixture and artifact policy

- Keep fixtures minimal, sanitized, and deterministic.
- Prefer output snippets that exercise a parser or classifier over full raw
  output files.
- Do not commit credentials, private paths, messenger bot tokens, chat/channel IDs, or private
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
