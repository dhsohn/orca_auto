# Validation

## Repository and package gates

Run `make check` in an isolated worktree. It prepares the local environment and
runs lint, formatting, type checks, import boundaries, and all tests with coverage.
Run `make check-packages` whenever installation/distribution behavior changes.
Before a release also run `bash examples/fake_orca_smoke/run.sh`.

The package gate checks wheel and source inventories, sdist-rebuilt wheels,
metadata/CLI versions, fresh external imports, dependency consistency, editable
installation and a prepared immutable runtime. Fake-engine tests exercise durable
submission, real worker subprocesses and terminal output without running chemistry.
The standard pytest suite validates emitted `machine.json` against the pinned
common v1 contract. It requires a `machine-contracts` clone as described in
[DEVELOPMENT](DEVELOPMENT.md); a missing validator fails the test instead of
skipping validation. CI and release checks provide that clone.

## Real-engine acceptance

If ORCA runtime or output-parsing behavior changes, add a bounded calculation using
the supported ORCA executable. Use a disposable input copy, explicit resource limits, external
configuration with notifications disabled, and the normal durable submission path.
Respect shared admission and keep operational input/output untouched.

Record the source revision, interpreter/engine identity, selected input hash,
job/generation identity, resource limits and exact commands. Verify queue ownership
and actual engine output, then inspect the machine receipt and available scientific
evidence. Match acceptance to the job: energy/termination for SP, geometry convergence
for optimization, frequency evidence for stationary points, and endpoint/path evidence
for IRC/NEB. A zero process exit or terminal queue row alone is insufficient.

Do not invent missing evidence or run expensive calculations merely to broaden a
source-only change. State what ran, what did not, and why the chosen acceptance applies.

Earlier real-engine evidence remains available in the
[concurrent RAM scratch acceptance record](https://github.com/dhsohn/orca_auto/pull/346).
It covers the named ORCA 6.1.1 cases at source revision `2d6cfa7a`, with its stated
limits; it is not a new acceptance run of version 7.

## Retirement and regression coverage

Version 7 tests reject removed commands/config and old workflow-owned execution
paths before side effects. Old data and durable identity readers are retained where
needed for ownership safety. Preserve standalone ORCA tests when moving or removing
former mixed test directories, including generation fencing, reports, queue crashes,
admission accounting and cancellation. A stale workflow extension must not appear
in a new package or prepared runtime.

For a refactor, compare output bytes or behaviors with the prior implementation.
For a removal, search code, dispatch strings, configuration, CI, documentation and
installed consumers; distinguish historical evidence from active behavior.
Run independent review for public contracts, durable ownership or scientific evidence.
Finish with `git diff --check`, source inventory and secret scanning.

Tests do not update an installed runtime. Release publication and idle deployment
require the separate checks in [RELEASE](RELEASE.md) and [RUNTIME](RUNTIME.md).
