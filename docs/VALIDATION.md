# Validation and testing

orca_auto validation is split into two honest layers:

1. CI and fake-engine checks that can run publicly without licensed chemistry
   binaries.
2. Opt-in retained smoke and manual acceptance checks that use a real
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

## Retained smoke batches and review packets

Use the developer smoke runner after each behavioral patch that can affect
submission, workers, terminal classification, or generated reports. The fake
profile covers successful and fail-closed cases across standalone ORCA,
standalone xTB-MD NVT/NVE, and the supported workflows:

```bash
orca_auto smoke
```

The command runs the default fake profile from the source checkout backing the
installed `orca_auto` command. It discovers the shared config and uses its
`runs_root`. The resolved runs root must be outside the repository worktree;
this keeps the retained batch itself from changing the source identity recorded
for the run.

Use an explicit isolated root or non-default shared config only when needed:

```bash
orca_auto smoke --runs-root /absolute/path/to/runs
orca_auto smoke --config /absolute/path/to/orca_auto.yaml
```

The retained `scripts/smoke.sh` wrapper accepts the same options but pins the
checkout containing the script. Use it for CI or when several editable
worktrees exist. A wheel-only installation without repository tests fails
closed instead of pretending to run this source smoke suite.

The runner creates one owned reserved tree and never mixes its retained jobs
with production activity:

```text
<runs_root>/.orca_auto_smoke/
├── index.json
└── batches/<batch_id>/
    ├── batch.json
    ├── summary.md
    ├── review/
    │   ├── index.html
    │   └── g-<generation>/
    │       ├── artifacts.json
    │       └── open/
    │           └── cNN-<case>/
    │               └── aNNNN/
    │                   ├── <short-artifact-name>
    │                   └── ... confined HTML-linked child reports ...
    └── cases/<scenario_id>/
        ├── case.json
        └── runtime/
            ├── _smoke_harness/
            │   ├── harness.stdout.log
            │   ├── harness.stderr.log
            │   └── pytest.xml
            └── pytest/... retained job/workflow trees and raw engine output ...
```

`batch.json` records complete source identities at batch start and finish plus
the aggregate result. The identity covers the git head, the tracked working
tree diff digest, and the status digest (untracked files reach it through
their names; their content is deliberately outside the provenance scope). A
source change or an unavailable identity fails the batch rather than attaching
a false provenance claim. Each `case.json` keeps the expected terminal state,
observed terminal state, and harness verdict as separate fields. Consequently,
a negative simulation that is expected to
end in `failed` can correctly produce a passing smoke verdict. A skipped or
failed pytest scenario, an unexpected terminal state, or a missing required
artifact still fails the case and the batch.

The runtime tree remains the artifact source of truth. `summary.md` provides a
compact manifest, while the offline `review/index.html` gives primary Open links
to bounded, Windows-friendly byte copies under one generation-specific short-path
generation. These copies are separate regular files, not hardlinks: creating or
opening them does not change the runtime inode or link count. The generation's
`artifacts.json` records each full runtime source path, short open path, size,
source SHA-256, copy SHA-256, issue, and HTML dependency mapping. The generator
revalidates every copied source and destination before publishing `index.html`
as the packet commit marker.

Pytest also creates a top-level `pytest/*current` symlink that merely points to
its latest numbered temporary directory. The runner removes those transient
convenience aliases after each scenario, so the retained runtime keeps only
the numbered directories with the real artifacts, listed normally subject to
the same bounded traversal rules. Any symlink that does survive in a runtime —
including aliases from batches produced before the removal — stays visible as
an ordinary blocked entry and is never followed.

The short projection preserves the confined local `href`/`src` closure needed
by generated reports. For example, a `workflow_report.html` copy carries its
relative `03_orca/01_ts_guess/<generation>/job_report.html` target in the same
artifact bundle, so the child report still opens offline. External URLs are left as
external references. Absolute, escaping, malformed, symlinked, hard-linked, or
otherwise unsafe local targets block that report copy rather than falling back
to the original long path. Per-file, aggregate-byte, entry, digest, or path
limits likewise leave a visible `not opened`/review issue. All originals remain
in the runtime tree for provenance inspection.

Short review paths are ASCII and capped relative to the batch so ordinary WSL
UNC paths stay practical with the documented `runs_root`; an arbitrarily long
custom root cannot be made Windows-safe by the packet alone. Do not edit review
copies: inspect them, then fix the producer and rerun the smoke batch. Editing a
copy would invalidate its recorded digest. Bounded, escaped previews are for
orientation only and do not certify scientific meaning.

Use this human-review rule:

- When a smoke scenario or output contract is introduced, open at least one
  example of every distinct artifact type it generates, including reports such
  as `job_report.html`, `si_block.md`, and `workflow_report.html`.
- On later patches, inspect every failed or unexpected case and every artifact
  type changed by the patch. Repeated child artifacts with the same role and
  matching available content digest can be sampled; all originals remain
  available when a deeper comparison is needed.

The fake scenarios generate their case-local configs with empty messenger
credentials. The shared config is used to resolve roots and is not copied into
the review packet. Preview redaction is only defense in depth: use sanitized
inputs, never put credentials or private research data in smoke artifacts, and
inspect raw files before sharing a batch.

The top-level `.orca_auto_smoke` name is reserved relative to the configured
production `runs_root`. Production `run-dir` submission rejects targets in that
tree, and production discovery, reindexing, and terminal-state cleanup prune
it. Each scenario uses a nested case-local runs root, so its own queue and
artifact discovery still exercise the normal product path.

Retention is intentional: production queue cleanup does not delete these
batches. Monitor disk use and archive or remove an entire reviewed batch only
under the owned `.orca_auto_smoke/batches/` tree; never delete selected child
files and leave a partially trusted packet behind.

## Opt-in real-xTB smoke

Changes to xTB-MD invocation or validation should also run the opt-in real-xTB
profile with the supported xTB 6.7.1 executable:

```bash
XTB_MD_REAL_EXECUTABLE=/absolute/path/to/xtb \
  orca_auto smoke --profile real-xtb \
  --config /absolute/path/to/orca_auto.yaml
```

The real-xTB profile requires scheduler settings from the shared config and holds
a production admission lease while the real-engine scenario runs. Unavailable
capacity, an unset/non-executable `XTB_MD_REAL_EXECUTABLE`, or a skipped test is
not reported as success. The retained NVT/NVE outputs must still be reviewed;
passing the adapter contract is not a claim of chemically meaningful dynamics.
When the shared config enables `orca.runtime.scratch_root`, the harness discards
any inherited scratch override and passes only that validated root/minimum to
the case-local xTB-MD config. The acceptance then requires a private tmpfs CWD,
committed `scratch_provenance`, the canonical durable allowlist, and cleanup.

## Opt-in real-ORCA smoke

Changes to ORCA process invocation, output parsing, or generated reports should
also run the opt-in real-ORCA profile with an explicitly selected executable:

```bash
ORCA_REAL_EXECUTABLE=/absolute/path/to/orca \
  orca_auto smoke --profile real-orca \
  --config /absolute/path/to/orca_auto.yaml
```

This profile submits a sanitized H2 HF/STO-3G single point through public
`run-dir`, executes one supervised ORCA worker lifecycle, and retains the raw
`h2.out`, terminal state, job reports, and SI block in its review packet. The
shared config must supply the matching production `runs_root` and scheduler
admission settings; the runner holds an `orca_auto_orca` production admission
lease until the supervised scenario process tree exits. Unavailable capacity,
an unset/non-executable `ORCA_REAL_EXECUTABLE`, or a skipped test is not success.

Passing this lane proves only that the selected binary can execute this small
serial input and that orca_auto can classify and render its observed output. It
does not validate multicore OpenMPI behavior, optimization/frequency semantics,
site scheduler integration, or chemical suitability for a research system.

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

The retained real-ORCA profile is deliberately one tiny serial single point.
Real ORCA/OpenMPI compatibility and calculation-specific output interpretation
remain explicit maintenance acceptance steps; a passing fake batch is not
real-ORCA validation, and a passing H2 batch is not evidence for calculation
types it did not execute.

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
When RAM scratch is enabled, they must also prove that the actual process CWD
and geometry/control arguments are inside the private scratch workspace while
the reported command and execution identity stay durable; only the two logs,
trajectory, checkpoint, and success marker may be published. Test committed
cleanup after success, false-success, and shutdown, plus fail-closed retention
after durable-input mutation or publication failure.

Changes to the standalone MD invocation or terminal validator also require a
small sanitized real-xTB NVT and NVE acceptance. Record the exact xTB version
and executable identity, manifest, generated `$md` input, queue terminal state,
`xtbmdok`, trajectory frame/atom counts, `mdrestart` validation, and output
identities. The supported adapter version is currently xTB 6.7.1; describe it as
the latest stable version selected for this adapter, not as issue-free. Confirm
that a fixture containing a known false-success marker fails closed. With RAM
scratch configured, also record the live process CWD, committed
`scratch_provenance`, durable allowlist, workspace cleanup, and absence of an
automatic SSD fallback.

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
