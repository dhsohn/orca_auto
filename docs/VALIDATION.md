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
The standard pytest suite validates emitted `machine.json` with the source-owned
`orca_auto.machine_contracts` validator, a pinned copy of the common v1 contract
([ADR 0014](adr/0014-source-owned-machine-observation-validator.md)). It needs no
external clone; a missing `jsonschema` fails the test instead of skipping
validation. The package gate installs the wheel and the sdist-rebuilt wheel with
the `validation` extra and checks that the installed validator accepts a fake-worker
`machine.json` and rejects an altered artifact.

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

Skip real-engine runs for changes that cannot affect ORCA execution or output parsing, and record which checks ran and why.

`tests/integration/test_orca_worker_smoke.py` holds bounded real-ORCA cases: an H2 single point, electronic-state and input-echo checks, a water optimization and an ammonia TS/IRC. They run through a real worker only when `ORCA_REAL_EXECUTABLE` names an ORCA executable, and are skipped otherwise.

Earlier ORCA 6.1.1 acceptance evidence for concurrent RAM scratch is in the [concurrent RAM scratch acceptance record](https://github.com/dhsohn/orca_auto/pull/346) (source revision `2d6cfa7a`).

## Regression coverage

Retain durable identity readers needed for historical ownership safety, and preserve standalone ORCA tests (generation fencing, reports, queue crashes, admission accounting, cancellation) when reorganizing tests. Remove wrapper-only duplicate tests only after finding the behavior test that protects the same contract.

Require independent review for changes to public contracts, durable ownership, or scientific evidence. For refactors, compare output bytes or behavior against the prior implementation. For removals, check code, dispatch strings, configuration, CI, documentation and installed consumers, distinguishing historical records from active behavior. Finish with `git diff --check`, source inventory and secret checks.

Assert observable behavior against fixed contract values or independent fixtures rather than the function under test, and demonstrate that stronger assertions reject a representative faulty implementation. When comparing two readers, check both valid and invalid inputs, and verify reported files exist before comparing contents. Prefer real boundary paths over stubs for submission and persistence tests; keep static type and ownership checks (`mypy` exercises type-only test modules); and avoid collecting impossible parameter combinations as permanent skips (environment-gated real-ORCA cases are intentional skips).

Tests do not update an installed runtime; see [RELEASE](RELEASE.md) and [RUNTIME](RUNTIME.md) for publication and idle cutover checks.

## Scientific evidence regression fixtures

The small H2, water and ammonia calculations in `tests/fixtures/orca_6_1_1` retain authentic inputs and complete ORCA 6.1.1 outputs with SHA-256 provenance. Literal expected energies, convergence verdicts and imaginary-mode counts supplement synthetic edge cases (SCF recovery sequencing has synthetic fixtures only). `tests/orca/test_atom_mode_association.py` pins atom identity, order and normal-mode association on the water and ammonia outputs; outputs with dummy/ghost/embedded atoms or numerical/partial Hessians are not covered.

Run both the full repository gate (`make check`) and the distribution gate (`make check-packages`) for scientific-evidence changes.

For 10.2.3 acceptance (source revision `482b4631bf354b402c12d42c4192c3bdaab363a7`, ORCA 6.1.1), water HF/STO-3G Opt calculations with `%freq AnFreq true` and `%freq NumFreq true` ran through the durable queue and worker (1 core, 512 MB maxcore), converging at `-74.965901189921 Eh` with nine frequencies and zero imaginary modes and passing `machine.json`, artifact-receipt, HTML and SI checks. This single water case does not establish general numerical-Hessian atom/mode association or extend supported ORCA versions.

For 10.2.4 acceptance (source revision `96b03e4531b10cf83addfaf926cb1327fbfdf4b1`, ORCA 6.1.1), the eight environment-gated real-ORCA cases in `tests/integration/test_orca_worker_smoke.py` passed. Water HF/STO-3G Opt calculations with a route-line `Freq` request and with `%freq AnFreq true` ran through the durable queue and worker (1 core, 512 MB maxcore), converging at `-74.965901189921 Eh` with nine frequencies and zero imaginary modes and passing `machine.json`, artifact-receipt, HTML and SI checks. `index rebuild` from scratch on those runs recorded their requested resources. The run held one shared operational admission reservation, left operational configuration and engine bytes unchanged, and released the reservation. It does not extend supported ORCA versions or numerical-Hessian coverage.
