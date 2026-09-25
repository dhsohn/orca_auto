# ADR 0003: Retire workflows for standalone ORCA jobs

- Status: Accepted
- Date: 2026-09-23
- Recorded: 2026-09-26

## Problem

Before 5.0.0, one monolithic package carried standalone ORCA execution and
multi-stage workflows: conformer screening (CREST to ORCA), automatic TS-search
and scan-TS workflows, scaffolds and internal xTB/CREST engines (CHANGELOG
5.0.0, 6.0.0).

5.0.0 (2026-09-12, #331, issue #330) split the repository into two same-version
distributions: core `orca_auto` and optional `orca_auto_workflows` at
`extensions/workflows`, which owned `orca_auto.flow`. The stated goal was that
standalone ORCA users could install the durable queue runtime without the
workflow implementation. Workflow state failed closed when the extension was
absent (CHANGELOG 5.0.0).

6.0.0 (2026-09-13) removed `ts_search` / `reaction_ts_search` and `scan_ts` /
`scan_ts_search` with no aliases, migration or resume support (#337, issue
#336). Issue #336 says these workflows "no longer fit the supported product
direction", and #337 says the author no longer wanted to maintain them or
compatibility-only paths for them. Conformer screening and the internal xTB
engine remained, and 6.0.0 still published matched Core/Workflows releases
(#343).

For 7.0.0, issue #352 states the aim as keeping ORCA_auto "focused on durable
execution of standalone ORCA inputs", and PR #353 carries it out. The record does not give a
further reason for removing the remaining conformer workflow and engines, such
as an observed failure or a measured maintenance cost.

## Decision

ORCA_auto 7.0.0 (#353, squash `637cabc3`) is a single package for standalone
ORCA jobs. It removes (CHANGELOG 7.0.0, `docs/RELEASE.md` "Upgrading to 7.0"):

- the `orca_auto_workflows` extension and the `extensions/workflows` tree,
  conformer screening orchestration, scaffolds, internal xTB/CREST engines,
  workflow workers and the workflow service;
- the `workflow` configuration section, which is rejected even when empty;
- workflow queue filters and grouping, workflow-only worker flags, and the
  `run-dir` resource overrides `--max-cores` / `--max-memory-gb`. Resources come
  from the ORCA input (`%pal` / `%maxcore`). The record does not state why the
  overrides were removed.

ORCA is the only engine (`docs/ARCHITECTURE.md`); standalone Opt/Freq, OptTS,
IRC, NEB-TS and relaxed-scan jobs remain supported (`ROADMAP.md`).

Existing workflow directories, reports and historical releases are kept, not
deleted or migrated. They are read-only to version 7:
`core/paths/retired.py` treats a directory as retired when it or an ancestor
under the runs root contains `flow.yaml` or `workflow.json`. Unreadable marker
evidence counts as retired. `queue_entry_is_retired_workflow_owned` also treats
a queue row carrying a `workflow_id` in its metadata as retired, which covers
historical rows without marker files (#353). Submission, `run-dir`, worker
preparation, recovery rebind, cancellation and cleanup refuse or skip such
targets and leave their files unchanged.

Upgrading is a manual, idle-window cutover (`docs/RELEASE.md`): finish or
cancel workflow work on its original version, stop and disable
`orca_auto-workflow-worker@USER.service`, install 7.0.0 into a fresh
environment or prepared runtime, and remove the `workflow` config section. If
the old root still holds retired queue rows, snapshot intents or admission
records, use separate `runs_root` and `scheduler.admission_root` paths.
Publishing the package performs none of these steps.

## Verification and limits

- `tests/test_removed_workflows.py`: removed commands and flags fail parsing
  (exit code 2), `workflow`/`xtb`/`crest` are absent from the engine catalog,
  and a `workflow` config section is rejected.
- `tests/test_retired_workflow_ownership.py`: for marker- and metadata-owned
  jobs, submission creates no snapshot or queue row, the worker cannot prepare
  or rebind a generation, cancel and clear leave queue and artifacts
  byte-identical, and cleanup keeps terminal state. A retired pending row does
  not block the next standalone job, and unrelated retired state does not block
  standalone listing.
- `tests/core/test_retired_workflow_paths.py` covers marker detection,
  fd-pinned aliases and fail-closed inspection errors.
  `tests/test_core_without_workflows.py` checks that an installed `orca_auto/flow`
  package is never imported.
- `scripts/check_distributions.py` fails if the built package advertises a
  `workflows` extra or dependency, or if `orca_auto_workflows` is installed.
- At merge, #353 reports `make check` passing (2,735 passed, 7 skipped), the
  package matrix and the fake-ORCA smoke, and that the removal tests fail
  against the previous source.

Limits: no new licensed-engine calculation was run for the retirement (#353).
Historical workflow data is protected only; version 7 has no workflow commands
and no migration path for it. Old workflow stages must not be resubmitted as
ordinary ORCA jobs (`docs/RELEASE.md`).
