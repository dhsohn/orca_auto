# Support policy

orca_auto support is best-effort and centered on reproducible project issues.
The project is maintained as research software, not as a commercial support
service or a substitute for local computational-chemistry judgment.

## Where to ask

Use GitHub Issues for:

- reproducible CLI, queue, worker, report, parser, retry, or documentation bugs;
- calculation-failure triage when orca_auto classified, retried, resumed, or
  reported a job incorrectly;
- feature requests that improve reusable ORCA-centered workflows;
- documentation gaps or stale examples.

Use the issue templates when possible. They ask for the information needed to
reproduce or triage the problem without exposing private data.

## What to include

For runtime or calculation issues, include sanitized versions of the relevant
artifacts when available:

- command run and exact output;
- OS/runtime context such as Linux distribution or WSL version;
- Python version and orca_auto commit or release;
- ORCA/xTB/CREST versions if the issue depends on real engines;
- selected `.inp` snippet, output tail, and terminal marker;
- terminal generation `machine.json`, plus relevant sanitized `job_state.json`
  or queue snippets for private recovery-state diagnosis;
- retry attempt number and generated retry input name, if applicable.

Remove messenger bot tokens, channel IDs, private paths, proprietary structures, and
private research data before posting.

## Scope boundaries

The project can help with orca_auto behavior and documentation. It generally
cannot provide support for:

- ORCA licensing or upstream ORCA bugs;
- site-specific scheduler, MPI, filesystem, or module-system policy;
- interpreting whether a computed result is chemically meaningful;
- debugging private calculations that cannot be reduced to sanitized artifacts;
- emergency production support or guaranteed response times.

If a problem depends on a real ORCA installation, distinguish that from a public
CI/fake-engine failure. The validation guide explains this split:
[docs/VALIDATION.md](docs/VALIDATION.md).

## Supported versions

Unless a release note says otherwise, support focuses on the current `main`
branch and the latest tagged GitHub release. Older versions may receive guidance
when the issue is easy to diagnose, but fixes are normally made on `main` first.
