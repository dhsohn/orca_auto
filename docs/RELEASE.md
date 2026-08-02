# Release process

**English only for now.** This file describes the repository operating process;
release notes can be mirrored into Korean documentation later when the release
surface stabilizes.

orca_auto is not currently being prepared for a JOSS submission. This release
process intentionally excludes paper drafting and Zenodo archiving while keeping
the useful open-source software hygiene: issues, focused branches, reviewable
PRs, changelog entries, tags, and reproducible verification.

## Release goals

A release should answer three questions clearly:

1. What user-visible or maintainer-visible problem motivated the release?
2. What changed in CLI/config/report/retry/docs behavior?
3. What verification evidence shows the release is safe to tag?

Use the same structure in release PRs and GitHub release notes:

```text
## Motivation

## Changes

## Verification
```

## Version policy

`pyproject.toml` is the source of truth for the package version.

- Patch version: bug fixes, documentation, tests, CI, or narrow retry/reporting
  hardening that preserves public contracts.
- Minor version: new public CLI/config/report behavior, new workflow surfaces,
  or meaningful contract additions.
- Major version: any change that breaks a behavior documented in
  [docs/PUBLIC_CONTRACTS.md](PUBLIC_CONTRACTS.md).

From 1.0.0 on, every surface in PUBLIC_CONTRACTS is committed, so release notes
must name every documented behavior a release changes and its cutover impact.

## Pre-release checklist

Create a release-prep issue and branch from `origin/main`, then verify:

- [ ] `CHANGELOG.md` has an entry for the release version and date.
- [ ] `pyproject.toml` version matches the changelog entry.
- [ ] `CITATION.cff` `version` and `date-released` match the release
      (`tests/test_release_metadata.py` gates the version; the date is manual).
- [ ] `README.md`, `docs/REFERENCE.md`, and example docs match current public
      CLI/config/report behavior.
- [ ] Any behavior changes have tests and a clear cutover note if needed.
- [ ] `bash scripts/check.sh` passes.
- [ ] `bash examples/fake_orca_smoke/run.sh` passes.
- [ ] A wheel can be built with a Python-module inventory exactly matching
      `src/orca_auto` and one root `orca_auto/py.typed` marker.
- [ ] If ORCA runtime semantics changed, at least one manual real-ORCA
      acceptance check is recorded in the PR.
- [ ] The PR body records Motivation, Changes, and Verification.

Suggested local commands:

```bash
bash scripts/check.sh
bash examples/fake_orca_smoke/run.sh
wheel_dir="$(mktemp -d)"
python -m pip wheel . --no-deps -w "$wheel_dir"
python scripts/check_wheel_contents.py "$wheel_dir"/orca_auto-*.whl
```

Run the wheel commands from a fresh checkout or worktree. Setuptools can reuse an
ignored local `build/` directory; the content checker fails closed if that adds a
deleted/stale module or omits a current source module. Do not tag a wheel until the
inventory check passes from a clean source tree.

## Tagging

After the release PR is merged and `main` is up to date:

```bash
git checkout main
git pull --ff-only origin main
git tag -a vX.Y.Z -m "orca_auto vX.Y.Z"
git push origin vX.Y.Z
```

Then create a GitHub release from the tag. The release body should include the
same three sections:

```text
## Motivation

## Changes

## Verification
```

Do not create a Zenodo archive as part of the current process unless the project
policy changes in a later issue/PR.

## Post-release checks

After the tag and GitHub release exist:

- [ ] Confirm the tag points at the intended merge commit.
- [ ] Confirm GitHub Actions completed for the release commit or tag.
- [ ] On each deployment that runs from an editable install, rerun
      `.venv/bin/python -m pip install -e .` after fast-forwarding: the
      editable metadata is frozen at install time, so without the refresh
      `orca_auto --version` keeps reporting the previous release.
- [ ] Install from the tag in a fresh temporary virtual environment when a user
      report or release risk justifies it.
- [ ] Open follow-up issues for any deferred docs, Korean translations, or manual
      ORCA acceptance gaps.
