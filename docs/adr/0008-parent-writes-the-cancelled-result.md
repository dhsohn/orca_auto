# ADR 0008: The worker parent is the one writer of a cancelled result

- Status: Accepted
- Date: 2026-09-27

## Problem

A cancelled running job's `job_state.json` got its `cancelled` `final_result`
from two processes. The worker child caught the stop
(`WorkerShutdownInterrupt`), asked the queue whether cancellation was
requested and, under `run.lock`, wrote the cancelled result itself
(`worker_execution._finalize_cancelled_run_state`) before it marked its row
cancelled with the replay marker. When `run.lock` was held it skipped the
write and logged `Skipping cancel finalization of …; run lock is held`. The
parent then settled the cancelled row and called
`terminal_state.record_cancelled_run_state` anyway: that writer is required
for a child that was SIGKILLed or died before it could write anything, and
for a restart replay after the parent itself died. When the child had
already written, the parent found the terminal result and rewrote the same
state with it (the double write in
`tests/contracts/golden/04_cancel_running/effects.json`).

The two writers followed different rules. The parent's writer checks, under
`run.lock`, that the state still belongs to the cancelled generation
(`terminal_marker.terminal_generation_verdict`); the child's wrote to
whatever state it loaded whenever that state had no `final_result`.

## Decision

`terminal_state.record_cancelled_run_state`, called by the parent's
settlement (`settlement.prepare`) when it settles a cancelled row live or on
restart replay, is the only writer of a cancelled `final_result`. The worker
child writes only the attempt and scratch evidence it writes for any
interrupted attempt; on a stop it marks its row cancelled with the replay
marker when cancellation was requested, or returns the row to the queue
otherwise, and exits.

Removed: `worker_execution._finalize_cancelled_run_state`, the child's
cancellation query after the interrupt, and its `Skipping cancel
finalization` warning.

## Verification and limits

- `tests/contracts/golden/04_cancel_running/effects.json`: the child's two
  `job_state.json` writes (`generation` and `root`, `cancelled`) are gone; the
  child's `cancelled+replay` queue write now directly follows its slot update,
  and the parent's settlement writes the generation state before the root
  state it already wrote. The scenario's `queue.json`, `job_state.json`,
  report, `job_locations.json` and notification goldens are unchanged, so the
  settled files are the same.
- `tests/orca/queue/test_settlement_faults.py` settles a cancelled child that
  ignored SIGTERM and was SIGKILLed (`cancel_killed`) and a cancelled row
  whose parent died before it wrote the state, replayed by a fresh worker
  (`cancel_restart`), under a fault at each settlement step.
- `tests/orca/test_worker_execution.py::test_cancelled_child_leaves_the_cancelled_result_to_the_parent`
  runs a real child through a cancellation: the state stays `running`, the
  row is cancelled with a marker that observed that state, and the parent's
  settlement of the row writes the `cancelled` result.
- A read-only check on 2026-09-27 hashed the 85 `job_state.json` files under
  the production runs root that record a cancelled result (84 ORCA generation
  states with reason `cancel_requested` and one CREST state with exit code
  143) before and after this change. None of their directories had a queue
  row for their job, an active row or a replay marker, so no settlement path
  can select them; the hashes were unchanged.

Limits: between the child's exit and the parent's settlement, `job_state.json`
still says `running` behind a cancelled queue row that shows `result
publication pending`. After a parent crash that lasts until the next worker
start replays the row. States that already record a cancelled result are not
rewritten.
