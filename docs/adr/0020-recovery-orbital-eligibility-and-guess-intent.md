# ADR 0020: Recovery orbital eligibility and submitted guess intent

- Status: Extended by ADR 0021
- Date: 2026-11-14
- Extends: [ADR 0009](0009-resume-only-by-rebind.md)

## Problem

The former `input_references.checkpoint_file_looks_intact` accepted any nonempty
checkpoint with a nonzero byte in its first sixteen bytes, including one byte
and arbitrary text. `_build._plan_bound_dependencies` skipped automatic seeding
only for submitted MORead/MOInp. Consequently a recovery could inject MORead
despite an explicit PModel or AutoStart false. Neither behavior established
that the interrupted generation had produced orbitals or permitted replacing
the submitted initial state.

The supplied incident evidence describes interruption before GUESS completed,
later selection of that generation's checkpoint, and a materialized dependency
mode of 0400. Artifact sizes and nonzero prefixes establish neither GBW structure
nor readiness. Content/readiness and permissions remain separate hypotheses;
this decision makes no permission-only repair.

## Decision

`recovery_orbitals` owns a conservative application policy and a pure output
eligibility reader. `execution_binding._recovery` confines and captures the
immediate predecessor's output and `<stem>.gbw`. `_build` materializes exactly
the captured checkpoint payload; it never reopens the candidate to copy bytes
different from those evaluated.

| Submitted intent | Optional recovery-orbital behavior |
| --- | --- |
| MORead or MOInp, including AutoStart false | Preserve the submitted checkpoint chain |
| PModel, PAtom, Hueckel, HCore, or any explicit `%scf Guess` | Preserve the submitted guess |
| NoAutoStart or `%scf AutoStart false` without explicit reuse | Skip automatic orbital seeding |
| Rotation, mixing, occupations, roots/state selection, unfamiliar or ambiguous SCF policy | Skip automatic orbital seeding |
| No explicit guess/state policy | Consider only the immediate predecessor and require affirmative readiness evidence |

The syntax reader respects capitalization, spacing, equals signs and active
comment semantics. Only the understood scalar numerical SCF controls listed
in `recovery_orbitals._NUMERICAL_SCF_KEYS`, and unambiguous AutoStart true, are
transparent to the policy. Unknown SCF controls conservatively suppress reuse.
Explicit reuse is checked first and remains unchanged even with other controls.

This opt-out is an application recovery policy. ORCA's AutoStart concerns
same-basename reuse and behaves differently for geometry optimizations; it
does not forbid explicit MORead. See the official
[ORCA 6.1 initial-guess manual](https://www.faccts.de/docs/orca/6.1/manual/contents/essentialelements/initialguess.html).

The gate recognizes an ORCA startup banner and an exact `Finished Guess after
<seconds> sec` line after the latest recognized startup, geometry-cycle or
guess boundary. Authentic 6.1.1 H2, water and ammonia fixtures characterize this
milestone, including optimization cycles which omit the full GUESS heading.
Input echoes and comments do not count. Missing output, an unterminated final
line, NUL bytes, undecodable text, unrecognized output, or a new boundary after
the milestone suppress reuse. A SCF heading or an `INITIAL GUESS DONE` fragment
alone is insufficient. No uncharacterized SCF milestone is substituted.

Unsafe paths, symlinks, nonregular files and multiple links still fail closed.
Stable bounded capture, input and aggregate budgets, basename collision checks,
and exact immediate-predecessor ownership remain. The evidence capture is also
charged against the selection's aggregate budget. Missing, changing or oversized
optional evidence/checkpoints skip the seed and preserve geometry recovery.
A zero-filled prefix is only a negative filter. There is no minimum-size
threshold, guessed binary layout, ancestor search or fallback to `.moinp.gbw`.

New recovery metadata contains `checkpoint_decision` with policy, status and
reason, and, when captured, the output identity and one-based boundary/evidence
line numbers. It describes the decision at binding; later claim verification
still verifies the immutable materialized bytes, without rereading old output.
Historical snapshots and already-bound inputs keep their behavior and format.

Fresh generations, one attempt per generation, the three-rebind limit,
cancellation fences, completed-output adoption, and scientific files in earlier
generations remain governed by ADR 0009. Dependency mode remains 0400.

## Verification and limits

- `tests/orca/test_recovery_orbitals.py` covers policy and actual bound inputs,
  authentic output milestones, later-phase invalidation, unsafe files, budgets,
  capture changes, same-payload materialization and no ancestor search. Its
  binding-stage tests intentionally avoid Linux queue/xattr dependencies.
- `tests/orca/test_recovery_rebind.py` retains the full recovery invariants;
  synthetic positive checkpoint tests now supply explicit GUESS evidence.
  Their placeholder GBW payloads are not interoperability evidence.
- `tools/checkpoint_acceptance.py` and its adjacent runbook implement a bounded
  baseline/candidate factorial matrix and actual worker interruption/rebind
  acceptance. `tests/tooling/test_checkpoint_acceptance.py` tests its preparation,
  observation and refusal rules without launching engines.
- Full Linux `make check`, `make check-packages`, release fake-engine smoke and
  the owner-executed real-engine acceptance remain release gates. Authoring the
  harness is not evidence that a matrix cell or recovery calculation passed.

The gate does not prove GBW completeness, chemical compatibility, or freedom
from later torn writes. Output and GBW writes are not an atomic transaction;
even a short nonzero payload accompanied by qualifying output can pass the
eligibility gate. Only real ORCA consumption establishes interoperability.
Unknown formats can reduce reuse. A writable dependency that reaches SCF but
then fails scratch publication is not a successful permission repair.
