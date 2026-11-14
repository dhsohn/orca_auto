# ADR 0021: Private engine-time MOInp copies

- Status: Accepted
- Date: 2026-11-14
- Extends: [ADR 0020](0020-recovery-orbital-eligibility-and-guess-intent.md)

## Problem

The owner-supplied ORCA 6.1.1 baseline experiment used identical diagnostic
input and donor bytes in each permission pair. V400 and P400 failed an
`O_RDWR` open with EACCES. V600 consumed MORead, completed GUESS and entered
SCF; P600 opened the file but failed before completed GUESS. All four cells
had valid machine/artifact receipts and neither timeout fired. MaxIter 1 was
intentional: all four were terminal failures, with no completed-SP evidence.
The original evidence is retained separately from candidate acceptance.

This establishes a writable-access requirement for the tested engine path,
and a separate content/readiness or compatibility failure for the pre-GUESS
donor. It does not decode GBWs or prove general chemical compatibility.
Changing bound dependency permissions would expose recorded input identities
to engine mutation; scratch publication also correctly rejects changed bound
bytes. Both explicit MOInp and injected automatic recovery use this path.

## Decision

Execution snapshot version 4 keeps the selected input and immutable dependency
files at 0400. Only MOInp reference tokens are rebound to the reserved basename
`orca_auto_moinp_<bound-dependency-basename>`. For automatic recovery the immutable
source remains `<stem>.moinp.gbw` and the engine reference is
`orca_auto_moinp_<stem>.moinp.gbw`. The prefix is reserved for engine copies, not
submitted input/dependency names. Other scientific directives, geometry and
state controls are unchanged; the eligibility policy in ADR 0020 still applies.

`orca/engine_moinp.py` owns this deterministic mapping and file creation.
`OrcaRunner` calls it immediately before `_run_in_place`. Bounded stable reads
of the immutable source and exclusive descriptor-relative creation produce a
new single-link 0600 file. Sources must still be 0400. Existing targets, symlinks,
hardlinks and paths outside a single basename fail closed. Originals are never
chmodded, renamed, hardlinked or used as the writable engine input.

With scratch, staging includes the immutable source under its original basename;
the capacity check also budgets the additional private copy before run state is
written. The runner creates the copy in the scratch workspace. Publication
verifies the durable and staged immutable sources independently (bytes and
modes, including missing staged files), then publishes the mutable engine copy
as an ordinary runtime artifact, subject to the existing transient-file filter. It does not compare that copy to the original
input digest. Without scratch, the runner creates the copy in the existing
flat generation and runs the same input basename there; output locations and
no-scratch behavior are otherwise unchanged. Both paths verify the immutable
snapshot after execution, including failure and interruption.

A private copy is execution evidence. It is not reused on recovery: the existing
one-attempt/fresh-generation boundary remains. Earlier generation outputs and
checkpoints are preserved. This convention needs no new mutable snapshot role
or public machine schema; materialized identities always describe the immutable
inputs. The bound input records the engine basename and thus its source mapping.

Version 3 snapshots cannot be executed or rebound by this version. Preserve all
historical artifacts; cancel pending older submissions and explicitly resubmit
reviewed source inputs in a fresh generation during the idle upgrade window.
Directory ownership/intent retirement still recognizes versions 2, 3 and 4.
Do not edit existing snapshots or silently upgrade their receipts. Release
versioning and the operational transition follow [RELEASE](../RELEASE.md).

## Verification and limits

`tests/orca/test_engine_moinp.py` uses real native subprocess `O_RDWR` writes for
explicit top-level/SCF MOInp and automatic seeds, with and without scratch. It
checks input identity, bytes, modes, inode separation, source/predecessor
preservation, capacity accounting, publication, failure/interruption and unsafe
paths. On Mac only the Linux reservation/probes and launch gate are substituted;
these tests do not establish Linux process ownership, tmpfs or real ORCA behavior.

`tools/checkpoint_acceptance.py production` keeps bound dependencies at 0400 and
requires live independent 0600 copies, real successful O_RDWR opens, exact-name
MORead consumption for the valid donor, negative pre-GUESS behavior, immutable
before/after identities and valid publication/artifact receipts. Its baseline
`matrix` action is separate and prohibited for candidate plans. Both timeout
latches and the stop/no-retry policy remain. Recovery still requires completed
SP evidence for each second launch. The separately prepared small water control
can establish bounded recovery mechanics if the incident family is too slow;
it cannot establish the incident family's SCF state or completed science.

Focused Mac checks are development evidence. Full Linux checks, installed-wheel
checks and bounded actual-engine acceptance remain owner-side requirements.
