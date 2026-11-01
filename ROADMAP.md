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

- Keep submission durable and recovery explicit; calculation failures remain terminal without automatic reruns.
- Keep queue, admission and generation ownership inspectable on disk.
- Tie reports to verified artifacts and report missing evidence explicitly.
- Keep the public surface small and English/Korean contracts aligned.
- Record real-engine acceptance separately from fake-engine CI when engine behavior changes.

## Scope

ORCA_auto does not aim to replace ORCA, cluster schedulers, or general workflow engines, and does not support native Windows execution or site-specific lab scripts.

Changes should name the affected public contract and record verification checks in a focused issue and PR. Public contract breaks require a major release; release publication and operational deployment remain separate steps.
