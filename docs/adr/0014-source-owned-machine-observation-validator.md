# ADR 0014: Source-owned machine observation validator

- Status: Accepted
- Date: 2026-10-03
- Extends: [ADR 0001](0001-one-public-machine-json-per-generation.md)

## Problem

`machine.json` conformance tests read `scripts/validate.py` out of a
`dhsohn/machine-contracts` clone at the pin
`bc252035d01edddf1314e6641689c6d5cb88af92` (`tests/machine_contract_helpers.py`
before this change). CI checked that repository out and the release workflow
cloned it, both exporting `FACTORY_MACHINE_CONTRACT_REPO`. A clean clone could
not pass the standard test suite without a second repository, and the shipped
wheel had no way to validate its own observations. With the clone path pointing
at a missing directory, a valid ORCA observation failed with `cannot provide CI
pin` (`tests/orca/test_state.py::test_common_machine_validation_needs_no_machine_contracts_clone`).

## Decision

`orca_auto.machine_contracts` owns the ORCA_auto subset of the v1 contract:

- Byte copies of `schemas/machine-observation-v1.schema.json`,
  `schemas/payloads/chemistry-results-bundle-v1.schema.json` and the upstream
  MIT `LICENSE` from the pin, with `PROVENANCE.md` recording each file's SHA-256.
- `validator.py`, derived from upstream `scripts/validate.py` with its checks and
  messages, and the two `orca_auto` registry routes (`chemistry/orca-run`,
  historical `chemistry/workflow`) plus the `chemistry/results-bundle` v1 entry,
  copied verbatim. Other producers are rejected as unregistered routes. An
  artifact path escaping the generation is reported as such instead of the
  upstream "on another volume" label; both refuse it.

The nine envelope fields, receipt semantics and payload are unchanged; no
producer code calls the validator, and import-linter forbids `orca_auto.core`
and `orca_auto.orca` from importing it. `jsonschema` is imported only when a
document is validated and comes from the optional `validation` extra (also in
`dev`); the runtime dependencies stay `PyYAML` alone. A missing `jsonschema`
raises `ImportError`; validation is never skipped. CI and release no longer
check out or clone `machine-contracts`.

## Verification and limits

`tests/machine_contracts/test_validator.py` pins the packaged schema, licence
and upstream fixture hashes, accepts the upstream ORCA fixtures and
failed, running and payload-less observations, and rejects schema, route,
payload, receipt, lineage, duplicate-key, basename and artifact byte, hash and
confinement faults, including a checked-in invalid fixture. The test helper and
its tooling tests fail on a rejection or a missing `jsonschema`, and never start
a subprocess. `tests/tooling/test_package_data.py` requires every non-Python
package file to be declared package data, and the workflow test refuses an
external checkout. `scripts/check_distributions.py` installs the wheel and the
sdist-rebuilt wheel with `[validation]` and checks that the installed validator
accepts the fake-worker `machine.json` and rejects an altered artifact.

Upstream changes are not picked up automatically: updating the contract means
copying the new files, their hashes and the ORCA registry entries by hand. The
validator checks structure and receipts, not scientific validity.
