# Public Contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

This document names the orca_auto surfaces that users, operators, and future
contributors may reasonably depend on. It is intentionally narrower than the
full implementation: internal modules, private helper functions, and runtime
plumbing may change when the documented behavior stays intact.

orca_auto is still in the 0.x series. Breaking changes can happen, but changes
to the contracts below should be deliberate, tested, documented, and called out
in [CHANGELOG.md](../CHANGELOG.md).

## Compatibility Level

- Existing documented commands, config keys, artifact names, and status strings
  should not be renamed or removed casually.
- Additive JSON fields are allowed. Consumers should ignore unknown fields.
- Human-oriented Markdown, HTML, and terminal table formatting may change
  without a migration path; use `--json` or JSON artifacts for scripts.
- Internal worker entrypoints and Python helper modules are not a stable public
  API unless this document or [docs/REFERENCE.md](REFERENCE.md) says otherwise.
- Real ORCA behavior that cannot be proven in public CI should be recorded as
  manual acceptance evidence, following [docs/VALIDATION.md](VALIDATION.md).

## Runtime Contract

Supported runtime assumptions:

- Python 3.11 or newer.
- Native Linux or WSL2.
- Linux/POSIX paths for configured roots and executables.
- ORCA, xTB, and CREST executables, when configured, must be absolute Linux
  executable paths.

Unsupported path and process assumptions:

- Windows drive paths such as `C:\...` or `C:/...`.
- `/mnt/<drive>/...` executable paths.
- Relative executable paths in config.
- `.exe` engine binaries.
- Public CI that requires licensed chemistry binaries.

## Public CLI Contract

The public user/operator CLI is `orca_auto ...`.

Supported commands:

- `orca_auto init`
- `orca_auto run-dir <path>`
- `orca_auto scaffold ts_search <path>`
- `orca_auto scaffold conformer_search <path>`
- `orca_auto scaffold scan_ts <path>`
- `orca_auto queue list`
- `orca_auto queue list clear`
- `orca_auto queue cancel <target>`
- `orca_auto service status`
- `orca_auto service restart`
- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto scan-notify`

Stable behavior:

- `run-dir` is queue-first. New work is enqueued durably and executed later by a
  supervised worker.
- A successful new submission returns `status: queued`.
- Closing the submitting terminal after a successful queue submission is safe.
- `queue cancel` accepts the visible activity id plus known aliases such as
  workflow id, queue id, run id, or path aliases.
- `queue list --json`, `queue cancel --json`, and `service status --json` are
  the script-friendly surfaces.
- `queue list --watch` is human-oriented and does not support `--json`.

Non-contract CLI surfaces:

- `orca_auto queue worker` and `python -m ...worker_child` are runtime plumbing.
  Users should normally manage long-running workers through `systemd`.
- Hidden `systemd install` flags exist for tests and maintenance; they are not
  the supported operator interface unless documented in the reference.

## Config Contract

Config discovery order:

1. `ORCA_AUTO_CONFIG`
2. `<project_root>/config/orca_auto.yaml`
3. `~/orca_auto/config/orca_auto.yaml`

Supported top-level sections and keys:

- `resources.max_cores_per_task`
- `resources.max_memory_gb_per_task`
- `scheduler.max_active_simulations`
- `scheduler.admission_root`
- `workflow.root`
- `workflow.paths.xtb_executable`
- `workflow.paths.crest_executable`
- `telegram.bot_token`
- `telegram.chat_id`
- `telegram.timeout_seconds`
- `telegram.max_attempts`
- `telegram.retry_backoff_seconds`
- `orca.runtime.allowed_root`
- `orca.runtime.default_max_retries`
- `orca.paths.orca_executable`

Stable behavior:

- `orca.runtime.allowed_root` is the single runs root by default.
- Workflow workspaces live under the runs root unless `workflow.root` is set.
- The shared admission directory defaults to `<runs root>/.admission` unless
  `scheduler.admission_root` is set.
- `scheduler.max_active_simulations` caps active ORCA, internal xTB, and
  internal CREST jobs together.
- `orca.runtime.default_max_retries: 0` disables ORCA retries.
- A positive `default_max_retries` enables calculation-type retry policy, still
  capped by ORCA route type.
- Telegram is enabled only when both `telegram.bot_token` and `telegram.chat_id`
  are non-empty.

## Queue And Activity Contract

The durable per-engine queue file is named `queue.json`. It is an implementation
file, but the queue lifecycle and visible activity fields are public behavior.

Queue entry fields that downstream code may rely on:

- `queue_id`
- `app_name`
- `task_id`
- `task_kind`
- `engine`
- `status`
- `priority`
- `enqueued_at`
- `started_at`
- `finished_at`
- `cancel_requested`
- `error`
- `metadata`

Queue statuses:

- `pending`
- `running`
- `completed`
- `failed`
- `cancelled`

`orca_auto queue list --json` returns:

- `count`
- `active_simulations`
- `activities`
- `sources`

Each activity item contains:

- `activity_id`
- `kind` (`job` or `workflow`)
- `engine` (`orca`, `xtb`, `crest`, or `workflow`)
- `status`
- `label`
- `source`
- `submitted_at`
- `updated_at`
- `cancel_target`
- `aliases`
- `metadata`

The `metadata` mapping is intentionally extensible. Scripts may use known keys
such as `queue_id`, `task_id`, `task_kind`, `run_id`, `workflow_id`,
`reaction_dir`, `job_dir`, `allowed_root`, `priority`, `template_name`, and
`workspace_dir`, but should tolerate missing or additional keys.

## ORCA Job Artifact Contract

Completed, failed, cancelled, and skipped ORCA jobs write artifacts next to the
job directory:

- `job_state.json`
- `job_report.json`
- `job_report.md`
- `job_report.html` when a report renderer applies

`job_state.json` and `job_report.json` use the normalized engine artifact shape:

- `schema_version`
- `engine`
- `job`
- `status`
- `input`
- `resources`
- `timestamps`
- `recovery`
- `process`
- `artifacts`
- `engine_payload`

Stable top-level expectations:

- `schema_version` is `1` for the current normalized artifact schema.
- `engine` is `orca` for ORCA job artifacts.
- `job.id` identifies the run when available.
- `job.dir` points at the job directory.
- `status.state` is the job state.
- `status.reason` is the final or current reason when available.
- `input.primary_path` is the selected ORCA input path.
- `timestamps.started_at`, `timestamps.updated_at`, and
  `timestamps.finished_at` are UTC-style ISO text when available.
- `artifacts.last_out_path` points at the last ORCA output path when known.
- `engine_payload.run_id`, `engine_payload.max_retries`,
  `engine_payload.attempts`, and `engine_payload.final_result` carry the
  ORCA-specific run details.

`engine_payload.final_result`, when present, contains:

- `status`
- `analyzer_status`
- `reason`
- `completed_at`
- `last_out_path`
- optional `resumed`
- optional `skipped_execution`
- optional `runner_error`

ORCA run statuses:

- `created`
- `running`
- `retrying`
- `completed`
- `failed`

ORCA analyzer statuses:

- `completed`
- `error_scf`
- `error_scfgrad_abort`
- `error_multiplicity_impossible`
- `error_disk_io`
- `error_memory`
- `error_geometry`
- `geom_not_converged`
- `ts_not_found`
- `incomplete`
- `unknown_failure`

Reason strings are part of issue triage and report interpretation once they are
documented or tested. Important current examples include `normal_termination`,
`existing_out_completed`, `retry_limit_reached`, `interrupted_by_user`,
`worker_shutdown`, `crashed_recovery`, `runner_exception`, `cancel_requested`,
`rewrite_failed`, and `scants_recipes_exhausted`.

## Workflow Contract

Workflow input manifests are named `flow.yaml`.

Supported workflow templates:

- `reaction_ts_search`, scaffolded by `orca_auto scaffold ts_search`
- `conformer_screening`, scaffolded by `orca_auto scaffold conformer_search`
- `scan_ts_search`, scaffolded by `orca_auto scaffold scan_ts`

Manifest keys that users may rely on:

- `workflow_type`
- `crest_mode`
- `priority`
- `resources.max_cores`
- `resources.max_memory_gb`
- `orca.route_line`
- `orca.charge`
- `orca.multiplicity`
- `crest`
- `endpoint_pairing`
- `max_crest_candidates`
- `max_orca_stages`
- `scan_coordinate`
- `barrier_threshold_kcal`
- `max_scan_extensions`
- `orca_optts_route_line`
- `allow_external_inputs`

Workflow runtime artifacts:

- `workflow.json` is the durable workflow payload.
- `workflow_report.html` is rewritten on workflow advances as a human-facing
  summary.
- `workflow_registry.json` and `workflow_registry.journal.jsonl` support
  cross-workflow listing and event history.
- Internal engine queues and outputs live under workflow stage directories such
  as `<runs root>/<workflow_id>/01_crest`, `02_xtb`, and `03_orca`.

Workflow and stage statuses use the shared status vocabulary where applicable:

- `created`
- `planned`
- `pending`
- `queued`
- `submitted`
- `running`
- `retrying`
- `waiting_for_slot`
- `cancel_requested`
- `cancelled`
- `completed`
- `failed`
- `cancel_failed`
- `submission_failed`
- `unknown`

Workflow-specific reason strings currently used in public reports or triage
include `scan_profile_no_barrier`, `ts_candidates_exhausted`,
`reaction_ts_search_xtb_phase_failed`, `conformers_failed`, and
`xtb_ts_guess_missing`.

## Systemd Contract

Supported unit filenames:

- `systemd/orca_auto-runtime@.target`
- `systemd/orca_auto-queue-worker@.service`
- `systemd/orca_auto-bot@.service`

Supported operator commands:

- `orca_auto systemd install --user <name> --repo <path>`
- `orca_auto service status`
- `orca_auto service restart`

Stable behavior:

- The installer enables the queue worker.
- The Telegram bot is enabled only when Telegram config is complete.
- `service status` reports the runtime target, queue worker, and bot status.
- `service restart` restarts the runtime target when enabled; otherwise it
  restarts the queue worker.

## Non-Contracts

These are intentionally outside the stable public surface:

- Private Python functions and modules, including helper modules under
  `src/orca_auto`.
- Internal worker child command lines.
- Exact terminal table widths, colors, icons, and wrapping.
- Exact HTML or Markdown report layout.
- Cache directories and local tool artifacts.
- Raw ORCA, xTB, or CREST output formatting.
- Any private scheduler, MPI, module-system, or workstation policy.

When in doubt, document a desired behavior here before writing external scripts
that depend on it.
