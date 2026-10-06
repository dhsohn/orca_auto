# ADR 0017: Queue Detail kind vocabulary and Unknown fallback

## Status

Accepted

## Context

The queue `Detail` column must name requested ORCA operations from admission
metadata only. Fix2 recorded a narrow `detail_kind` (`sp`, `irc`, `neb`, `other`)
only when the coarse `job_type` was `other`, and displayed unrecognized rows as
`ORCA`, which users read as an engine label rather than an unknown calculation.

## Decision

- Record a normalized `detail_kind` on every submission whose input has a route
  line, from the same single admitted read as today.
- Use a fixed vocabulary of lowercase tokens and `+` compounds with stable
  display labels (`Opt+Freq`, `TS+Freq`, `NEB-TS`, `TS+IRC`, `TS+Freq+IRC`,
  `NEB-TS+Freq+IRC`, …). Canonical compound token order is primary run type,
  then `freq`, then `irc`, then plain `neb`.
- Render Detail as: specific `task_kind` that names an operation (never generic
  `orca` / `orca_run_inp`), then recognized `detail_kind`, then coarse
  `job_type` when it names a known operation, else `Unknown`. Arbitrary type
  strings are never shown.
- Persist `unknown` (display `Unknown`) for unsupported or uncertain inputs,
  including ESD and legacy `other` values. Do not change `job_type` or scientific
  completion contracts.

## Consequences

- New submissions show compound operations without inferring from paths or names.
- Historical rows without `detail_kind` show `Unknown` unless `job_type` or
  `task_kind` still names the job.
- Tests in `tests/orca/test_queue_detail.py`, `tests/cli/test_activity_rendering.py`
  and `tests/orca/test_submission.py` lock the display contract.

## Limits

- `+` means requested combination, not proven stage order.
- `%freq` switches count only when the existing parser reliably proves a frequency
  request; otherwise the kind stays `unknown`.
