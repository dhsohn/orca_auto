# ADR 0011: Read-only PID lookups

- Status: Accepted
- Date: 2026-09-28

## Problem

`core/utils/process.read_live_pid_file` read an identity, then unlinked the
path when the owner was not proven live. During worker restart, a reader can
capture the old identity before `write_worker_pid_file` atomically replaces
the path, and then delete the new worker's file. Submission and worker checks
read this file without the worker's lifetime lock.

## Decision

PID lookups only return a proven live PID or `None`; they never remove files.
The existing worker lifetime lock protects publication at startup and removal
at shutdown (`OrcaQueueWorker.run` and `run_once`, with `_before_run` and
`_after_run`). Startup overwrites any stale file. Remove the reader's
unlink exception; add no new lock, file format or cleanup subsystem.

## Verification and limits

`tests/core/queue/test_worker.py::test_pid_lookup_preserves_worker_replacement`
schedules the production atomic writer immediately after a reader captures an
old or malformed payload. Both cases delete the new file before the fix; after
the fix its bytes and live PID remain readable. Process identity tests and
the process-owner truth table retain the same liveness decisions, with no
reader-side deletion. Worker startup tests cover singleton exclusion and PID
cleanup after startup failure.

A concurrent lookup can still return `None` for the old identity; it is a
snapshot, not a synchronization guarantee. A crashed worker's stale file can
remain until the next startup, but cannot be reported as live without verified
boot ID and process start ticks. Duplicate-worker prevention remains the
lifetime lock's responsibility.
