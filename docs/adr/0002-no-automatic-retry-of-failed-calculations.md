# ADR 0002: No automatic retry of failed calculations

- Status: Accepted
- Date: 2026-09-05
- Recorded: 2026-09-26

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
