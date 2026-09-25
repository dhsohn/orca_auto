# ADR 0004: Concurrent RAM scratch under a summed memory guard

- Status: Accepted
- Date: 2026-09-23
- Recorded: 2026-09-26

## Problem

RAM scratch was added in 0.3.0 with "a root lock admits one scratch attempt"
(CHANGELOG 0.3.0). Before #345, `core/engine_scratch.py` refused any launch
while a workspace with a live owner existed (`Another engine scratch attempt is
active`), and its `MemAvailable` check covered only the launching job: own
task-memory cap + free tmpfs + `scratch_min_free_gb` (`2d6cfa7a^`).

With RAM scratch enabled, raising `scheduler.max_active_simulations` above 1 made
the second admitted job fail before ORCA started, as `runner_exception` (#345).
The recorded negative control reproduced this on the acceptance host with
`max_active_simulations: 2` (`docs/VALIDATION.md` at `9c215d2d`). #345 states the
goal as running more than one calculation at a time on a host with enough
memory while keeping fail-closed handling of unresolved workspaces.

## Decision

The one-workspace rule is removed (#345, merged 2026-09-17, released in 7.0.0).
Scratch admission is owned by `EngineScratchWorkspace.create` in
`core/engine_scratch/_workspace.py`, under the scratch-root lock:

- `_sweep_scratch_root` finishes interrupted cleanup tombstones, raises on any
  workspace that blocks launch, and returns the summed task-memory caps of
  proven-live workspaces.
- The launch requires tmpfs free space ≥ `scratch_min_free_gb` + input size, and
  `MemAvailable` ≥ own cap + live caps + free tmpfs + `scratch_min_free_gb`. The
  shared tmpfs pool counts once; live caps count in full because those jobs may
  still grow to them (code comment in `create`).
- Workspace manifest schema 2 records `max_task_memory_bytes`
  (`_constants.py`). Owner classification is `live`, `stale` or `unknown`
  (`_manifest.py`). A schema-1 manifest, a missing or non-positive cap
  (`invalid-manifest`), a dead owner or other boot (`stale`), or an owner that
  cannot be verified (`unverifiable`) blocks every later launch instead of
  being counted.
- The root-lock wait is 300 s instead of the 10 s default, so an attempt queues
  behind a peer's staging or publication recovery (#345, `_constants.py`).

A capacity refusal no longer fails the job (#348, merged 2026-09-17). Only the
capacity checks and the root-lock timeout raise `EngineScratchCapacityError`,
and only with no workspace left behind. `OrcaRunner.prepare()` reserves the
workspace before the first state write; on refusal the worker child returns the
row to `pending`, records `admission_deferral` with a fixed 60 s `not_before`,
and exits 75 (`core/queue/deferral.py`, `orca/worker_execution.py`). #348 gives
the reason for reserving early: the job state and "started" notification
already existed when the refusal surfaced in `run()`, and requeueing then would
spend the crash-recovery rebind budget. A read-only pre-check was rejected
because two children started back to back would both pass it. A blocking
workspace and other scratch errors still fail the attempt through the ordinary
path (`test_other_scratch_failures_still_fail_the_row`). #351 made `queue list`
show `(waiting for resources)` and `admission_deferral_reason` for pending ORCA
rows.

Operators resolve blockers with `orca_auto scratch list` and `scratch clear NAME
| --all-stale`, which remove `stale`, `unverifiable` or `invalid-manifest`
workspaces and refuse live ones (#357, `docs/PUBLIC_CONTRACTS.md`). Other
options for the memory accounting are not recorded.

## Verification and limits

`tests/test_orca_scratch.py` covers two workspaces coexisting and cleaning up
independently, a live cap charged to the next launch, a capless schema-1
manifest, an invalid manifest, an unverifiable owner and a stale workspace each
blocking, and the lock timeout as a capacity refusal. `tests/test_scratch_capacity_deferral.py` covers the deferral, including
a worker-child run with a fake ORCA binary that defers and later completes in
the same generation with no rebind (#348), and the `queue list` display (#351).

Real-engine acceptance (ORCA 6.1.1, WSL2, 102 GiB RAM), recorded by #346 and
#351 and condensed in current `docs/VALIDATION.md` to a link to #346: two
66-atom scans ran concurrently at `2d6cfa7a` with two schema-2 manifests, and a
32 GiB-cap attempt was later admitted beside a running 16 GiB one; two benzene
`Opt Freq` jobs with 47 GiB caps showed one waiting about a minute and then
completing in the same generation. Neither covered more than two attempts, a
tmpfs-space or busy-root refusal, or a job that can never fit. Current
`docs/VALIDATION.md` states this is not a new acceptance run of version 7.

Accepted limits (#345, #348): the guard is conservative, since each live cap is
added in full although its RSS is already excluded from `MemAvailable`; the cap
is an `RLIMIT_AS` per launched process, so MPI ranks each inherit it; the guard
is an admission check, not a memory quota (`config/orca_auto.yaml.example`); a
job whose cap never fits waits until cancelled, and smaller jobs may overtake
it; a SIGKILL between reservation and launch leaves a stale workspace that
blocks the root until cleared.
