# Public contracts

**English** | [한국어](PUBLIC_CONTRACTS.ko.md)

ORCA_auto 7 supports standalone ORCA jobs on Linux/WSL with Python 3.11+ and
systemd supervision. Use absolute Linux paths and a separately installed ORCA
executable. Native Windows execution is unsupported.

## Public commands

| Command | Behavior |
| --- | --- |
| `init` | Create/update shared configuration; `--config` selects its location |
| `run-dir PATH` | Validate and durably enqueue an ORCA input directory; returns before execution |
| `queue list` | List ORCA jobs and global active count; `--json` is the automation surface |
| `queue list clear` | Clear terminal queue/run listings while preserving calculation artifacts |
| `queue cancel TARGET` | Cancel a queue id, run id or unambiguous known path alias |
| `index prune` | Preview missing-path index rows; only `--apply` removes them |
| `systemd install` | Render/install matching runtime units |
| `service status` | Inspect units and actual worker freshness; stale/undetermined returns nonzero |
| `service restart` | Hold admission locks and refuse active or unresolved reservations unless forced |

Queue list accepts `--engine orca`, `--kind job`, repeated status filters,
non-negative `--limit` and explicit `--refresh`. Global active counts and admission
blockers remain visible when filters or limits hide individual rows. Clear does
not accept list filters. Ordinary discovery uses queue/index locations; refresh
also scans unindexed standalone runs without registering them.

`run-dir` chooses the latest eligible `.inp`, with deterministic name ordering
for ties. It binds inputs, dependencies, executable identity and resource values
to a new generation. An active directory cannot be submitted twice. `--force`
permits a new execution where an existing successful result would otherwise be
reused. ORCA `%pal`/`%maxcore` determine resources; configuration fills missing
directives. There are no command-line resource overrides.

## Configuration

Discovery uses an explicit CLI path, `ORCA_AUTO_CONFIG`, the checkout's
`config/orca_auto.yaml`, then `~/orca_auto/config/orca_auto.yaml`. A fresh wheel
installation creates its default config outside its environment, under the last
path. Configuration is validated before defaults: malformed mappings, explicit
null values, duplicate/unknown keys and removed fields fail closed.

Supported top-level keys are `runs_root`, `scheduler`, `resources`, `orca`, and
`messenger`. See the [complete example](../config/orca_auto.yaml.example).
`scheduler.admission_root` defaults to `<runs_root>/.admission`; its shared slots
limit active simulations. Discord is outbound only; blank token/channel strings
disable delivery. Notifications are best effort.

## Durable execution and recovery

Submission saves a snapshot and durable queue acceptance before returning success.
An uncertain enqueue outcome is reconciled before its snapshot can be removed.
Each execution has an isolated generation beneath its public job directory.
Nested generations and retired workflow-owned directories cannot be submitted.

Workers validate bound identities and preserve unresolved ownership. Calculation
failures are recorded without automatic calculation retries. A launch denied by
scratch capacity before execution remains pending and can be admitted later.
Cancellation/finalization must release verified ownership before the same row or
directory can run again. Corrupt queue/index/admission state is reported rather
than treated as empty.

Queue/state writers invalidate a rebuildable activity index. A warm bounded query
does not reread all historical state. Initial indexing, explicit refresh and
recovery still inspect source history; external manual file edits require refresh.

## Machine observations and reports

Terminal `machine.json` uses the immutable common contract
`{"name":"factory/machine-observation","version":1}`, with operation kind
`chemistry/orca-run` and domain payload `chemistry/results-bundle` v1.
Lifecycle, delivery and handoff are separate decisions. Artifacts carry content
receipts; process exit or a notification alone does not prove scientific success.

The reader verifies state, generation ownership, file binding and ORCA-owned
summary/results fields. Missing or inconsistent owned fields are rejected;
additional domain fields remain allowed. Each distinct artifact is hashed once
per read and its identity is rechecked before acceptance. Do not edit generated
reports or interpret private `job_state.json` as a public handoff.

Human HTML and SI reports depend on the calculation type and available verified
evidence. Missing charge/multiplicity or frequency evidence is reported as
unavailable, not inferred as a neutral singlet or a successful stationary point.
Users remain responsible for chemical input design and scientific acceptance.

## Version 7 removal

Workflows, conformer scaffolds, xTB/CREST execution, workflow configuration,
grouped workflow activity and the optional `orca_auto_workflows` distribution
are removed. There is no execution alias or state migration. Historical files
are retained, and retired admission/state identifiers remain readable only for
ownership/accounting safety. Use the [upgrade procedure](RELEASE.md#upgrading-to-70).

Public behavior changes use semantic versioning. Consumers should ignore unknown
additive JSON fields. Python internals, state files, worker subprocess plumbing,
and terminal formatting are not stable integration APIs.
