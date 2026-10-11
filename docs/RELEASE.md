# Release process

## Version and preparation

The root `pyproject.toml` is the version source of truth. Public contract removal
requires a major release; fixes use patch releases and compatible additions use
minor releases. ORCA_auto 7.0 retired workflow support and shipped one package.
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
checkout. They also verify a prepared immutable runtime and, through the
`validation` extra, the installed `machine.json` validator and its packaged
schemas; release needs no `machine-contracts` clone. Check metadata with
`twine check --strict`; inspect release README links and images separately.
If ORCA runtime behavior changes, record bounded real-engine acceptance as
described in [VALIDATION](VALIDATION.md). Tests and package builds do not deploy.

## Upgrading to 10.2

Version 10.2 adds queue Detail labels and fixes input metadata and final-coordinate
parsing. It removes or renames no public field, status, reason, CLI option or
configuration key. Completion criteria and coarse job types are unchanged.

- Consumers of `queue list --json` may see the expanded `detail_kind` vocabulary;
  `unsupported` displays as `Other`, while uncertain or legacy values display as
  `Unknown`. These are presentation labels, not execution or success evidence.
  Existing queue rows are not backfilled; see the
  [Detail contract](PUBLIC_CONTRACTS.md#queue-list-detail-column).
- An incomplete final coordinate table no longer supplies stale or partial
  geometry to a report. Geometry metadata is empty and no SI block is produced
  when final coordinates cannot be established. Final energy and recorded
  completion status retain their existing meaning. Historical terminal
  observations are not regenerated or reclassified.

In an idle window:

1. Wait for `queue list --json` to show `active_simulations: 0` and for all
   admission reservations to clear.
2. Retain the current runtime, units and configuration for rollback.
3. Install 10.2 in a new environment or [prepared runtime](RUNTIME.md).
4. Restart under the admission guard and verify `service status --json` against
   the running worker's version, build and source root.

There is no state migration. To roll back, restore the retained runtime and units
in an idle window; observations already published by 10.2 remain as written.
Publishing the package performs none of these deployment steps.

## Upgrading to 10.1

Version 10.1 is a minor release. It removes or renames no field, status, reason,
CLI option or configuration key, and `completed` keeps its 10.0 meaning
([PUBLIC_CONTRACTS](PUBLIC_CONTRACTS.md#scientific-evidence-and-compatibility)).
Review two narrower behaviors before switching:

- The last `VIBRATIONAL FREQUENCIES` section decides frequency evidence. 10.0.0
  could complete a run on an earlier or truncated section when the last section
  printed no supported value or an unsupported one such as `NaN cm**-1`. Such a
  run now fails with `frequency_evidence_missing` and is not retried. Runs
  finished under 10.0.x keep their recorded status.
- `run-dir` refuses an input whose active `%maxcore` has no readable value in MB.

After an optional report fails, `machine.json` can carry the additive
`payload.data.results.report_generation` key. The HTML report and SI block word
constrained TS searches and IRC evidence more narrowly. The `validation` extra
and `orca_auto.machine_contracts` are optional, and running jobs does not need
them.

Switch as for any release:

1. Wait for `active_simulations: 0`.
2. Keep the 10.0.x runtime, units and configuration.
3. Install 10.1 in a new environment or [prepared runtime](RUNTIME.md).
4. Restart under the guard and verify `service status --json` against the
   running worker.

10.1 adds no state file or queue field. To roll back to 10.0.x, reinstall the
retained runtime and units in an idle window; observations published by 10.1
stay as written. Publishing the package performs none of these steps.

## Upgrading to 10.0

Version 10.0 changes a public contract: a run is `completed` only with present
completion evidence (a finite final single-point energy, final optimization
convergence for Opt/TS, a final frequency section when Freq is requested), and a
run without it fails ([ADR 0012](adr/0012-positive-scientific-completion-evidence.md),
[PUBLIC_CONTRACTS](PUBLIC_CONTRACTS.md#scientific-evidence-and-compatibility)).
It also adds the optional Slack provider; Discord settings are unchanged
([ADR 0016](adr/0016-slack-notification-provider.md)). Publishing the package
performs none of the steps below.

Before the maintenance window:

1. Review consumers of `queue list --json`, `job_state.json` and `machine.json`
   that select `completed` runs or resubmit `failed` ones. After the switch some
   runs that 9.0.x would have completed fail instead, and none of them is retried
   automatically. Runs finished under 9.0.x keep their recorded status; nothing
   reclassifies them.
2. Keep the current 9.0.x runtime (its environment or prepared runtime
   directory), its installed units and its configuration file unchanged so that
   they can be restored.

In the idle window:

3. Wait until `queue list --json` shows `active_simulations: 0` and
   `<runs_root>/.admission/admission_slots.json` holds no reserved or active
   slot; `service restart` checks again under the admission lock. No job may run
   across the switch. Then stop the 9.0.x worker and every submitter (`run-dir`
   callers, scripts and automation) so that nothing writes the queue or job
   states while the snapshot is taken; take it only while no slot is reserved or
   active and no submission, cancellation or result publication is unfinished.
4. Copy this frozen pre-cutover evidence to a dated location outside `runs_root` and
   keep it with the retained runtime, units and configuration: the
   configuration file, `<runs_root>/queue.json`,
   `<runs_root>/.admission/admission_slots.json`,
   `<runs_root>/job_locations.json` and every `job_state.json` under
   `runs_root` (job roots and generation directories), keeping their relative
   paths. Do not move or delete calculation data.
5. Install 10.0 and its units as for any release (a new environment or a
   [prepared runtime](RUNTIME.md)), restart under the guard and verify
   `service status --json`: the running worker's version, build and source root
   must match the installed unit, and the worker must have started after the
   last configuration change.

After the switch:

- `queue_generation` digests are comparable only within one major version. Do
  not compare 9.x and 10.x digests, and do not let a 9.0.x worker take over rows
  or job states written by 10.0 unless their compatibility has been shown.
- Rolling back to 9.0.x is an explicit owner decision made in an idle window.
  Stop the 10.0 worker; a 9.0.x worker must not continue work that 10.0
  started. Before restoring anything, copy aside every `job_state.json`, result
  and queue row that 10.0 created or changed, and leave the job and generation
  directories in place. Then restore together the step 4 copies (`queue.json`,
  `admission_slots.json`, `job_locations.json` and every `job_state.json`), the
  9.0.x configuration (9.0.x rejects `messenger.provider: slack`) and the
  retained 9.0.x runtime and units. Never restore the old `queue.json` alone on
  top of job states that 10.0 changed. Keep the 9.0.x worker and submitters
  stopped after restoring. Before any 9.0.x worker starts, compare every
  restored `pending` row (queue ID, job directory, generation and selected
  input identity) with the 10.0 outcomes and generation evidence copied aside.
  A row proven not claimed, run or resubmitted under 10.0 may resume as that
  same existing row, not as a new submission or generation, but only by an
  explicit owner decision after this reconciliation is complete. A row that
  10.0 already ran, completed or advanced stays blocked, and so does a row
  whose identity cannot be matched unambiguously; settle those only through
  supported queue commands such as `queue cancel`, after an explicit owner
  decision, and never by editing a status by hand. Nothing is replayed or
  requeued automatically. Start the 9.0.x worker only after every restored
  pending row is either approved to resume or settled. This procedure relies on the
  pre-cutover copies, not on 9.0.x reading state written by 10.0, and it
  deletes no data.

## Upgrading to 9.0

Version 9.0 changes public contracts (the CHANGELOG entries marked *public
contract*) and needs an idle-window cutover; publishing the package alone
performs none of these steps. No job may run across the switch: the queue
generation token ([ADR 0006](adr/0006-one-generation-identity-for-token-and-fences.md))
and the writer of a cancelled result
([ADR 0008](adr/0008-parent-writes-the-cancelled-result.md)) changed, and the
worker child takes its admission slot token only from `--admission-token`. The
idle window ensures that a 9.0 worker never supervises or settles a child that
an 8.x worker started.

Before the maintenance window:

1. Cancel or clear every pending or running row that belongs to old workflow
   work, and move workflow trees that must stay untouched out of `runs_root`
   ([ADR 0005](adr/0005-remove-retired-workflow-support.md)). A directory that
   holds `flow.yaml` or `workflow.json`, or lies under one, becomes an ordinary
   directory: the new worker claims and runs its rows, `index rebuild` records
   its ORCA job states and `queue list clear` removes the `job_state.json` of
   its terminal runs.
2. Update scripts ([ADR 0010](adr/0010-queue-commands-read-queue-rows.md)).
   Replace `queue list --refresh` with `orca_auto index rebuild` followed by
   `queue list`; a run state without a queue row is no longer listed.
   `queue cancel --json` reports its outcome in `result`, which is now
   `{status, reason, queue_id, job_id, reaction_dir}` with `reason` one of
   `target_not_found`, `ambiguous`, `already_terminal` and `cancel_failed`; a
   target that names no row or several rows prints the whole document with
   empty row fields. Journal filters that match a logger name follow the
   renamed loggers listed in the CHANGELOG.

In the idle window:

3. Wait until `queue list --json` shows `active_simulations: 0` and
   `admission_slots.json` in the configured admission store
   (`scheduler.admission_root` if still set, else `<runs_root>/.admission`)
   holds no reserved or active slot; `service restart` checks again under the
   admission lock. A job still
   running across the switch keeps its 8.x `queue_generation`: `queue list`
   shows it without its run ID and `queue cancel <run ID>` cannot find it
   until it finishes. Upgrading directly from 7.0.x also needs the empty slot
   file, because the new version rejects a slot row that carries the retired
   `workflow_id` field.
4. Delete `scheduler.admission_root` from the config
   ([ADR 0007](adr/0007-one-admission-store-under-runs-root.md)). Admission
   state always lives in `<runs_root>/.admission` and its limit is
   `scheduler.max_active_simulations`; a config that still sets the key,
   including one that followed the 7.0 steps below with a separate root, no
   longer loads. The old directory may then be deleted.
5. Install the new version and reinstall its units as for any release: a new
   environment or a [prepared runtime](RUNTIME.md), then
   `orca_auto systemd install --user USER --repo <repo>`, which loads the
   config and stops with a one-line hint while the removed key is present.
   `ReadWritePaths` in the rendered unit names only `runs_root`. Restart under
   the guard with `orca_auto service restart` and verify
   `service status --json` against the running worker. The restart may refuse
   because the edited config is newer than the worker's start or because
   `<runs_root>/.admission/admission.lock` does not exist yet; after confirming
   the host is idle, run `orca_auto service restart --force`, or stop and start
   the service.

After the switch:

6. Run `orca_auto index rebuild` to record in `job_locations.json` the runs
   that `queue list` no longer shows, including those under leftover workflow
   trees.
7. Once `service status --json` shows the new worker, the unused
   `<runs_root>/.activity.sqlite3*` files, `<runs_root>/.activity-query.lock`
   and `<runs_root>/.activity-dirty/` may be removed by hand.

Rolling back to 8.x:

- Roll back in an idle window too. 8.x counts the queued-notification flag in
  `queue_generation` again, so a job the new version started that is still
  running shows the same effects in reverse.
- Before 8.x starts, delete every `<runs_root>/.activity.sqlite3*` file, the
  database and its `.activity.sqlite3-journal` alike, so that 8.x rebuilds the
  projection from disk instead of trusting one this version did not update.
- The config needs no change: 8.x uses the same `<runs_root>/.admission` when
  `scheduler.admission_root` is absent. Reinstall the 8.x units with its own
  `systemd install` and restart through the same idle sequence.

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
are not deleted or migrated. In 7.x and 8.x, retired workspace markers are used
only to refuse new execution and protect old data. Do not submit an old workflow stage as an
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
