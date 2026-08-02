# orca_auto Roadmap

This roadmap describes the direction of orca_auto as research software. It is
not a date-based promise or a JOSS submission plan. Its purpose is to keep the
project's public contracts, maintenance priorities, and deliberate non-goals
visible while the implementation continues to evolve.

Release-specific changes belong in [CHANGELOG.md](CHANGELOG.md). Release
procedure belongs in [docs/RELEASE.md](docs/RELEASE.md), and validation
expectations belong in [docs/VALIDATION.md](docs/VALIDATION.md).

## Guiding Principles

- Keep submission queue-first: user commands enqueue durable work; supervised
  workers execute it.
- Treat ORCA inputs, queue entries, state files, reports, and documented CLI
  behavior as the main user-facing contracts.
- Prefer fail-closed recovery over broad automatic reruns that can waste compute
  or hide unsafe chemistry.
- Keep general xTB and CREST calculations as internal workflow-stage engines.
- Make public CI honest: fake-engine tests should prove project contracts, while
  real ORCA acceptance should be recorded separately when runtime semantics
  depend on licensed or site-specific binaries.
- Keep Linux and WSL as the supported runtime target; do not broaden path or
  process policy without a specific maintenance reason.

## Current Public Contracts

The canonical contract list lives in
[docs/PUBLIC_CONTRACTS.md](docs/PUBLIC_CONTRACTS.md). As of 1.0.0 every surface
it documents is committed: the 0.x two-tier split (Stable Core plus
Experimental) was resolved before the tag by promoting or removing every
Experimental surface. Changes to any documented surface are deliberate, tested,
and recorded in release notes; breaking a documented behavior requires a major
version.

## 0.1.x: Initial Public Surface (Released)

The 0.1 development series established the initial queue-first ORCA and workflow
surface. Its hardening priorities were:

- Stabilize the single runs-root model after organize removal.
- Keep workflow workspaces, standalone ORCA runs, and shared admission state
  separated clearly under the configured runs root.
- Harden queue cancellation, orphan reconciliation, worker restart, and terminal
  state transitions.
- Keep ScanTS and `scan_ts_search` behavior explicit: job-level retries only for
  recognized calculation failures, workflow-level extension/reverse exploration
  for search strategy.
- Improve report readability for failed jobs, partial workflow progress, and
  retry-exhausted outcomes.
- Add focused tests around low-coverage runtime glue only when it protects a
  public contract or a high-risk failure mode.
- Keep Korean documentation updated for user-facing behavior when English docs
  change substantially.

## 0.2.0: Durable Multi-Engine Runtime (Released)

The 0.2.0 release binds queue execution to immutable generation snapshots,
strengthens cancellation and recovery, and adds standalone xTB-MD as a
single-attempt first-class engine. It also adds provider-neutral Telegram and
Discord messaging, durable Discord archive submission, richer conformer
science outputs, and stricter resource and artifact provenance checks. The
shipped details and upgrade note live in [CHANGELOG.md](CHANGELOG.md).

The 0.3.0 release removes the standalone xTB-MD public engine, narrowing the
supported public surface back to standalone ORCA plus the internal workflow xTB
and CREST stages.

## 1.0: Stable Public Surface

The 1.0.0 cut closes the stability-readiness track. What it settles:

- One canonical contract for CLI flags, config keys, persisted state/report
  fields, and workflow layout, with the Experimental tier dissolved — every
  documented surface promoted or removed before the tag.
- The import-layer contract stays enforced by CI (four import-linter contracts
  in `pyproject.toml`); cross-layer engine wiring goes through the declared
  catalog, not ad-hoc imports.
- The three scaffold templates (`ts_search`, `conformer_search`, `scan_ts`) are
  the first-class workflow features; general xTB and CREST stay
  workflow-internal.
- Changes to ORCA invocation, restart/resume, resource handling, or output
  classification require real-engine acceptance evidence per
  [docs/VALIDATION.md](docs/VALIDATION.md).
- Release notes stay user-centered: motivation, behavior changes, cutover
  impact, and verification evidence.

## Maintenance Mode

After 1.0.0 the project is in maintenance: bug fixes, documentation accuracy,
and dependency upkeep. New capabilities need a deliberate scope decision first.
PyPI packaging, archived releases, and DOI registration remain optional
distribution choices to revisit on demand, not roadmap items.

## Deliberate Non-Goals

The following are outside the current project direction unless a future issue
changes the scope deliberately:

- Replacing ORCA, xTB, CREST, or chemical judgment.
- Becoming a general workflow platform or cluster scheduler.
- Supporting Windows-native execution, Windows drive paths, `/mnt/<drive>`
  executable paths, relative executable paths, or `.exe` binaries.
- Requiring licensed chemistry binaries in public CI.
- Publishing private structures, raw proprietary outputs, credentials, or
  site-specific scheduler policy as fixtures.
- Reintroducing broad automatic retry ladders that are not tied to explicit,
  tested calculation classes.
- Adding one-off local lab scripts to the public CLI without a reusable contract.

## How To Use This Roadmap

- For a bug fix, prefer a narrow issue and PR that names the affected public
  contract and verification command.
- For a feature, state which roadmap section it advances and which public
  contract it changes.
- For a risky runtime change, include fake-engine verification plus a real ORCA
  acceptance record when the behavior depends on real engine semantics.
- For a release, use this roadmap for prioritization, then record the actual
  shipped behavior in `CHANGELOG.md`.
