# Contributing to ORCA_auto

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md) ([한국어](CODE_OF_CONDUCT.ko.md)) in all project interactions.

## Project scope

ORCA_auto runs standalone ORCA calculations through a persistent local queue on Linux and WSL. It is not a general workflow engine, an ORCA replacement, or a collection of site-specific scripts.

Contributions generally cover:

- durable queue submission, cancellation, and worker supervision;
- ORCA state, report, and provenance files;
- fail-closed execution and interruption recovery;
- documentation, examples, validation, and release checks.

## Development workflow

1. Open or reference a GitHub issue stating the problem.
2. Create a focused branch or worktree from `origin/main`.
3. Open a pull request before merge.
4. Let CI and applicable local or manual checks pass before merging.

Suggested branch prefixes:

- `fix/...` for behavior fixes;
- `docs/...` for documentation and project-hygiene changes;
- `test/...` for regression coverage;
- `ci/...` for CI-only changes;
- `refactor/...` for behavior-preserving internal cleanup.

## Pull request narrative

Use these sections in every PR:

### Motivation

State the user problem, failure mode, or maintenance gap. For ORCA runtime changes, name the calculation class or queue state involved.

### Changes

List code, documentation, test, or configuration changes, calling out public surface changes (CLI flags, config keys, report fields, execution behavior, output layout).

### Verification

List exact commands run and their results. Claim real-ORCA acceptance only when a real ORCA binary was used. For docs- or template-only changes, run the relevant checks and note that runtime behavior did not change.

Example:

```text
## Verification

- `bash scripts/check.sh tests/orca/attempt/test_single_attempt_contract.py -q` — passed
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

The bootstrap script creates `.venv`, installs the package editable, and writes
the configuration template to `~/orca_auto/config/orca_auto.yaml` when that file
does not exist; a checkout-local `config/orca_auto.yaml` is not discovered.

`make test` runs `scripts/check.sh`, which creates or repairs `.venv`, installs
`.[dev]`, then runs Ruff, Ruff format check, mypy, import-linter, the docs
parity gate (`scripts/check_docs_parity.py`) and the coverage-gated pytest suite. The repository ships one distribution, `orca_auto`.
For a manual development install, use `python -m pip install -e '.[dev]'`.
Run `make check-packages` for package or installation changes. Version 7 removes
the former workflows extension; see the [upgrade guide](docs/RELEASE.md#upgrading-to-70).

The `machine.json` conformance tests use the validator in
`src/orca_auto/machine_contracts/`, a source-owned copy of the ORCA_auto part of
`dhsohn/machine-contracts` at `bc252035d01edddf1314e6641689c6d5cb88af92`
([ADR 0014](docs/adr/0014-source-owned-machine-observation-validator.md)). No
clone, git or network access is needed. The tests fail if `jsonschema` is
missing; `make check` installs it with the development dependencies.

For a narrower loop:

```bash
bash scripts/check.sh tests/orca/attempt/test_single_attempt_contract.py -q
bash scripts/check.sh tests/orca/test_worker_execution.py -q
```

Run the fake-ORCA smoke test without a licensed ORCA binary:

```bash
bash examples/fake_orca_smoke/run.sh
```

## Git hooks (optional)

Versioned hooks live in `.githooks/`. Enable them once per clone:

```bash
git config core.hooksPath .githooks
```

- `pre-commit` runs the fast checks from `scripts/check.sh` (Ruff lint and format, mypy, import-linter) on the staged snapshot, setting aside unstaged and untracked files during the check. Fully staged Python files are formatted and re-staged automatically.
- `pre-push` runs the full `scripts/check.sh` (including coverage-gated pytest) against `.venv`. It refuses to push when tracked changes or non-ignored untracked files exist, or when pushing a ref other than `HEAD`.

Both require `.venv` (run `scripts/check.sh` once first) and can be bypassed with `git commit --no-verify` or `git push --no-verify`.

## ORCA, and path policy

- Use absolute Linux paths for configured executables and runtime roots.
- Do not add support for Windows drive paths, `/mnt/<drive>/...` executable paths, relative executable paths, or `.exe` binaries.
- CI uses fake engines and sanitized fixtures and must not require a licensed ORCA installation.
- Include real-ORCA acceptance checks in PR verification when a change depends on ORCA runtime semantics.

## Execution and failure-classification changes

Changes to execution or recovery must:

- keep every calculation failure terminal after one attempt;
- reject unsupported routes before queue/generation publication;
- preserve verified worker/host interruption recovery;
- keep historical artifacts immutable and verify their receipts when reading;
- update both public-contract translations and focused regression tests.

## Documentation changes

- Keep README examples on the public `orca_auto ...` CLI unless the document is explicitly about internals.
- Use placeholder paths such as `/home/user/orca_runs`; never include workstation paths, tokens, channel IDs, or private calculation data.
- When changing behavior in `docs/REFERENCE.md`, update both prose and examples.
- Update the English and Korean counterparts together whenever user-facing behavior or a public contract changes.

## Reporting issues

Use the issue templates when possible. For calculation failures, include the sanitized terminal `machine.json`, command run, calculation type, and terminal/error marker, plus a sanitized `job_state.json` excerpt if relevant to recovery state (`job_state.json` is internal metadata, not a result contract). Remove secrets and private paths before posting.
