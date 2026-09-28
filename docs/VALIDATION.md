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

`tests/integration/test_orca_worker_smoke.py` holds bounded real-ORCA cases: an H2
single point, electronic-state and input-echo checks, a water optimization and an
ammonia TS/IRC. They run through a real worker only when `ORCA_REAL_EXECUTABLE`
names an ORCA executable, and are skipped otherwise.

Earlier real-engine evidence remains available in the
[concurrent RAM scratch acceptance record](https://github.com/dhsohn/orca_auto/pull/346).
It covers the named ORCA 6.1.1 cases at source revision `2d6cfa7a`, with its stated
limits; it is not a new acceptance run of version 7.

## Regression coverage

Durable identity readers for historical data are retained where needed for
ownership safety. Preserve standalone ORCA tests when moving or removing tests,
including generation fencing, reports, queue crashes, admission accounting and
cancellation.

For a refactor, compare output bytes or behaviors with the prior implementation.
For a removal, search code, dispatch strings, configuration, CI, documentation and
installed consumers; distinguish historical evidence from active behavior.
Run independent review for public contracts, durable ownership or scientific evidence.
Finish with `git diff --check`, source inventory and secret scanning.

Test quality is checked against observable behavior. Expected values come from
fixed contract values or independent input fixtures, not the function under test.
When two readers are compared, also state which inputs must succeed and which
must fail; agreement alone does not prove correctness. Artifact checks require
every reported file to exist before comparing its contents.

Prefer real boundary paths over successful stubs when testing submission or
persistence. Remove wrapper-only duplicates only after locating the behavior test
that protects the same contract. Demonstrate that stronger assertions reject a
representative faulty implementation. Keep static type and ownership checks:
a test module with only type-checking code can still be exercised by mypy.
Environment-gated real-ORCA cases are intentional; impossible parameter
combinations should not be collected as permanent skips.

Tests do not update an installed runtime. Release publication and idle deployment
require the separate checks in [RELEASE](RELEASE.md) and [RUNTIME](RUNTIME.md).

## Scientific evidence regression fixtures

The small H2, water and ammonia calculations in tests/fixtures/orca_6_1_1
retain authentic inputs and complete ORCA 6.1.1 outputs, with SHA256 provenance.
Literal expected energies, convergence and imaginary-mode counts supplement
synthetic edge cases; SCF recovery sequencing currently has synthetic evidence
only. These are parser regressions, not a fresh execution of the current runner.

For the scientific-evidence change, the full repository and distribution gates
are required. A new bounded real-engine run remains pending while both slots in
the operational shared admission store are occupied. Do not bypass that limit.
Historical reports remain untouched. The wheel-only systemd test renders and
applies units to a temporary directory with injected systemctl; it does not
restart the operational services.
