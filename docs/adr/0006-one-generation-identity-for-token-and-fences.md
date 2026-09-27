# ADR 0006: One generation identity for the persisted token and every queue-row fence

- Status: Accepted
- Date: 2026-09-27

## Problem

A queue row is rewritten by several processes: the submitter publishing its
location record, the worker claiming, requeueing and marking it, the child
reading its cancellation flag and `queue cancel`. Each writer holds a snapshot
of the row it read and must refuse to change a row that has since become a
different generation. Before this decision "same generation" had two
definitions that drifted apart:

- `core/queue/generation.py` computed the `queue_generation` token that
  `job_state.json` records. Its key set was hard-coded there with ORCA and
  retired-workflow keys and exempted six metadata keys no live row carried
  (`attempt`, `candidate_count`, `retained_conformer_count`, `execution_dir`,
  `terminal_artifacts`, `terminal_repair_blocked_reason`); on 2026-09-27 none
  of the 47 rows in the production `queue.json` had any of them.
  `queue_entries_same_generation` compared rows by that token.
- `orca/queue/adapter.py` fenced the publication writers with
  `_immutable_publication_metadata`, which ignored `enqueued_at` but counted
  the queued-notification flag, and the dequeue claim compared the whole row
  for exact equality.

The two answers disagreed on real rows. A `queue cancel` that read a pending
row before the worker claimed its queued notification was refused and
reported the job as "already terminal", because the claim rewrote a flag the
fence counted as identity (commit bc6bd405, pin
`tests/contracts/pins/queue_cancel_after_notification_claim.json`).

## Decision

`orca/queue/entries.py` owns one identity:

- `LIFECYCLE_METADATA_KEYS` lists the metadata a row's lifecycle rewrites:
  the admission deferral, the run ID, the terminal replay marker and fence,
  the queued-notification claim and the six publication lease keys, listed
  explicitly instead of the former prefix match.
- `generation_identity(entry)` is the queue ID, app, task ID, task kind,
  engine, priority, `enqueued_at` and the metadata without those keys.
  `queue_entry_generation_token` is the SHA-256 of its canonical JSON and is
  what `job_state.json` stores as `queue_generation`; `same_generation`
  compares the identity.
- Every adapter writer and both cancellation probes (through
  `_same_orca_generation`), the publication repair and submission fences,
  direct cancel's confirmations and the dequeue claim compare this identity
  and add only their own status rule.

Removed: core `queue_entries_same_generation`, the dequeue's exact-equality
`expected_entry`, `adapter.queue_entries_same_publication_generation` and
`_immutable_publication_metadata`, and the six exemptions for dead keys, which
now count as identity like any other key.

`enqueued_at` is now part of every fence's identity; the publication fence
used to ignore it. No path that rewrites an existing row changes it: requeue,
admission deferral, orphan reconciliation, publication repair and lease
writes, the queued-notification claim, cancel, the claim itself, metadata
updates and the terminal marks all keep it, and only `adapter.enqueue` sets it
when a row is created.

## Verification and limits

- `tests/contracts/pins/queue_generation_identity.json` holds one
  `same_generation` column in place of the former two.
  `tests/contracts/pins/queue_writer_fences.json` changed in 94 of 1552
  cells: snapshots that differ only in the queued-notification flag or in
  other lifecycle data of a claimable row are now accepted, and snapshots
  that differ only in `enqueued_at` are now refused. Deferred and
  cancel-requested rows and every identity difference stay refused. Every
  other golden and pin was unchanged by that commit.
- `tests/orca/queue/test_entries.py` covers the token and the key set.
- `tests/core/queue/test_ownership_guards.py` fails when a `QueueEntry` is
  constructed outside `entry_from_dict` and `adapter.enqueue`, or when a
  `replace()` call sets `enqueued_at`.

Limits: the token is opaque and comparable only within one major version, and
nothing rewrites the values 8.x recorded. Only the activity projection
compares it, for an active row that has no run ID; completed rows match by run
ID. Rows queued under 8.x keep their stored value, and a pending row claimed by
the new child records the new token. A job still running across an upgrade
outside an idle window keeps an 8.x token that no longer matches: `queue list`
shows it twice (its queue row and a run-state row) and `queue cancel <run ID>`
cannot find it until it finishes, while `queue cancel <queue ID>` still works.
An upgrade in an idle window sees no difference (`docs/RELEASE.md`).
