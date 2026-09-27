# ADR 0009: Resume only by rebinding into a fresh generation

- Status: Accepted
- Date: 2026-09-28
- Supersedes: [ADR 0002](0002-no-automatic-retry-of-failed-calculations.md)

## Problem

[ADR 0002](0002-no-automatic-retry-of-failed-calculations.md) kept
interruption and checkpoint recovery: `interrupted_by_user`, `worker_shutdown`
and `crashed_recovery` were resumable failed reasons
(`RESUMABLE_FAILED_REASONS` in `orca/state.py` at `dfb655ee`). A resumed run
continued in its own generation. The attempt engine rewrote its input to
`<stem>.resume.inp` with `MORead` from the generation's own `.gbw`
(`attempt/resume.prepare_resumed_checkpoint_input` and
`inp_rewriter.prepare_checkpoint_restart_input`), and a Ctrl-C that reached
the attempt closed the run as `interrupted_by_user` with exit code 130.
Binding refused a dependency named like the resume input or its outputs
(`<stem>.resume.*`), the names that path wrote into the generation, and crash
recovery seeded a replacement generation from the newer intact one of
`<stem>.gbw` and `<stem>.resume.gbw`.

None of these paths could run in production. Traced through the code at the
change:

- The worker child is the only caller of `execute_orca_run`. Before it runs a
  claim, `recovery_rebind.maybe_rebind_recovery_generation` rebinds every
  running claim whose generation shows started-execution evidence, meaning any
  directory entry beyond the snapshot's files. It keeps the generation only
  when its output already verifies as completed, and then the claim settles
  from its recorded attempt or adopts that output without launching ORCA, or
  when cancellation was requested, and then the row is cancelled. A claim it
  cannot rebind, for example at the rebind limit, is rejected.
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

The production runs root agreed on 2026-09-28. Of its 1124 `job_state.json`
files, no final result is `resumed`, no attempt ran a `.resume.inp`, no
attempt carries patch actions, no final reason is `interrupted_by_user` or
`worker_shutdown`, and 2 are `crashed_recovery`. None of its 46197 files is
named `*.resume.*`.

## Decision

A run resumes only by rebinding into a fresh generation. A generation runs
ORCA at most once: its one attempt runs the unchanged bound input, and a claim
whose generation shows started execution is rebound before it runs unless its
completed output settles it.

`crashed_recovery` is the only resumable failed reason
(`attempt/resume.CRASHED_RECOVERY_REASON`). The claim continues its root
`job_state.json` only when that state belongs to the same bound input and was
left active by a crash or closed as `crashed_recovery`
(`attempt/resume.recover_crashed_state`). Such a state is settled from its
recorded attempt or from its generation's completed output; without either,
the attempt runs the bound input unchanged. States that carry the removed
reasons stay readable and are replaced like any other settled state.

Removed:

- the `<stem>.resume.inp` restart input and its rewriter, and the
  `interrupted_by_user` result with exit code 130;
- `interrupted_by_user` and `worker_shutdown` as resumable reasons;
- binding's reservation of the `<stem>.resume.*` names;
- crash recovery's seeding from `<stem>.resume.gbw`. Recovery seeds `MORead`
  only from the crashed generation's `<stem>.gbw`, and claim-time verification
  accepts only that name as a recovery checkpoint's source.

The rest of ADR 0002 stands: a failed calculation ends after one attempt, and
crash recovery still seeds geometry and `MORead` from the crashed generation.

## Verification and limits

- `tests/orca/attempt/test_run.py::test_resumed_run_without_a_recorded_attempt_runs_the_selected_input_unchanged`
  runs a resumed state without a recorded attempt on the unchanged input, and
  `test_resumed_terminal_attempt_finishes_without_running_again` settles one
  from its recorded attempt.
- `tests/orca/attempt/test_resume.py::test_only_active_and_crash_recovered_states_are_resumable`
  resumes only active and `crashed_recovery` states.
- `tests/orca/test_orca_runner.py::test_repeated_sigint_during_cleanup_is_one_worker_shutdown`
  turns repeated SIGINT into one worker shutdown.
- `tests/orca/test_recovery_rebind.py::test_rebind_moves_crashed_claim_into_new_generation`
  rebinds a started generation, `test_recovery_build_seeds_scf_checkpoint`
  seeds `MORead` from `<stem>.gbw`, and
  `test_verify_rejects_checkpoint_rename_contract_violation` refuses any other
  checkpoint source.
- The read-only check of the production runs root above: no generation holds a
  resume input or checkpoint, so no queued snapshot seeds from one.

Limits: the generation's `job_state.json` is written only while its owner
marker (an extended attribute set at submission) verifies. If the marker was
removed by hand, a run killed while ORCA worked in RAM scratch leaves no
started-execution evidence and its next claim starts the unchanged input again
in that generation, as it did before this change.
