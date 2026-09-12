# Release process

**English only for now.** This file describes the repository operating process;
release notes can be mirrored into Korean documentation later when the release
surface stabilizes.

ORCA_auto is not currently being prepared for a JOSS submission. This release
process intentionally excludes paper drafting and Zenodo archiving while keeping
the useful open-source software hygiene: issues, focused branches, reviewable
PRs, changelog entries, tags, and reproducible verification.

## Release goals

A release should answer three questions clearly:

1. What user-visible or maintainer-visible problem motivated the release?
2. What changed in CLI/config/report/execution/docs behavior?
3. What verification evidence shows the release is safe to tag?

Use the same structure in release PRs and GitHub release notes:

```text
## Motivation

## Changes

## Verification
```

## Version policy

The root `pyproject.toml` is the version source of truth. The project at
`extensions/workflows/pyproject.toml` must carry the same version, and the core
`workflows` extra and extension dependency must pin that exact pair.

Development versions use an `Unreleased` changelog heading and omit
`date-released` from `CITATION.cff`. Do not invent a release date or tag merely
to align development metadata. A release-prep change replaces both project
versions and dependency pins, dates the changelog and citation, and reruns the
complete verification below. The `5.0.0` release records `2026-09-12` in both
release metadata files.

- Patch version: bug fixes, documentation, tests, CI, or narrow execution/reporting
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
- [ ] Both `pyproject.toml` versions and exact dependency pins match the changelog entry.
- [ ] `CITATION.cff` `version` and `date-released` match the release
      (`tests/test_release_metadata.py` checks development/release metadata separately).
- [ ] `README.md`, `docs/REFERENCE.md`, and example docs match current public
      CLI/config/report behavior.
- [ ] Any behavior changes have tests and a clear cutover note if needed.
- [ ] `bash scripts/check.sh` passes.
- [ ] `bash examples/fake_orca_smoke/run.sh` passes.
- [ ] Core and workflows distributions build independently with exact source
      inventories and no overlapping installed files. Packaged installation
      checks pass for both the core-only and workflow-enabled profiles.
- [ ] If ORCA runtime semantics changed, at least one manual real-ORCA
      acceptance check is recorded in the PR.
- [ ] The PR body records Motivation, Changes, and Verification.

Suggested local commands:

```bash
bash scripts/check.sh
bash examples/fake_orca_smoke/run.sh
make check-packages
```

`make check-packages` runs `python -m scripts.check_distributions`. To retain its
isolated build/install workspace, pass `--work-dir <empty-directory>` directly to
that module. Run packaging checks from a fresh checkout or worktree. Neither
building distributions nor passing their tests publishes them or deploys services.

## Moving from the monolithic 4.x installation

The 5.0 default became core-only. This section describes the package split;
the current development extension supports only CREST-to-ORCA conformer
screening. Its internal xTB engine is separate from that workflow. For the
additional break in 6.0, read the next section
before upgrading. Both packages retain
the existing `orca_auto.*` imports and the `orca_auto` CLI.

Prepare a fresh virtual environment for the split, especially when replacing an
installed monolithic wheel that owned `orca_auto/flow` files. Do not overlay or
remove package files from a live worker environment. From this source checkout,
install the desired profile in the new environment:

```bash
# Core only
python -m pip install -e .

# Core with the matching local workflows extension
python -m pip install -e . -e ./extensions/workflows
```

These source-install commands do not require either distribution on a package
index. For release wheels, download the desired pair from GitHub Releases and
follow the [package installation instructions](INSTALLATION.md).
The `workflows` extra selects the exact matching extension when both
distributions are available to pip; independent version combinations are not
supported.

Keep the existing runtime intact until an idle maintenance window. Retain the
same configuration and durable run state, and do not remove workflow support
from an environment still responsible for workflow state. A core-only queue view
refuses incomplete workflow inspection rather than acting as a migration tool.

Systemd deployment remains separate: the installer needs the chosen checkout's
`systemd/` assets even when invoked from a wheel. Configure the intended worker
interpreter, install the units deliberately, and restart only in the idle window.
Verify the resulting services with `orca_auto service status`. Installing Python
packages alone neither updates those unit files nor replaces running workers.

## Removing TS workflows in 6.0

The current source is unreleased `6.0.0.dev0`. It removes
`reaction_ts_search` (`scaffold ts_search`) and `scan_ts_search`
(`scaffold scan_ts`), their workflow-specific configuration, and automatic TS
search orchestration. Only `conformer_screening` (`scaffold conformer_search`)
remains in the optional extension. Core still accepts user-prepared ORCA
OptTS/Freq, IRC, NEB-TS, and ordinary relaxed-scan jobs; direct ORCA `ScanTS`
remains unsupported.

This is a hard removal, not deprecation. There are no legacy execution paths,
aliases, or automatic state migrations. The new version cannot submit, resume,
or advance the removed workflow types. Existing calculation files and reports
are not deleted or converted by this source change.

Before a separately approved idle-window deployment, finish or explicitly
cancel old workflow work under its existing runtime and retain its original
state and artifacts. Do not point the new workflow worker at a runs root still
responsible for those workflows. Use a fresh runs root for new work if the old
root contains unsupported workflow records; this release does not migrate,
repair, or purge those records. Keep the existing running worker's checkout,
environment, configuration, and services untouched until that cutover.

The published 5.0.0 assets predate this removal and remain historical release
artifacts. Neither this development version nor its documentation publishes a
new release or updates an installed runtime.

## Tagging

After the release PR is merged and its CI passes, work from the isolated release
worktree, not a checkout serving active calculations. Fetch and inspect the
intended merge commit before creating the tag:

```bash
git fetch origin main
git show --stat origin/main
git switch --detach origin/main
git tag -a vX.Y.Z -m "ORCA_auto vX.Y.Z"
git push origin vX.Y.Z
```

Build and verify both wheels and source distributions from that exact tree with
`make check-packages`. Attach the four resulting files and `SHA256SUMS` to the
GitHub release. Verify the uploaded files' digests against the local build, and
confirm the tag resolves to the intended merge commit. This is a GitHub release,
not a PyPI upload; package-index publication is a separate action.

The release body should include the same three sections:

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
- [ ] Confirm the four distribution assets and their checksums are downloadable.
- [ ] Keep publication separate from local deployment. If a runtime must remain
      unchanged, defer all checkout updates, installations and restarts below
      until an explicitly approved idle maintenance window.
- [ ] On each deployment that runs from an editable install, rerun
      `.venv/bin/python -m pip install -e .` for core-only, or
      `.venv/bin/python -m pip install -e . -e ./extensions/workflows` for the full
      profile, after fast-forwarding in an idle window: the
      editable metadata is frozen at install time, so without the refresh
      `orca_auto --version` keeps reporting the previous release. Refresh every
      interpreter that runs the checkout, not just the one on `PATH`: the
      systemd units use the interpreter their unit files name, which need not
      be the `orca_auto` your shell resolves. Confirm each with
      `<interpreter> -m orca_auto.cli service status`, which names the
      interpreter it inspected and exits non-zero while that install still
      trails the checkout.
- [ ] Install from the tag in a fresh temporary virtual environment when a user
      report or release risk justifies it.
- [ ] Open follow-up issues for any deferred docs, Korean translations, or manual
      ORCA acceptance gaps.
