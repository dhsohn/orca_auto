# ADR 0007: One admission store per installation under `<runs_root>/.admission`

- Status: Accepted
- Date: 2026-09-27

## Problem

`scheduler.admission_root` could place `admission_slots.json` outside the
runs root; the example configuration offered it to "coordinate workers on one
host". Answering "where do the slots live" took three derivations with
different fallbacks: `load_config` used the configured key or
`<runs_root>/.admission`, `core/config/files.resolved_admission_root` did the
same from the shared config, and `RuntimeAdmissionMixin.resolved_admission_root`
fell back to the runs root itself, which only test-built configurations
reached. The limit came from `SchedulerConfig.admission_limit`, set only when a
`scheduler` section was written and always equal to
`max_active_simulations`, from the mixin's fallback, and from the worker's own
effective-concurrency path. `systemd install` required an explicitly
configured root to exist and added it to `ReadWritePaths`, and the restart
guard locked one root per worker unit. The 7.0 upgrade guide suggested a
separate `admission_root` to fence off retired admission records.

The coordination it served no longer exists: ORCA is the only engine and each
runs root has one worker behind its PID lock. On 2026-09-27 the production
configuration set no `admission_root`, and its slots lived in
`<runs_root>/.admission`.

## Decision

An installation has one admission store, `core.admission.admission_dir(runs_root)`,
which is `<runs_root>/.admission`, and its limit is
`scheduler.max_active_simulations`, which is also the worker's concurrency
(`OrcaRuntimeConfig.max_concurrent`):

- The configuration loader rejects `scheduler.admission_root` with one line:
  "scheduler.admission_root was removed: admission state always lives in
  <runs_root>/.admission. Delete the key." Every consumer that loads the
  configuration (`load_config`, `queue list`, `systemd install`) refuses it.
- `systemd install` renders `ReadWritePaths` with `runs_root` only; the worker
  creates `.admission` beneath it.
- `service restart` guards the one worker unit it restarts and locks exactly
  one admission store.

Removed: `SchedulerConfig.admission_root`, `configured` and `admission_limit`,
`RuntimeAdmissionMixin`, `schema.resolved_admission_limit`,
`files.resolved_admission_root`, `default_shared_admission_root` and
`resolve_configured_path`, the `admission_root` and `admission_limit` fields of
`OrcaRuntimeConfig` (which moves to `orca/config.py`), the queue worker's
`max_concurrent` argument and effective-concurrency path,
`run_context.configured_admission_root`, the worker `resolve_admission_root`
helper and its `WorkerConfig` protocol, `systemd_plan`'s explicit-directory
precondition and admission `ReadWritePaths` entry, and the restart guard's
root set.

## Verification and limits

- `tests/contracts/pins/admission_resolution.json`: the rows without the key
  are unchanged, and every row that sets `scheduler.admission_root` now pins
  the rejection for `load_config`, the worker, `engine_runtime_paths` and the
  systemd plan.
- `tests/contracts/golden/cli/systemd_units.txt` lists only the runs root in
  `ReadWritePaths`, and `tests/contracts/golden/cli/run_dir.json` logs
  `<runs_root>/.admission`, because the contract harness can no longer
  configure a separate root. The scenario goldens are byte-identical with the
  harness's admission store moved under its runs root.
- `tests/core/config/test_files.py`,
  `tests/orca/test_config_shared_loaders.py` and
  `tests/cli/systemd/test_cli_systemd.py` check the rejection;
  `tests/orca/queue/test_worker_start.py::test_worker_roots_and_limit_match_the_rendered_unit`
  checks that the worker's queue root, admission store and limit match the
  rendered unit.

Limits: a configuration that still sets the key does not load until it is
deleted. The old directory is not migrated; in the idle window that an upgrade
and `service restart` already require it holds no reserved or active slots.
Several workers can no longer share one admission store across runs roots.
Rolling back to 8.x needs no configuration change, because 8.x uses the same
`<runs_root>/.admission` when the key is absent (`docs/RELEASE.md`).
