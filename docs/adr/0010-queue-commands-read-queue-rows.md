# ADR 0010: Queue commands read queue rows directly

- Status: Accepted
- Date: 2026-09-28
- Supersedes: [ADR 0006](0006-one-generation-identity-for-token-and-fences.md)

## Problem

`queue list` was served by a SQLite projection at
`<runs_root>/.activity.sqlite3` (`core/activity_index.py`,
`activity/_orca_index.py`), queried under `<runs_root>/.activity-query.lock`.
Writers kept it current: queue and job-location saves mirrored their rows
after the commit (`published_source` in `core/queue/persistence.py` and
`core/indexing/store.py`), and the state writer and `queue list clear` wrote an
invalidation ticket under `<runs_root>/.activity-dirty/` inside the state
mutation lock (`core/activity_invalidation.py`, called from
`orca/state.save_state` and `orca/run_cleanup.clear_terminal_run_states`). The
ticket was written before the state, so a failed ticket write failed the state
save of a running calculation: a read model sat inside the execution-critical
state write.

The projection merged `queue.json`, `job_locations.json` and the root
`job_state.json` of every related directory, and it listed a run state that no
queue row owned as a row of its own. `queue cancel` did not use it. It built a
second catalog from the same merge on disk, picked a row with
`activity/_cancel.match_activity_record`, and then called
`orca/direct_cancel.cancel_target`, which read the queue again and matched the
row's queue ID with a second matcher, `queue/adapter.find_entry_by_target`.
`direct_cancel` wrapped its answer in an emulated subprocess envelope
(`returncode`, `command_argv`, `stdout`, `stderr`, `parsed_stdout`), which
`queue cancel --json` printed as `result`.

The production runs root, read on 2026-09-28, held 47 queue rows and 1030
job-location rows. 47 of the location rows name a queue row's directory, 275
name a directory that no longer exists, and none of the other directories holds
a root `job_state.json`. The location index therefore added no row to the
listing; it only added reads.

## Decision

`queue list` and `queue cancel` share one catalog, `activity/_orca.catalog`:
the ORCA rows of `queue.json`, each joined with the root `job_state.json` of
its own directory when that state belongs to the row's run ID or, for an
active row without one, to its generation token
([ADR 0006](0006-one-generation-identity-for-token-and-fences.md)). Nothing
else is read: not `job_locations.json` and no other directory. A run state
without a queue row is not listed. `index rebuild` still records such runs in
`job_locations.json`; `queue list --refresh`, which ran the same rebuild before
listing, is removed.

`queue cancel` resolves its target once over that catalog
(`activity/_cancel.target_rows`). A target equal to a row's queue ID or run ID
(the one the row records, or a running row's from its own state) names that
row before any other match, as `match_activity_record` let an exact ID win
over aliases. Otherwise a target matches a row's job ID or directory, given as an absolute
path, a path relative to `runs_root` or to the working directory, or its name.
Matches in several directories are ambiguous. Within one directory the active
generation is the target, two active generations are ambiguous, and with none
active the newest finished row in list order answers. A target that, relative
to the working directory, is an existing directory that none of the matched
rows has is ambiguous too, and that directory is listed among the matches: a
name never falls back from the directory it names to another directory's row.
The resolved row is cancelled through `queue/adapter.cancel`, fenced on its
generation; when that call refuses or raises, a read of the row decides whether
the cancel of that generation is durable.

`queue cancel --json` keeps its top-level keys (`ok`, `activity_id`, `kind`,
`engine`, `source`, `label`, `status`, `cancel_target`, `error`). `result` is
`{status, reason, queue_id, job_id, reaction_dir}`, with `reason` one of
`target_not_found`, `ambiguous`, `already_terminal` and `cancel_failed` on
failure. A target that names no row or several rows now prints the same
document with empty row fields instead of `{"ok": false, "error": ...}` alone.
The text output is unchanged, and so are the rows and `active_simulations` of
`queue list --json`. `admission_blockers` lists the blocked rows in
`queue.json` order and then the admission-store entry; the projection returned
them in its storage order, so only a listing with several blocked rows can
differ.

Removed: `core/activity_index.py`, `core/activity_invalidation.py`,
`activity/_orca_index.py`, `orca/direct_cancel.py`, the writers' mirror and
ticket calls, `adapter.find_entry_by_target` with `AmbiguousQueueTargetError`
and its alias helpers, the run-state row rules of `orca/run_status.py` and the
lock and location-index options of `run_snapshot.collect_run_snapshots`. The
record shows no alternative that was weighed.

## Verification and limits

- `tests/activity/test_orca.py` covers the catalog join
  (`test_catalog_joins_queue_rows_with_their_own_state_only`,
  `test_catalog_lists_no_run_state_without_its_queue_row`), the target rule
  (`test_target_rows_select_active_then_newest_terminal`,
  `test_target_rows_prefer_a_queue_or_run_id_to_any_alias`,
  `test_cancel_activity_path_alias_prefers_active_generation`,
  `test_cancel_activity_by_state_run_id_of_running_job`,
  `test_cancel_by_name_never_passes_a_directory_of_that_name`,
  `test_cancel_by_queue_id_wins_over_a_directory_of_that_name`), the blocker
  order (`test_admission_blockers_follow_queue_row_order`) and the payload
  (`test_cancel_activity_routes_orca_targets`,
  `test_cancel_activity_reports_an_empty_or_unknown_target`).
  `tests/activity/test_orca_discovery.py::test_listing_reads_neither_the_location_index_nor_the_run_tree`
  fails when a listing reads the location index or walks the run tree.
- The `queue list` goldens under `tests/contracts/golden/cli` are unchanged.
  The `queue cancel` goldens there and in `03_cancel_pending` and
  `04_cancel_running` record the new `result`, and `argparse_tree.json` no
  longer has `--refresh`.
- A read-only copy of the production runs root above (`queue.json`,
  `job_locations.json`, `admission_slots.json` and the 47 root states) was
  listed by the code before and after this change, as JSON and text, with and
  without status filters and limits, with the running job's run lock held and
  free. With the running job's state carrying the current generation token, the
  outputs were identical. With the token its 8.x worker recorded, the earlier
  code also listed that state as a second, run-state row, the upgrade effect
  ADR 0006 describes, and this change lists the queue row alone.

Limits: a run whose root state has no queue row is not listed, whether its row
was cleared, edited out of `queue.json` or never existed. Such a run's
directory still blocks a bare name: `queue cancel foo` run inside `runs/x`,
which holds `runs/x/foo` without a queue row, is refused as ambiguous even when
one queued `foo` elsewhere is the only row of that name; run it from another
directory or name the job by queue ID or path. A job still running from a
worker of an earlier major version is listed once, without its run ID, so
`queue cancel <run ID>` cannot find it until it finishes; its queue ID and
directory still name it. The second, run-state row and the ambiguous directory
cancel that ADR 0006 describes for such a job no longer occur. The files
`.activity.sqlite3*`, `.activity-query.lock` and `.activity-dirty/` under
`runs_root` are no longer read or written and may be deleted. 8.x would trust
a projection that this version stopped updating, so a rollback to 8.x first
deletes every `.activity.sqlite3*` file, the database and its journal alike,
and lets 8.x rebuild the projection from disk.
