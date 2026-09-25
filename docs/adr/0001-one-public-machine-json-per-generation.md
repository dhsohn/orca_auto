# ADR 0001: One public machine.json per generation

- Status: Accepted
- Date: 2026-08-10
- Recorded: 2026-09-26

## Problem

Before 2.0.0 the public machine-readable result of an ORCA generation was
`<generation>/job_report.json`. It used the same normalized engine artifact
shape as the private `job_state.json` (`schema_version`, `engine`, `job`,
`status`, ... `engine_payload`), and its `artifacts` map was an extensible
capability map of path keys with no byte or hash binding
(`docs/PUBLIC_CONTRACTS.md` at `9504153f^`). For workflows the contract listed
`workflow.json` as the durable workflow payload.

Success consumers (workflow orchestration, runtime job locations) trusted a
`succeeded` status alone; #188 changed them to require a consumable
observation, with handoff `ready` and delivery `complete`. The record does
not describe an observed failure caused by the old report, and does not state
why the shared `dhsohn/machine-contracts` envelope was chosen over extending
`job_report.json`.

## Decision

Every ORCA generation and every terminal workflow root publishes exactly one
public machine metadata file, `machine.json` (#188, `9504153f`; CHANGELOG
2.0.0):

- The envelope is `factory/machine-observation` v1 with nine top-level fields
  (`contract`, `producer`, `operation`, `lifecycle`, `handoff`, `delivery`,
  `artifacts`, `lineage`, `payload`) and a `chemistry/results-bundle` v1
  payload. Generations use operation kind `chemistry/orca-run`; workflow roots
  used `chemistry/workflow`.
- Artifact receipts bind generation-relative paths to exact byte counts and
  SHA-256. A successful run is ready only when every required receipt is
  available; otherwise it is `succeeded / blocked / incomplete`.
- HTML and SI files are written first, `machine.json` last, and a terminal
  observation is immutable. A junk or symlinked `machine.json` fails the
  advance closed; re-entering a published generation leaves its reports
  untouched.
- The public `job_report.json` is removed. `job_state.json` and `workflow.json`
  remain private recovery state, and `job_state.json` was documented at
  `9504153f` as an implementation detail, "not a Hermes handoff contract".
- CI validates emitted observations against `dhsohn/machine-contracts` pinned
  at `bc252035d01edddf1314e6641689c6d5cb88af92`.

Removing a documented public file made this a major release (#189). No
alternatives are recorded.

## Verification and limits

CI checks out the pinned `machine-contracts` commit and runs `scripts/check.sh`
with `FACTORY_MACHINE_CONTRACT_REPO` set (`.github/workflows/ci.yml`); the
release workflow does the same (`.github/workflows/release.yml`).
`tests/machine_contract_helpers.py` reads the validator from the pinned commit
and fails, rather than skips, when the clone, the commit or `jsonschema` is
missing (#361, CHANGELOG Unreleased). In 2.0.0 the check was optional: the test
helper validated only when `FACTORY_MACHINE_CONTRACT_VALIDATOR` was set, and CI
ran two named tests.

Conformance tests today: `tests/test_state.py::test_write_report_files_json_fields`,
`test_common_machine_validation_rejects_changed_input_receipt` (a changed input
byte fails with `artifact sha256 mismatch`), and the fake-ORCA worker
lifecycle and preflight-failure tests in
`tests/integration/test_orca_worker_smoke.py`. Several tests assert that no
`job_report.json` is written (`tests/test_state.py`,
`tests/test_run_inp_submission.py`).

Later changes:

- 3.0.3 spells `MACHINE_OBSERVATION_FILE` out instead of aliasing the internal
  `RUN_REPORT_JSON_FILE`, since both are `machine.json` but answer to different
  contracts (`58c47c74`, #244).
- 7.0.0 removed workflows (#353,
  [ADR 0003](0003-retire-workflows-for-standalone-orca-jobs.md)). No new workflow-root `machine.json` is
  produced, and the workflow conformance tests left CI. Historical workflow
  directories remain read-only data (`docs/RELEASE.md`).
- The Unreleased changes add an `execution-provenance` artifact bound through
  `machine.json`; historical reports are not backfilled (#359).
- The condensed `docs/PUBLIC_CONTRACTS.md` (`5499b4c1`) no longer states that
  internal state files are never alternate public contracts; section 4 names
  `machine.json` as the artifact for downstream tools.

The contract covers structure and receipts. Completion is not a guarantee
that every numerical property converged, and scientific acceptance remains
the researcher's responsibility (`docs/PUBLIC_CONTRACTS.md` section 4).
