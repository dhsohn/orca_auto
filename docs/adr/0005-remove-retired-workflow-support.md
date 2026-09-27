# ADR 0005: Remove retired workflow support

- Status: Accepted
- Date: 2026-09-27
- Supersedes: [ADR 0003](0003-retire-workflows-for-standalone-orca-jobs.md)

## Problem

ADR 0003 removed workflows in 7.0.0 but kept a read-only boundary around their
data. `core/paths/retired.py` treated a directory as workflow-owned when it or
an ancestor under the runs root held `flow.yaml` or `workflow.json`, and
`queue_entry_is_retired_workflow_owned` also matched a queue row carrying
`workflow_id` metadata. Submission, `run-dir`, the worker's skip rule, claim,
preparation, recovery rebind, publication repair, orphan reconciliation,
terminal replay, cancellation, cleanup, snapshot intents and the production
scan each called one of these checks. `queue cancel` answered such a row with
reason `retired_workflow`. Admission slot rows were allowed to carry the
retired `workflow_id` field, and the packaging checks and
`scripts/prepare_runtime.py` refused a workflows distribution.

The boundary was about 47 references in `src` and served no live data. On
2026-09-27 none of the 47 rows in the production `queue.json` carried
`workflow_id` or `max_retries`, and neither of the 2 rows in
`admission_slots.json` carried `workflow_id`; 8.0.0 had stopped writing that
field (CHANGELOG 8.0.0). Three workflow marker files remained under the
production runs root (two `flow.yaml`, one `workflow.json`, from 2026-08-11
screens), and older smoke-test batches under `.orca_auto_smoke` held more.

## Decision

ORCA_auto has no notion of workflows. The marker detection, the queue-row
ownership check, every call site and the `retired_workflow` cancel branch are
removed:

- A directory holding `flow.yaml` or `workflow.json`, or lying under one, is an
  ordinary directory. `run-dir` accepts it when it has an ORCA input, the worker
  claims and runs its rows, `queue cancel` cancels them, cleanup and
  `queue list clear` treat its terminal state like any other, and
  `index rebuild`, listing and snapshot intents include it.
- A queue row's `workflow_id` metadata has no meaning.
- `admission_slots.json` rows follow the canonical slot schema only; a row that
  still carries `workflow_id` is rejected as corrupt.
- The worker and recovery rebind no longer check queue-row metadata for
  `max_retries`. Rows written before 4.0.0 carry a version-2 execution snapshot,
  which the version-3 snapshot check already refuses before any side effect.
  The snapshot-level `max_retries` checks and the historical generation freeze of
  [ADR 0002](0002-no-automatic-retry-of-failed-calculations.md) are unchanged.
- The packaging checks no longer look for a workflows extra, dependency,
  source tree or installed distribution, and runtime preparation no longer
  refuses one.

The 7.0.0 removal of the workflow extension, commands, configuration and
engines stays in effect: none of it exists, and a `workflow` configuration
section is rejected as an unknown key.

## Verification and limits

- The golden scenario `11_retired_workflow` and the CLI document
  `queue_cancel_retired` are deleted together with the removed behavior; every
  other golden under `tests/contracts` passes unchanged.
- `tests/test_retired_workflow_ownership.py`,
  `tests/core/test_retired_workflow_paths.py`, `tests/test_removed_workflows.py`
  and `tests/test_orca_worker_boundary.py` are deleted. The parser surface stays
  pinned by `tests/contracts/golden/cli/argparse_tree.json`.
  `tests/test_isolated_installation.py` keeps the isolated-installation worker
  run without the workflow package checks.
- `tests/core/test_admission_store.py::test_slot_row_with_a_field_outside_the_schema_fails_closed`
  checks that a slot row carrying `workflow_id` is rejected.
- `grep -rniE "flow\.yaml|workflow\.json|retired_workflow|workflow_id" src`
  finds nothing.

Limits: leftover workflow trees under the runs root now take part in scans.
On 2026-09-27 the production scan found 208 more `job_state.json` files, all in
`.orca_auto_smoke` smoke-test batches, 78 of them terminal ORCA states. After
upgrading, `index rebuild` records them and `queue list clear` removes their
`job_state.json`. The 2026-08-11 screen stage directories stay excluded because
they sit under an execution-generation-named directory. Upgrading directly from
7.0.x needs an empty `admission_slots.json` and no pending or running row that
belongs to old workflow work (`docs/RELEASE.md`).
