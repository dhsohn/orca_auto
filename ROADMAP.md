# ORCA_auto roadmap

ORCA_auto focuses on durable execution of user-prepared ORCA inputs on Linux/WSL.
Release history belongs in [CHANGELOG](CHANGELOG.md), supported behavior in
[PUBLIC_CONTRACTS](docs/PUBLIC_CONTRACTS.md), and release steps in
[RELEASE](docs/RELEASE.md).

## Current scope

One package handles standalone ORCA submission, execution, cancellation and
reporting. Version 7 removed the workflows extension, conformer orchestration,
internal xTB/CREST engines, scaffolds and workflow workers. Version 9 removes
the remaining workflow handling: a directory that holds workflow files is an
ordinary directory ([ADR 0005](docs/adr/0005-remove-retired-workflow-support.md)).

Standalone ORCA Opt/Freq, OptTS, IRC, NEB-TS and ordinary relaxed scans remain
supported. Users design the chemistry inputs and assess the scientific results.

## Maintenance priorities

- Keep submission durable and recovery explicit. Calculation failures remain
  terminal without automatic reruns.
- Make queue, admission and generation ownership straightforward to inspect.
- Keep reports tied to verified artifacts and expose missing evidence honestly.
- Prefer a small public surface, direct ownership and simple installation.
- Keep English and Korean contracts aligned. Record real-engine acceptance
  separately from fake-engine CI when engine behavior changes.

## Scope

New capabilities require a deliberate scope decision. The project does not aim
to replace ORCA, chemical judgment, a cluster scheduler or a general workflow
platform. Native Windows execution and site-specific lab scripts are outside its
supported surface. Retired workflow execution and compatibility aliases are not
part of it.

For changes, identify the affected public contract, use a focused issue and PR,
and record the checks that demonstrate the resulting behavior. Public contract
breaks require a major release; release publication and operational deployment
remain separate steps.
