# ADR 0002: No automatic retry of failed calculations

- Status: Accepted
- Date: 2026-09-05
- Recorded: 2026-09-26
- Amended: 2026-09-28 (resume only by rebind; see [Amendment](#amendment-2026-09-28-resume-only-by-rebind))

## Problem

Before 4.0.0 the attempt engine could rerun a failed ORCA calculation with a
rewritten input. `orca.runtime.default_max_retries` enabled a retry policy
chosen by route type; the budget was recorded in `job_state.json` and queue
metadata, retries were written as `<name>.retryNN.inp`, and each retry sent a
retry notification (`src/orca_auto/orca/retry_policy.py` and
`attempt/retry.py` at `d4117b7c^`). By then only `ScanTS` had a nonzero cap
(3): Opt, Opt+Freq, Freq, single-point and standalone OptTS/NEB-TS routes were
already capped at 0. The ScanTS recipes continued a crashed scan from its last
point or ran one OptTS from the highest surface point after a zero-distance
abort. The retry paths closed a run under their own reasons:
`retry_limit_reached`, `scants_recipes_exhausted` or `rewrite_failed`.

The surface had been shrinking: 0.1.0 moved the ScanTS recipes that fired on
non-failure outcomes into `scan_ts_search` stages, and 0.3.0 removed inert
recipe scaffolding. Changelog entries also fix retry-path defects: a stale
checkpoint seeded after a crash during a retry (0.3.0), a torn `.gbw` seeded as
`MORead` (3.1.0), and retry reasons written over the analyzer verdict (3.1.0;
no live generation carried those reasons).

Issue #298 says the direct ScanTS recovery surface "no longer meets my
reliability requirements" and that no compatibility path should be kept. PR
#299 says "I no longer want to maintain that recovery surface." The record does
not name which reliability requirements failed, and it does not link the
defects above to the decision.

## Decision

PR #299 (`d4117b7c`, CHANGELOG 4.0.0) removes calculation-failure retries,
retry policies, input recipes, retry-input naming, budgets, callbacks and retry
notifications. A failed calculation ends after one attempt, and its final
reason is the analyzer's reason for that attempt (CHANGELOG 4.0.0).

- Config: `orca.runtime.default_max_retries` is an unknown key, and it is
  rejected even when set to 0. There is no alias.
- Snapshots: new execution snapshots use schema 3
  (`ORCA_EXECUTION_SNAPSHOT_VERSION = 3`). A queue row or snapshot that still
  carries `max_retries` is refused with "resubmit the job"
  (`orca/worker_execution.py`, `orca/recovery_rebind.py`). Older submissions
  are neither executed nor converted; they are drained or cancelled and then
  resubmitted.
- Historical data: generations written before the removal stay read-only.
  `retired_generation` in `orca/state.py` detects them, and the state writer
  leaves them untouched. Terminal replay and notification receipts update only
  current-format root bookkeeping. `retrying` remains a status that is only
  read from historical state (`orca/statuses.py`).

PR #299 keeps interruption and checkpoint recovery, explicit workflow restart
(removed later with the workflows, see [ADR 0003](0003-retire-workflows-for-standalone-orca-jobs.md))
and unrelated infrastructure retries such as index publication. A crashed
worker or interrupted host is recovered into a fresh execution generation
(#121); only `interrupted_by_user`, `worker_shutdown` and `crashed_recovery`
are resumable (`RESUMABLE_FAILED_REASONS`), with at most
`RECOVERY_REBIND_LIMIT = 3` rebinds per row. `run-dir --force` starts a new
generation on user request (`docs/PUBLIC_CONTRACTS.md`). The README states the
result as "without unwanted automatic retries of failed chemistry runs."

The same PR removed direct `ScanTS` routes, which are rejected before a
generation or queue row exists. ScanTS was the only route with a nonzero retry
cap, and both removals landed in one PR; this ADR covers the retry part. Plain
relaxed scans remain; `scan_ts_search` remained until 6.0.0 removed it (#337).
The record shows no rejected
alternatives.

## Verification and limits

- `tests/test_single_attempt_contract.py`: eight failure outputs each run once
  with the attempt's analyzer reason, no extra `.inp` and no `max_retries`. The
  attempt API takes no retry parameter, and ScanTS is rejected.
- `tests/test_cli.py::test_standalone_optts_failure_does_not_retry` runs a
  failing OptTS once. `tests/core/test_orca_shared_config_validation.py`
  rejects the removed key, 0 included, and
  `tests/test_orca_execution_binding.py::test_new_snapshot_rejects_retired_policy_field`
  rejects a snapshot carrying it.
- `tests/test_orca_terminal_replay.py::test_retired_generation_is_frozen_across_terminal_replay_and_notification`
  checks that historical generation bytes survive replay and notification.
- `tests/test_orca_crash_recovery.py` checks the separate crash path, including
  a rebind into a new generation and the fail-closed recovery limit.
- These tests passed at `ac852bf9` when this ADR was recorded; CI
  runs the full suite through `scripts/check.sh`.
- The PR #299 record shows a real ORCA 6.1.1 acceptance run. An intentionally
  nonconverged water SCF stopped after one attempt with `scf_not_converged`.
  Its input was unchanged and no retry artifacts were left. An H2 single point
  and a four-point relaxed scan completed.

Limits: the PR does not claim transition-state acceptance or complete method
coverage. A changed input after a failure is a new submission by the user.
Queued work from before 4.0.0 must be resubmitted by hand. The rule covers
calculation failures only: crash recovery still seeds geometry and `MORead`
from the crashed generation (CHANGELOG 0.3.0, `execution_binding/_recovery.py`).

## Amendment (2026-09-28): resume only by rebind

The decision above still lists `interrupted_by_user` and `worker_shutdown` as
resumable reasons. Attempt-level checkpoint resume is now removed: a resumed
run no longer rewrites its input to `<stem>.resume.inp` with `MORead` from the
generation's own `.gbw` (`attempt/resume.prepare_resumed_checkpoint_input` and
`inp_rewriter.prepare_checkpoint_restart_input`), and the `interrupted_by_user`
result with exit code 130 is gone. A run resumes only by rebinding into a fresh
generation.

Why the removed paths could not run in production, traced through the code at
the change:

- The worker child is the only caller of `execute_orca_run`. Before it runs a
  claim, `recovery_rebind.maybe_rebind_recovery_generation` rebinds every
  running claim whose generation shows started-execution evidence, meaning any
  directory entry beyond the snapshot's files. It keeps the generation only
  when its output already verifies as completed, and then the claim settles
  from its recorded attempt or adopts that output without launching ORCA, or
  when cancellation was requested, and then the row is cancelled.
- A run writes the generation's `job_state.json` before ORCA launches, and a
  `<stem>.gbw` beside the bound input is itself started-execution evidence
  (binding forbids a dependency with that name). So a requeued interrupted
  row, whether requeued by a worker shutdown or by orphan reconciliation after
  a lost worker, never executes again in its generation, and the checkpoint
  resume could never find a checkpoint to seed from.
- The child installs its SIGINT and SIGTERM handlers before the run, and the
  run always passes its cancel-or-shutdown check to `OrcaRunner`, so Ctrl-C
  stops ORCA as `WorkerShutdownInterrupt` and the row is requeued. Nothing
  produced `interrupted_by_user`.

The 1124 `job_state.json` files under the production runs root on
2026-09-28 agree: no final result is `resumed`, no attempt ran a
`.resume.inp`, no attempt carries patch actions, no final reason is
`interrupted_by_user` or `worker_shutdown`, and 2 are `crashed_recovery`.

Now `crashed_recovery` is the only resumable failed reason
(`attempt/resume.CRASHED_RECOVERY_REASON`). A resumed state is settled from
its recorded attempt or from its generation's completed output. States that
carry the removed reasons stay readable and are replaced like any other
settled state. Binding still reserves the `<stem>.resume.*` names, and crash
recovery still seeds from a `<stem>.resume.gbw`, for generations written
before the removal.

Verification: `tests/orca/attempt/test_run.py` runs a resumed state without a
recorded attempt on the unchanged input; `tests/orca/attempt/test_resume.py`
resumes only active and `crashed_recovery` states;
`tests/orca/test_orca_runner.py` turns repeated SIGINT into one worker
shutdown; `tests/orca/test_recovery_rebind.py` rebinds a started generation.
Limit: the generation's `job_state.json` is written only while its owner
marker (an extended attribute set at submission) verifies. If the marker was
removed by hand, a run killed while ORCA worked in RAM scratch leaves no
started-execution evidence and its next claim starts the unchanged input again
in that generation, as it did before this change.
