# Architecture

**English** | [한국어](ARCHITECTURE.ko.md)

ORCA_auto validates and durably queues standalone ORCA input directories. Supervised
workers claim eligible jobs, reserve shared admission slots, execute isolated
generations, and publish verified terminal observations.

## Ownership

| Component | Responsibility |
| --- | --- |
| `cli*.py`, `activity/` | User commands, queue views, cancellation and service inspection |
| `orca/` | ORCA input, execution, recovery policy, analysis and reports |
| `core/` | Durable queues, admission, process supervision, paths and storage |

Imports follow `orca` → `core`; domain code does not import CLI commands.
`core/engine_catalog.py` registers ORCA alone. Version 7 has no extension loader
or workflow implementation.

## Submission and execution

`orca/submission.py` prepares input resources and explicitly creates the execution
snapshot. Metadata assembly is pure. The submitter owns cleanup until durable
enqueue transfers ownership; an uncertain commit is resolved before cleanup.

The worker validates queue identity and the immutable input/executable binding
before launch. It tracks every live child, including a child whose row already
returned to pending. One unsettled terminal generation blocks its own directory;
unresolved directory identity or queued-publication failure can block the queue.
Capacity refusal before execution defers a job without recording a calculation failure.

Cancellation observations reuse unchanged queue snapshots. Terminal notification
dispatch has a durable claim and bounded background sends; notification delivery
is best effort and does not retain execution admission slots.

## State, evidence and reports

`orca/state.py` owns state mutation. `orca/state_reading.py` is a read-only consumer;
`orca/report/publication.py` publishes machine observations. They share the pure
`orca/report_fields.py` projection for ORCA-owned summary/results fields.

A report read verifies state, generation ownership, artifact paths and receipts.
It hashes each distinct file once per read, hashes the selected input after
ownership and other artifacts, then rechecks file identities before returning.
Receipt reuse is local to that read. Scientific evidence comes from validated
engine output; HTML and SI presentation are not evidence sources.

## Queue views and deployment

Durable JSON/state remains authoritative. `core/activity_index.py` supplies a
rebuildable SQLite projection for ordered/filterable queries; durable invalidation
tickets keep published state changes visible. `--refresh` discovers unindexed
runs. Initial builds and recovery still read source history.

Production can use a verified, immutable wheel runtime with external config and
state. Service status compares the installed unit with the actual process build.
See [RUNTIME](RUNTIME.md) for preparation and idle cutover.

Retired workflow directory markers are read only to reject execution and protect
historical files from standalone discovery/cleanup. Historical admission fields
remain readable so old reservations cannot disappear from capacity accounting.
