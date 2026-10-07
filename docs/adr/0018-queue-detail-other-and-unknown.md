# ADR 0018: Distinguish Other operation evidence from Unknown Queue Detail

- Status: Accepted
- Date: 2026-10-07
- Supersedes: [ADR 0017's unsupported-input fallback](0017-queue-detail-kind-vocabulary.md)

## Problem

[ADR 0017](0017-queue-detail-kind-vocabulary.md) recorded `unknown` both for a
definite operation outside the specific Detail vocabulary and for insufficient
operation evidence. `queue_detail.py` therefore displayed an active `EnGrad` or
complete ESD request exactly like a missing value or unclosed operation block.
The legacy token `other` also carried no distinction between those cases.

## Decision

Keep the classifier and its fixed labels in `orca/queue_detail.py`. Introduce
`unsupported`, displayed as `Other`, only when the admitted lines contain positive
operation evidence: a known active unquoted unsupported route keyword or complete
ESD mode, a closed operation block, a closed recognized operation value or true
frequency switch, a complete compound file directive, or route-bearing segments
around `$new_job`. Unknown, missing, quoted, duplicate or unclosed operation values
remain `unknown`, displayed as `Unknown`. This evaluates the operation evidence
the classifier uses; it is not a validator for the entire ORCA input.
An additional positive marker does not resolve already-uncertain operation
evidence, including orphan or wrong-block operation markers. Compound references
need an unescaped closing delimiter under the existing tokenizer's backslash rules.

Legacy `other` continues to display `Unknown`. Existing `unknown` rows remain
`Unknown`; listing does not reread their input, backfill them or rewrite metadata.
Only `detail_kind=unsupported` carries the new positive-evidence meaning: coarse
`job_type` or `task_kind` strings named `unsupported` are not display evidence.
The existing task-kind/detail-kind/job-type precedence and suffixes remain intact.

Ignore quoted and commented route names in a presentation-only copy of the
admitted lines. A route with no active unquoted tokens remains `unknown`, while
an input without a route retains the existing absence of a Detail kind. Preserve
all existing supported operation labels and compounds.

Keep known NEB names instead of classifying them as Other. The
[ORCA 6.1 NEB keyword summary](https://www.faccts.de/docs/orca/6.1/manual/contents/structurereactivity/neb.html#summary-of-keywords)
names NEB/NEB-CI, their ZOOM aliases, NEB-TS with FAST/LOOSE/TIGHT/ZOOM aliases,
and exact NEB-IDPP. Exact NEB-MMFTS and the existing FLAT-NEB-TS alias are also
named in the
[ORCA 6.0 structure keyword table](https://www.faccts.de/docs/orca/6.0/manual/contents/structure.html#nudged-elastic-band-methods).
NEB/NEB-CI variants retain `NEB`, TS variants retain `NEB-TS`, and standalone
`neb-idpp` and `neb-mmfts` display `NEB-IDPP` and `NEB-MMFTS`. Do not invent
prefix/suffix combinations: unknown aliases and conflicting named NEB requests
remain `unknown`. NEB-IDPP names non-quantum path initialization only; it creates
no quantum-energy or scientific-completion guarantee.
For a named NEB request, route frequency keywords and true block frequency
switches describe the same unsupported combination and remain `unknown`; false
block switches and quoted or commented frequency names preserve the standalone label.

This partly replaces ADR 0017's combined unsupported/uncertain fallback and
extends its fixed display vocabulary. Admission still reads the input once.
Coarse job types, execution binding, completion analysis, reports, machine
scientific state, method and basis classification are unchanged.

## Verification and limits

`tests/orca/test_queue_detail.py` fixes literal expected kinds for definite and
uncertain operations, known NEB aliases, quoted names, missing route tokens,
conflicting named NEB requests, uncertainty precedence with additional positive
markers, escaped compound delimiters and preserved supported compounds.
`tests/cli/test_activity_rendering.py` fixes labels, legacy compatibility,
precedence and publication/resource suffixes. `tests/orca/test_submission.py`
asserts one admitted source read, unchanged input bytes and coarse job type,
metadata roundtrips and the resulting Detail cell.

The Windows writer used the bundled Python standard library to execute focused
test bodies: the classifier imported directly, and the presentation module's
exact AST omitted only its POSIX queue dependency import. This is synthetic
evidence, not pytest, durable-submission execution or runtime acceptance.
Standalone Ruff, document parity and `git diff --check` provide lightweight
checks. The full Linux `make check` gate remains required by
[VALIDATION](../VALIDATION.md) and was not run in this restricted Windows
environment. No ORCA calculation or operational queue was used; runtime and
scientific output parsing did not change.
