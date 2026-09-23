# Release Process

This document describes the release workflow, versioning policy, and checklist for ORCA_auto.

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
complete verification below. The `6.0.0` release records `2026-09-13` in both
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
- [ ] Direct and sdist-rebuilt wheels, installed distribution metadata, and
      `orca_auto --version` match the root `pyproject.toml` version
      (`make check-packages` checks this across its installation profiles).
- [ ] PyPI-facing README images and links use absolute URLs and refer to the
      intended release. `twine check --strict` checks distribution metadata,
      not Markdown link reachability or the rendered page.
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
the current extension supports only CREST-to-ORCA conformer
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
index. From 6.0, release packages are published to PyPI and GitHub Releases;
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

Version `6.0.0` removes
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
artifacts. Publishing 6.0.0 does not update an installed runtime.

## One-time Trusted Publishing setup

The two distributions use separate GitHub Actions environments in one release
workflow, without a stored PyPI API token. Before the first tag, register two
pending publishers on the
[PyPI account Publishing page](https://pypi.org/manage/account/publishing/):

| Field | Core | Workflows |
| --- | --- | --- |
| PyPI project name | `orca_auto` | `orca_auto_workflows` |
| GitHub owner | `dhsohn` | `dhsohn` |
| Repository | `orca_auto` | `orca_auto` |
| Workflow filename | `release.yml` | `release.yml` |
| Environment | `pypi` | `pypi-workflows` |

Pending publishers cannot share an identical owner/repository/workflow/environment
combination, even for different project names. Keep the environments distinct;
this initial-registration restriction differs from linking a normal publisher to
multiple existing projects. See the
[PyPI publisher model](https://github.com/pypi/warehouse/blob/main/warehouse/oidc/models/github.py).

The account owner completes login, identity checks and publisher registration.
Do not put credentials in issues, logs or repository files. A pending publisher
does not reserve a project name; the first successful upload creates the project.
See [PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

In the GitHub repository, configure both `pypi` and `pypi-workflows` to allow only the
selected **tag** pattern `v*`, with no branch deployment rule. This is different
from allowing protected branches. Each environment must match its PyPI
publisher. Tag creation is the maintainer's publication decision;
there is no additional environment approval step in this setup.

## Tagging and automated publication

After the release PR is merged and its CI passes, work from the isolated release
worktree, not a checkout serving active calculations. Fetch and inspect the
intended merge commit before creating the tag:

```bash
git fetch origin main
git show --stat origin/main
# Select only the release-prep merge whose CI has passed.
git switch --detach <verified-release-merge-commit>
git tag -a vX.Y.Z -m "ORCA_auto vX.Y.Z"
git push origin vX.Y.Z
```

Do not move or delete a published tag. The
[`release.yml` workflow](../.github/workflows/release.yml) runs only for pushed
`v*` tags. Its release guard accepts only a stable `vX.Y.Z` that matches the
source versions and points to a commit contained in `main`; development,
prerelease and mismatched tags stop before publication.

The workflow has three job definitions; the PyPI matrix creates one publishing
job per distribution:

1. **Validate and build** has read-only repository access. It runs the standard
   checks, builds and exercises the two distributions with
   `python -m scripts.check_distributions`, checks metadata with
   `twine check --strict`, and retains the four original wheel/sdist files,
   checksums and release notes in one Actions artifact.
2. **Publish to PyPI** downloads that exact artifact ID and verifies its files
   in each matrix job. Only these jobs have `id-token: write`: Core uses `pypi`,
   and Workflows uses `pypi-workflows`. Each job selects only its project's exact
   wheel and sdist into a fresh upload directory, without rebuilding or installing
   project code. Checksums, notes and the other project's files are not uploaded
   by that job. Matrix fail-fast is disabled so one failure does not cancel the
   other publisher mid-upload.
3. **Create GitHub Release** runs only after both PyPI jobs succeed. It checks the
   published PyPI filenames and SHA-256 digests against the retained files,
   confirms the remote tag still names the release commit, and attaches the
   same four distributions and `SHA256SUMS`. It has repository write access,
   but no OIDC publishing permission.

An Actions artifact is an intermediate build result, not proof that a package
index or GitHub release is complete. Follow the post-release checks below.

The release body should include the same three sections:

```text
## Motivation

## Changes

## Verification
```

Do not create a Zenodo archive as part of the current process unless the project
policy changes in a later issue/PR.

## Failed or partial publication

The two PyPI projects and their four files are **not** one atomic transaction.
The workflow stops on an upload error; it does not silently skip existing files,
overwrite release assets or rebuild a replacement set. A failing step and its
Actions run remain the recovery checkpoint. Concurrent runs for the same tag
do not cancel an in-progress publisher.

Before retrying, retain the original run ID, source commit, Actions artifact ID
and downloaded files. Compare each expected filename and SHA-256 digest with
both projects' release JSON endpoints:

- `https://pypi.org/pypi/orca-auto/X.Y.Z/json`
- `https://pypi.org/pypi/orca-auto-workflows/X.Y.Z/json`

Inspect each publisher separately before choosing a retry. A successful Core
job need not be repeated when the Workflows job failed without accepting a file
(or vice versa). Use **Re-run failed jobs**, not a full workflow rerun, to retain
the original build and avoid replaying a successful publisher. Then choose the
matching case for every failed publisher:

- **Neither of its two files accepted:** correct the setup or transient failure
  and rerun only failed jobs so they consume the original build artifact.
- **One of its two files accepted:** stop. A plain rerun will reject duplicate
  filenames.
  After confirming every accepted file matches the original artifact, obtain
  an explicit recovery decision and publish only the missing original files.
  The initial workflow does not automate this recovery. Never delete accepted
  files, replace the tag or rebuild files to make the error disappear.
- **Both of its files accepted, upload job failed:** the final upload response may
  have been lost after PyPI accepted the file. Stop for an explicit recovery
  decision after digest readback. Do not rerun the upload job through duplicate
  files; its failed dependency also prevents the GitHub job from simply running.
- **Both PyPI jobs succeeded, GitHub step failed:** verify the PyPI digests, then
  inspect whether a GitHub release or partial asset upload already exists.
  If no release exists, rerun the failed GitHub job. If one exists, first compare
  its assets with the retained artifact and complete only missing assets under
  an explicit recovery decision; do not overwrite assets.

A digest mismatch, expired build artifact, ambiguous remote state or incomplete
inspection blocks replay. Resolve the discrepancy before publishing anything
else. This bounded manual recovery is intentional; there is no second upload
credential or general-purpose reconciliation service.

## Post-release checks

After publication:

- [ ] Confirm the tag points at the intended merge commit.
- [ ] Confirm GitHub Actions completed for the release commit or tag.
- [ ] Confirm the four distribution assets and their checksums are downloadable.
- [ ] Confirm both PyPI versions expose exactly the expected two files each and
      their digests equal the retained build and GitHub assets.
- [ ] From a new temporary environment outside a checkout, install the exact
      version from PyPI, run `python -m pip check` and `orca_auto --version`,
      and check the Core-only profile before adding the exact matching
      `workflows` extra. Inspect both rendered PyPI descriptions and links.
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
