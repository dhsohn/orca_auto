# Architecture decision records

An architecture decision record (ADR) keeps the reason for one decision that
changes how the project is built or what it promises. `docs/ARCHITECTURE.md`
describes the system as it is; the ADRs record why it became that way, what was
replaced, and how the decision was checked.

## When to write one

Write an ADR in the same pull request as a change that does one of the following,
or before the first pull request when the change spans several:

- changes a public contract or requires a major version,
- removes a feature, file format or supported path,
- moves the ownership of state, persistence or a mutation rule, or
- makes the project depend on external behavior it must keep, such as an engine,
  a file format or a platform.

Bug fixes, refactoring inside one owner and documentation changes do not need one.

## Rules

- One decision per ADR. Files are named `NNNN-short-title.md` with a four-digit
  sequence number and a lowercase, hyphenated title.
- ADRs are written in English.
- Facts come from the repository: pull requests, commits, the changelog, tests or
  documents, cited where they are used. When the record does not show why
  something was decided, the ADR says so instead of supplying a reason.
- Every ADR names the checks that show the decision holds and the limits it
  accepts.
- An accepted ADR is not rewritten. A later decision that replaces or extends it
  gets its own ADR, and only the earlier ADR's `Status` line changes to point to
  it.
- Every ADR is added to the lists in `docs/ARCHITECTURE.md` and its Korean
  edition `docs/ARCHITECTURE.ko.md`.
- ADRs written before this document keep their original header lines and
  sections.

## Status values

| Status | Meaning |
| --- | --- |
| `Proposed` | Under discussion; not yet in effect |
| `Accepted` | In effect |
| `Completed` | A migration whose completion criteria are met |
| `Superseded by ADR NNNN` | Replaced by a later decision |
| `Partly superseded by ADR NNNN` | A later decision replaces part of it; the rest is in effect |
| `Extended by ADR NNNN` | Still in effect, with a later decision building on it; several are listed as `ADR NNNN, ADR MMMM` |

## Decisions recorded after the fact

A decision made before it had an ADR can be recorded later. Its `Date` is the
date of the release that shipped the decision, as the changelog gives it, or the
merge date when no release has shipped it yet. A `Recorded` line gives the day
the ADR was written. The problem and the decision come from the history up to
that release: the changelog, pull requests, commit messages, code and tests.
Verification describes the checks as they stand on the recording date, and says
when a figure comes from an earlier revision.

## Template

```markdown
# ADR NNNN: Title

- Status: Accepted
- Date: YYYY-MM-DD
- Supersedes: [ADR NNNN](NNNN-short-title.md)
- Extends: [ADR NNNN](NNNN-short-title.md)
- Recorded: YYYY-MM-DD

## Problem

What was wrong or missing, with evidence: observed failures, the contract at
stake, and what the code did before the change.

## Decision

What changes, who owns what afterward, and what is removed. Alternatives that
were rejected, with the reason, when the record shows one.

## Verification and limits

The tests, gates or real runs that show the decision holds, and what the
decision deliberately does not cover.
```

`Supersedes`, `Extends` and `Recorded` are included only when they apply.
