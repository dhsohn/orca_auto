# Support policy

For help with ORCA_auto, open a GitHub issue with a small example that reproduces
the problem. Support focuses on the queue, worker, reports and documentation;
response times depend on maintainer availability.

## Where to ask

Use GitHub Issues for:

- reproducible CLI, queue, worker, report, parser, or documentation bugs;
- calculation-failure triage when ORCA_auto classified, resumed, or reported a
  job incorrectly;
- feature requests that improve reusable ORCA-centered workflows;
- documentation gaps or stale examples.

Use the issue templates when possible.

## What to include

For runtime or calculation issues, include sanitized versions of the relevant artifacts when available:

- command run and exact output;
- OS/runtime context such as Linux distribution or WSL version;
- Python version and ORCA_auto commit or release;
- ORCA version when the issue involves real ORCA;
- selected `.inp` snippet, output tail, and terminal marker;
- terminal generation `machine.json`, plus sanitized `job_state.json` or queue snippets for recovery-state diagnosis;
- attempt number, if applicable.

Remove bot tokens, channel IDs, private paths, proprietary structures, and private research data before posting.

## Scope boundaries

Out of scope for repository support:

- ORCA licensing or upstream ORCA bugs;
- site-specific scheduler, MPI, filesystem, or module-system policy;
- interpreting whether a computed result is chemically meaningful;
- debugging private calculations that cannot be reduced to sanitized artifacts;
- operational support with a guaranteed response time.

Note whether an issue requires a real ORCA installation or reproduces with the fake engine; see [docs/VALIDATION.md](docs/VALIDATION.md).

## Supported versions

Support focuses on `main` and the latest tagged GitHub release; fixes are normally made on `main` first, except for release-specific issues.
