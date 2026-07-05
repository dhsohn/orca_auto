# Contributing to orca_auto

Thank you for helping improve orca_auto. This project is not being prepared as a
JOSS submission, but it intentionally borrows JOSS-style open-source operating
practices: clear motivation, reviewable changes, objective verification, and a
public development record.

## Project scope

orca_auto is a queue-first runtime and workflow layer for ORCA-centered
computational chemistry work on Linux and WSL. It should make calculations more
observable and recoverable without becoming a general workflow engine, an ORCA
replacement, or a collection of one-off site scripts.

Good contributions usually improve one of these surfaces:

- durable queue submission, cancellation, and worker behavior;
- ORCA state/report/provenance files;
- fail-closed retry and resume policy;
- workflow handoff contracts for internal xTB/CREST stages;
- documentation, examples, validation, and release hygiene.

## Development workflow

Use the same small-record workflow for normal changes:

1. Open or identify a GitHub issue that states the problem.
2. Create a focused branch or worktree from `origin/main`.
3. Keep the change narrow enough to review.
4. Open a pull request before merge.
5. Let CI and any relevant local/manual checks finish before merging.

Suggested branch prefixes:

- `fix/...` for behavior fixes;
- `docs/...` for documentation and project-hygiene changes;
- `test/...` for regression coverage;
- `ci/...` for CI-only changes;
- `refactor/...` for behavior-preserving internal cleanup.

## Pull request narrative

Every PR should use this structure. Keep it factual and evidence-backed.

### Motivation

Explain the user/research-workflow problem, failure mode, or maintenance gap.
For ORCA runtime changes, name the calculation class or queue state involved.

### Changes

List the concrete code, docs, test, or configuration changes. Call out public
surface changes such as CLI flags, config keys, report fields, retry behavior,
or output layout changes.

### Verification

List commands actually run and the result. Do not claim a full ORCA acceptance
unless a real ORCA binary was used. If a change only affects docs/templates, a
targeted Markdown/YAML/template check is acceptable; say that no runtime behavior
changed.

A good verification section looks like:

```text
## Verification

- `bash scripts/check.sh tests/test_scants_support.py -q` — passed
- `bash examples/fake_orca_smoke/run.sh` — passed; fake ORCA queue lifecycle completed
- Manual ORCA acceptance: not run; docs-only change
```

## Local setup

From the repository root:

```bash
bash scripts/bootstrap_wsl.sh
source .venv/bin/activate
make test
```

`make test` runs `scripts/check.sh`, which creates or repairs `.venv`, installs
`.[dev]`, then runs Ruff, Ruff format check, mypy, and the coverage-gated pytest
suite.

For a narrower loop:

```bash
bash scripts/check.sh tests/test_scants_support.py -q
bash scripts/check.sh tests/flow -q
```

The fake ORCA example smoke is intentionally runnable without a licensed ORCA
binary:

```bash
bash examples/fake_orca_smoke/run.sh
```

## Git hooks (optional)

Versioned hooks live in `.githooks/`. Enable them once per clone:

```bash
git config core.hooksPath .githooks
```

- `pre-commit` runs the fast half of `scripts/check.sh` (Ruff lint + format,
  mypy, import-linter) and auto-formats fully-staged Python files, so a missing
  `ruff format` cannot reach CI.
- `pre-push` runs the full `scripts/check.sh` (adds the coverage-gated pytest
  suite) against the existing `.venv`.

Both need `.venv` (run `scripts/check.sh` once first) and can be bypassed with
`git commit --no-verify` / `git push --no-verify`.

## ORCA, xTB, CREST, and path policy

- Use absolute Linux paths for configured executables and runtime roots.
- Do not add support for Windows drive paths, `/mnt/<drive>/...` executable
  paths, relative executable paths, or `.exe` binaries.
- CI is allowed to use fake engines and sanitized fixtures. It must not require
  a licensed ORCA installation.
- Real ORCA acceptance checks belong in PR verification when the change depends
  on ORCA runtime semantics.

## Retry and failure-classification changes

Retry policy changes are high-risk because they can waste compute or silently
hide bad chemistry. Keep these contributions especially small and test-backed.

Required expectations:

- prefer explicit classifiers over broad fallback behavior;
- fail closed when a safe restart artifact cannot be verified;
- keep ScanTS-specific retry ladders separate from generic ORCA retries;
- do not introduce silent `.xyz`/`.gbw` restarts for ScanTS failures;
- update docs and tests whenever retry caps, reasons, or report fields change.

## Documentation changes

- Keep README examples on the public `orca_auto ...` CLI unless the document is
  explicitly about internals.
- Use placeholder paths such as `/home/user/orca_runs`; never include local
  workstation paths, tokens, chat IDs, or private calculation data.
- If changing behavior documented in `docs/REFERENCE.md`, update both the
  behavior text and any related examples.
- Korean translations are maintained on a best-effort basis; when an English
  public doc changes substantially, note whether the Korean counterpart was
  updated or left for a follow-up.

## AI assistance disclosure

AI tools may be used to draft code, tests, documentation, or reviews. The human
maintainer or contributor remains responsible for reviewing, editing, validating,
and licensing the result. If AI assistance was substantial, disclose it briefly
in the PR body and include the same verification evidence required for any other
change.

Do not paste secrets, private credentials, proprietary ORCA output, or private
research data into AI tools.

## Reporting issues

Please use the issue templates when possible. For calculation failures, include
sanitized `job_state.json`, `job_report.json`, the command used, the calculation
type, and the exact terminal/error marker when available. Remove secrets and
private system paths before posting logs.
