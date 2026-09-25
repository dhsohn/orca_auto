# Release process

## Version and preparation

The root `pyproject.toml` is the version source of truth. Public contract removal
requires a major release; fixes use patch releases and compatible additions use
minor releases. ORCA_auto 7.0.0 retires workflow support and ships one package.
Development metadata uses `Unreleased` and omits the citation release date.
Release metadata must agree in `pyproject.toml`, `CHANGELOG.md` and `CITATION.cff`.

Prepare a focused branch based on current `origin/main`, preserving any approved
work already in progress. Open a release-prep issue and PR with these sections:
`Motivation`, `Changes`, `Verification`. Use first-person singular or objective
language. Record contract breaks and observed checks; do not add a Zenodo archive.

Run in an isolated worktree:

```bash
make check
bash examples/fake_orca_smoke/run.sh
make check-packages
```

Package checks build the wheel/sdist, rebuild from the sdist, verify exact source
and installed inventories, and exercise ORCA in fresh environments outside the
checkout. They also verify a prepared immutable runtime. Check metadata with
`twine check --strict`; inspect release README links and images separately.
If ORCA runtime behavior changes, record bounded real-engine acceptance as
described in [VALIDATION](VALIDATION.md). Tests and package builds do not deploy.

## Upgrading to 8.0

Version 8.0 removes public contracts and needs an idle-window
cutover; publishing the package alone performs none of these steps.

- The worker unit `ExecStart` no longer passes `--app orca`, and the new
  worker rejects that option. A unit that still carries it cannot start the new
  code: an automatic restart after a crash or a reboot would hit the systemd
  start limit, and `service restart` refuses such a unit. Install the new units
  before anything restarts: during an idle window (`active_simulations: 0`) run
  `orca_auto systemd install --user USER --repo <repo>` (it rewrites `ExecStart`
  without going through the restart guard), then restart under the guard and
  verify `service status --json` against the running worker as in
  [RUNTIME](RUNTIME.md).
- Rolling back to 7.0.x: admission slot rows written by the new version omit the
  retired `workflow_id` field, which 7.0.x readers require. Released slots are
  removed from `admission_slots.json`, so roll back only in an idle window with
  no reserved or active slots; do not carry a slot file with new-format rows
  back to 7.0.x.
- Configuration discovery no longer probes a checkout-local
  `config/orca_auto.yaml`; only `--config`, `ORCA_AUTO_CONFIG` and
  `~/orca_auto/config/orca_auto.yaml` are consulted. Move a checkout-local file
  to `~/orca_auto/config/orca_auto.yaml`, or pass it with `--config` or
  `ORCA_AUTO_CONFIG`, before restarting. `systemd install --config` defaults to
  the target user's `~/orca_auto/config/orca_auto.yaml`.
- `queue worker --app`, `queue list --engine` and `queue list --kind` are
  removed; drop them from scripts. `service status --json` no longer carries
  `version_drift`.

## Upgrading to 7.0

This release removes all workflow support: the `orca_auto_workflows` extension,
conformer scaffolds, xTB/CREST execution, workflow workers, `workflow` configuration,
workflow queue filters/grouping and workflow-only options. `run-dir` accepts ORCA
input directories; resource overrides are supplied through `%pal`/`%maxcore`.

Existing calculation files, reports, historical releases and old installations
are not deleted or migrated. Retired workspace markers are used only to refuse
new execution and protect old data. Do not submit an old workflow stage as an
ordinary ORCA job or redirect a new worker at unfinished workflow work.

1. Finish or explicitly cancel all pending/running workflow work using its
   original version. Preserve its input, state, output, configuration and runtime.
2. Wait for an idle maintenance window. Verify `active_simulations: 0` using the
   current installation; do not alter a checkout or environment serving live jobs.
3. Stop and disable any old `orca_auto-workflow-worker@USER.service` instance.
   New unit installation does not remove old installed service files. Retain the
   old runtime and units needed for rollback; do not leave the retired worker enabled.
4. Create a fresh environment or [prepared runtime](RUNTIME.md) with
   `orca_auto==7.0.0`. Do not upgrade an environment that still contains the old
   extension. Copy configuration outside the runtime and remove the entire
   `workflow` section; the new parser rejects it even when empty.
   Use queue/admission state dedicated to standalone ORCA. If the existing root
   still contains retired queue rows, snapshot intents or admission records,
   choose separate `runs_root` and `scheduler.admission_root` paths for 7.0 and
   preserve the old root with its original runtime; do not migrate those records.
5. Install the new ORCA units using that configuration, restart under the idle
   guard and verify `service status --json` against the actual running build.
   Check the ORCA queue and artifacts after cutover. Keep historical workflow
   directories intact; copy only deliberately selected inputs into a new standalone
   job directory when a new calculation is intended.

Publishing 7.0.0 alone performs none of these operational steps.

## Publishing

After the release PR merges and its CI passes, fetch `origin/main` in the release
worktree and inspect the intended merge commit. Never tag an active runtime checkout.

```bash
git fetch --no-tags origin main
git show --stat origin/main
git tag -a vX.Y.Z <verified-merge-commit> -m "ORCA_auto vX.Y.Z"
git push origin vX.Y.Z
```

The release workflow accepts only a stable tag matching source metadata and a
commit contained in `main`. Published tags must never be moved or replaced.

1. The read-only build job runs standard checks, tests distributions and retains
   the tested wheel/sdist, `SHA256SUMS` and release notes in one Actions artifact.
2. The `pypi` environment publishes only those exact two files using Trusted
   Publishing. It alone has OIDC permission. Register the `orca_auto` PyPI project
   for owner `dhsohn`, repository `orca_auto`, workflow `release.yml`, environment
   `pypi`; restrict the GitHub environment to tag pattern `v*`.
3. The GitHub release job verifies PyPI filenames/digests and the unchanged remote
   tag, then attaches the same two distributions and checksum file. It has
   repository write permission without OIDC publishing permission.

The retired extension publisher is not used. Account authentication and publisher
registration are performed by the account owner without sharing credentials.

## Failed or partial publication

Keep the run ID, source commit, artifact ID and original files. Compare remote
filenames and SHA-256 digests with `https://pypi.org/pypi/orca-auto/X.Y.Z/json`.

- No files accepted: resolve the failure, then rerun only failed jobs using the
  original retained artifact.
- One file accepted, or both accepted but the upload response failed: stop after
  digest readback and obtain a recovery decision. Publish only a missing original
  file; do not rebuild, overwrite, delete or replay an already accepted upload.
- PyPI succeeded and the GitHub step failed: check for an existing release and
  partial assets. If absent, rerun the failed GitHub job. Otherwise reconcile only
  missing original assets under an explicit recovery decision.

Ambiguous state, digest mismatch or an expired artifact blocks replay.

## Publication verification

Verify the tag/merge commit and successful Actions run. Download both GitHub
distributions and `SHA256SUMS`; compare their digests with the retained build and
the exact PyPI version. From a fresh environment outside a checkout, install
`orca_auto==X.Y.Z`, run `pip check`, `orca_auto --version`, and standalone smoke
checks. Verify the removed module, commands, extra and unit are absent.

Record publication separately from local deployment. A deployment is complete
only when the actual process build/root matches its installed unit; follow
[RUNTIME](RUNTIME.md) for preparation, idle cutover, status and rollback.
