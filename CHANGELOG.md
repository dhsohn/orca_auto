# Changelog

All notable changes to orca_auto are documented in this file.

This project follows a lightweight [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
style. Version numbers are recorded in `pyproject.toml`; release procedure lives
in [docs/RELEASE.md](docs/RELEASE.md).

## [Unreleased]

### Added

- JOSS-style open-source operating materials: contribution guidance, issue and
  pull request templates, release checklist, validation documentation, and a
  fake ORCA smoke example.
- Workflow HTML report: every workflow advance rewrites a self-contained
  `workflow_report.html` in the workflow workspace, showing the stage chain
  with statuses, the CREST → (xTB) → ORCA funnel counts, and a ranked ORCA
  results table (relative energies from `.engrad`, imaginary-frequency counts,
  links to per-job `job_report.html`). Covers both `conformer_screening`
  (conformer ranking) and `reaction_ts_search` (TS-candidate ranking).
- ScanTS OptTS fallback: when ORCA's TS-guess refinement aborts with a
  zero-distance geometry after the scan already bracketed a maximum (an ORCA
  6.x bug observed on TS6/TS8), the retry chain now runs one plain OptTS
  attempt directly from the highest surface point, bypassing the refinement.
  If that attempt also fails, the ordinary endpoint-completion/reverse-scan
  chain resumes from the crashed scan's artifacts.
- Relaxed-scan HTML report: plain relaxed scans (`Opt` route with a
  `%geom Scan` block) now get the scan-profile flavor of `job_report.html`
  (energy profile, interior-barrier prominence, scan-coordinate alignment)
  instead of the optimization-convergence flavor.
- HTML job reports: Opt, OptTS/NEB-TS, and ScanTS jobs now write a
  self-contained `job_report.html` next to `job_report.md` for successful and
  failed runs alike (shared renderer in `orca/report/`). ScanTS reports show
  the scan energy profile across all attempts (inline SVG); Opt/OptTS reports
  show the optimization convergence trace with an imaginary-frequency
  expectation check (0 for minima, 1 for TS). All flavors include the
  retry-recipe chain and a vibrational summary of the final frequency
  calculation — imaginary modes, dominant atom displacements, and (for ScanTS)
  their alignment with the scanned coordinate.
- ScanTS barrierless-profile detection: after the endpoint-completion scan, the
  assembled forward profile is checked for an interior maximum above
  0.5 kcal/mol; without one the run fails immediately with reason
  `scan_profile_no_barrier` instead of spending hours on a reverse scan that
  can only mirror the same monotonic profile.

### Changed

- An exhausted ScanTS retry-recipe chain now reports the actionable reason
  `scants_recipes_exhausted` (previously the generic `rewrite_failed`, which
  read like a rewriter bug). `rewrite_failed` is reserved for genuine rewrite
  crashes.

## [0.1.0] - Initial public development series

### Added

- Queue-first ORCA runtime with durable queue state, worker execution, retry
  state/report files, and organized output support.
- Internal workflow support for xTB and CREST stages.
- Linux/WSL-first CLI, configuration template, systemd user-service assets, and
  CI coverage for fake-engine integration paths.
