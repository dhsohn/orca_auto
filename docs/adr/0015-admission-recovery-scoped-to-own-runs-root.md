# ADR 0015: Admission recovery and compaction scoped to the worker's own runs root

- Status: Proposed
- Date: 2026-10-02
- Extends: [ADR 0007](0007-one-admission-store-under-runs-root.md)

## Problem

ADR 0007 gives an installation one admission store, `<runs_root>/.admission`.
The bounded real-engine acceptance helper binds its private runs roots to the
operational store by making `<runs_root>/.admission` a symlink to it, so that
its jobs respect the operational capacity. The worker's recovery pass then
treated the whole store as its own: `recover_orphaned_engine_slots` could
signal the engine group of another runs root's slot whose owner had died,
and `list_slots`, `reconcile_stale_slots` and every live-slot mutation
(`reserve_slot`, `update_slot_metadata`, `activate_reserved_slot`) rewrote the
file without that root's dead-owner records. The real Astra review recorded
this as blocker B2; the regression
`tests/test_astra_shared_admission_scope.py` reproduced the foreign SIGTERM.

## Decision

A worker and its children recover and drop only the admission records they
own: those whose `work_dir` resolves inside their own `runs_root`
(`core.admission.runs_root_ownership`, a resolved-path comparison, never a
text prefix). A record without a `work_dir`, or with one that cannot be
resolved, is not owned and is left as written.

- `recover_orphaned_engine_slots`, `list_slots`, `reconcile_stale_slots`,
  `reserve_slot`, `activate_reserved_slot`, `update_slot_metadata` and the
  store's live mutation take a keyword-only `owned` predicate. Without it they
  behave exactly as before; the queue worker and the worker child pass their
  runs root's predicate.
- Records that are not owned are never signalled, cleared, normalized or
  deleted by that worker, and still count toward the limit under the same
  liveness rule as before: a slot with a live owner, or with a pending or
  active engine record, occupies capacity.
- The store, its lock, its file format, the limit, token-scoped recovery of the
  worker's own jobs, cancellation, and the process-start, boot and
  queue/task/generation fences are unchanged.

Rejected: a private admission store for the acceptance run (it would bypass
the shared capacity), stopping operational submitters, replacing recovery with
a stub, and a configuration switch (one root per installation needs none).

## Verification and limits

`tests/test_astra_shared_admission_scope.py` runs the real worker against two
temporary runs roots that share one temporary store, with recording-only
signal fakes: foreign dead-owner engine and pending records and a foreign stale
idle record stay unchanged and unsignalled, foreign occupancy still blocks
admission at the limit, a reservation below the limit leaves foreign records
unchanged, and the worker's own orphaned engine is still stopped and its slot
removed. The sibling root shares the own root's name as a text prefix.

Limits: in a single-root installation every attached slot lies inside the
runs root, so recovery is unchanged; the one difference is that a dead-owner
reservation that never received a `work_dir` (a worker that died between
reservation and attach) is no longer deleted. It does not count toward the
limit. Sharing one store across runs roots remains outside the supported
configuration of ADR 0007; this decision only keeps such a worker from acting
on records it does not own. No live engine, operational store or concurrent
production run has been used to verify it.
