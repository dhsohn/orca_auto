# Checkpoint acceptance through the ordinary queue

This helper is for owner execution on Linux after the execution hold is lifted.
It has not been run against ORCA in the authoring Mac snapshot. It launches only
`OrcaQueueWorker.run_once()` through the installed runtime; the ordinary worker
and child perform admission, engine launch, shutdown and publication. There is
no engine wrapper, direct engine invocation, outer reservation, queue metadata
edit, source-data chmod, donor manufacture or automatic retry.

## Inputs required before execution

Supply an installed **baseline** runtime built from
`909f3bdae67efb53e69a8e9a2862104417329d51` (record any separately deployed Slack
frame patch), an installed **candidate** runtime, and the operational worker's
runtime prefix. A prefix is the Python environment containing `bin/python`,
often `<prepared-runtime>/.venv`, not the prepared runtime's parent directory.
The script records package-tree, Python, engine, MPI and configuration identities.
The source revision in the plan is an owner attestation: a package digest alone
does not establish a Git revision. This snapshot has no Git metadata.

For each family supply hash-pinned original/bound reference input, reviewed SP
diagnostic and dependencies, a written derivation review, and authentic donor
bytes. The first family is `cp_apical_optts_hc37_20261110`:

| Donor | Original generation under `/home/daehyupsohn/orca_runs/cp_apical_optts_hc37_20261110` | SHA-256 |
| --- | --- | --- |
| Candidate positive control | `20261110-105640-30f13617/job.gbw` | `307f569201fe700bb1082cb6aff99cec146046b213c9f9f14c54ed7177a16301` |
| Authentic interrupted startup | `20261110-170409-bd8665b2/job.gbw` | `7130f4c344e9200f8a383be02c0d06970aaa26ef2c3ee9f54c760fe4e17b85f7` |

The positive donor becomes a positive control only after successful consumption.
No alternate donor is selected when a path/hash is missing. Stop and supply the
missing evidence. The harness does not generate donors. A separately authorized
normally queued donor calculation, at most one per family, needs its own bounded
execution and scientific receipts; truncating a GBW is not an authentic negative.

Prepare the diagnostic as `probe.inp`, preserving atom identity/order, charge,
multiplicity, method, basis/ECP, relativity, solvent and state settings. The review
must identify the verified input hashes and every intentional derivative change:
single point, explicit route `MORead`, `%moinp "probe.moinp.gbw"`, `%scf AutoStart
false MaxIter 1 end`, and removal of conflicting guess/operation directives.
Use explicit `%pal nprocs 4 end` and `%maxcore 2048` for the 8 GiB request.
Do not use NoIter. The four baseline cells and the two candidate content controls receive identical
source input/dependency bytes except for the donor. Bound input hashes agree
within each runtime stage; snapshot 4 deliberately changes only the engine MOInp
basename, so bound-input hashes are not expected to agree across stages.
It checks syntax/closure/resources but cannot certify the scientific equivalence
of an arbitrary edited input: the hash-pinned derivation review is required.

For first-family recovery, also supply reviewed SP inputs `recovery_default` and
`recovery_explicit`, each with explicit four-process/8 GiB resources and matching
chemistry. Allow enough SCF iterations for a completed SP within 180 seconds.
The default input must permit automatic recovery (no explicit guess, no
NoAutoStart/AutoStart false or state controls); the explicit input must request
PModel/PAtom/Hueckel/HCore. Record these deliberate policy differences in the
review. A slow or too-fast case may be inconclusive; it is not retried.

## Plan and commands

Keep the helper, plans and prepared diagnostic inputs outside both runtimes and
operational jobs. Use the experiment root
`/home/daehyupsohn/orca_review_fix_20261114/science-validation`.
First collect identities, with the owner-supplied runtime paths:

```bash
SCIENCE=/home/daehyupsohn/orca_review_fix_20261114/science-validation
HARNESS="$SCIENCE/tools/checkpoint_acceptance.py"
"$BASELINE_PREFIX/bin/python" -I "$HARNESS" identity > "$SCIENCE/baseline-identity.json"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" identity > "$SCIENCE/candidate-identity.json"
"$PRODUCTION_PREFIX/bin/python" -I "$HARNESS" identity > "$SCIENCE/production-identity.json"
```

The owner must place the reviewed helper at `$HARNESS`; these commands do not
copy/deploy it remotely. Populate this JSON shape. Every `REQUIRED` must be
replaced with verified evidence. `dependencies` is the complete closure excluding
the matrix donor, with plain names exactly matching the input references. Use
`[]` only for an inline geometry with no other dependencies.

```json
{
  "schema": "orca-checkpoint-acceptance-v1",
  "stage": "baseline",
  "source_revision": "909f3bdae67efb53e69a8e9a2862104417329d51",
  "experiment_root": "/home/daehyupsohn/orca_review_fix_20261114/science-validation",
  "session": "baseline-first",
  "runtime_prefix": "REQUIRED",
  "runtime_tree_sha256": "REQUIRED",
  "production_runtime_prefix": "REQUIRED",
  "production_runtime_tree_sha256": "REQUIRED",
  "production_config": {"path": "/home/daehyupsohn/orca_auto/config/orca_auto.yaml", "sha256": "REQUIRED"},
  "engine": {"path": "REQUIRED", "sha256": "REQUIRED"},
  "mpi": {"path": "REQUIRED", "sha256": "REQUIRED"},
  "families": [{
    "name": "cp_apical_optts_hc37_20261110",
    "reference_input": {"path": "REQUIRED", "sha256": "REQUIRED"},
    "derivation_review": {"path": "REQUIRED", "sha256": "REQUIRED"},
    "diagnostic": {"path": "REQUIRED", "sha256": "REQUIRED", "dependencies": []},
    "positive_donor": {"path": "REQUIRED", "sha256": "307f569201fe700bb1082cb6aff99cec146046b213c9f9f14c54ed7177a16301"},
    "preguess_donor": {"path": "REQUIRED", "sha256": "7130f4c344e9200f8a383be02c0d06970aaa26ef2c3ee9f54c760fe4e17b85f7"},
    "recovery_default": {"path": "REQUIRED", "sha256": "REQUIRED", "dependencies": []},
    "recovery_explicit": {"path": "REQUIRED", "sha256": "REQUIRED", "dependencies": []},
    "explicit_guess_output": "MODEL POTENTIAL"
  }]
}
```

A dependency descriptor is `{"name":"input.xyz","path":"/absolute/source/input.xyz","sha256":"verified hash"}`.
Use a candidate plan with `stage: candidate`, its actual source/runtime identities
and a fresh session name. Keep the diagnostic/donor hashes identical to baseline.
Both the selected and operational runtimes' scoped admission call sites are checked before any submission.
The script checks installed import locations; `PYTHONPATH` cannot substitute a
checkout. `identity` never runs ORCA or MPI.

```bash
FAMILY=cp_apical_optts_hc37_20261110
"$BASELINE_PREFIX/bin/python" -I "$HARNESS" preflight --plan "$SCIENCE/baseline.json" --family "$FAMILY"
"$BASELINE_PREFIX/bin/python" -I "$HARNESS" matrix --plan "$SCIENCE/baseline.json" --family "$FAMILY"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" preflight --for production --plan "$SCIENCE/candidate.json" --family "$FAMILY"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" production --plan "$SCIENCE/candidate.json" --family "$FAMILY"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" preflight --for recovery --plan "$SCIENCE/candidate.json" --family "$FAMILY"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" recovery --plan "$SCIENCE/candidate.json" --family "$FAMILY"
```

Run through the owner's normal SSH access to node01, as the ordinary engine user.
Do not execute through sudo. The helper refuses root, effective/permitted/ambient
capabilities, wrong runtime hashes, unsafe roots, missing inputs, missing strace,
insufficient affinity/address-space/cgroup limits, missing validation dependency,
and stale or unsafe scratch entries. It never cleans an operational scratch pool.
The scratch capacity lock and summed-memory guard are the production mechanisms.

## Bounds and cleanup

The session has one private runs root, one pending row, the operational admission
limit, and a symlink from its `.admission` to the operational store (ADR 0015).
Its generated config disables all outbound notifications. With `use_scratch: true`
(the default), it retains the operational scratch root and reserve. An explicit
candidate-only `use_scratch: false` removes the two scratch settings solely from
the private experiment config; the operational config is unchanged. Preflight does not create a session or launch work.

Each worker invocation has a 180-second wall bound, including launch overhead.
A separate watchdog survives loss of the supervisor, requests normal cancellation
of only that queue ID and observes cleanup for at most 60 seconds. The session's
monotonic deadline is 35 minutes, reserves cleanup time and rejects reuse across
a reboot. There are at most eight baseline/production diagnostic launches and six recovery launches
(three cases, each interruption plus one rebind). No donor launches are included.
Limits are upper bounds, not permission to retry an inconclusive cell.

The supervisor signals only its pidfd-pinned ordinary worker for the intentional
pre/post interruption. Worker shutdown stops the child/engine through normal
ownership and requeues it. After cleanup, a second ordinary worker claims the
same row and creates the replacement generation. Frozen output determines whether
the actual interruption hit the intended phase; a missed phase stops acceptance.
The explicit-guess case interrupts after GUESS and checks preservation in both
the rebound input and the engine's next output.

The private worker uses its existing `sleep_fn` hook to split each ordinary
five-second poll sleep into at most 50 ms sleeps. It reads the shutdown flag
set by the ordinary signal handler and returns when that flag is set; otherwise
it waits the full original interval. It does not raise from a signal handler,
change queue polling cadence, signal a child/engine, or wrap or delay the engine.
Scheduling and I/O can still delay shutdown. The trigger remains real startup
output for pre-GUESS and real completed-GUESS output for post-GUESS; only frozen
output after normal cleanup establishes which phase was actually interrupted.

Every advance requires the experiment's slots to clear and all observed child
identities/engine process groups to be gone. Reads use `list_all_slots`, which
does not normalize foreign records. Foreign slots need not be empty. Unresolved
cleanup writes `STOP.json`, retains the reservation/evidence, and forbids further
launches. The helper never frees a slot or kills an engine directly. If normal
cancellation cannot complete, the owner must investigate the retained ownership;
the helper reports unresolved cleanup rather than claiming termination.

A capacity refusal/deferral, timeout, missing phase, malformed receipt or missing
scientific evidence stops the session. Do not erase `STOP.json` to retry. Preserve
all evidence and make a new reviewed plan/session only after resolving the cause.
If sufficient session time remains, a second family with its own matching donors
may be included in the original plan and run with `matrix --family SECOND` in its reviewed stage. Recovery is intentionally limited to the first
family. Inputs for the second family are never inferred from the first.

## Reading the evidence

Each result retains queue/snapshot/generation identity, source and bound hashes,
mode intervention, actual scratch path/mode/UID/hash, observed process/slot
identities, output, state, machine validation result and artifact identities.
`strace -ff` surrounds the ordinary worker tree; `opens.*` and extracted
`checkpoint_open_trace` show actual file-open flags/errors outside generations.

`diagnostic_pass` means observed MORead of `probe.moinp.gbw`, completed GUESS and
entry into SCF, the intended scratch mode, and a valid machine receipt. It is not
successful calculation or publication acceptance. MaxIter 1 can intentionally
fail convergence. Inspect scratch-publication details in the state, worker log
and machine receipt separately, especially for 0600 dependencies modified by
ORCA. A passing GUESS followed by rejected publication is not a permission fix.
Every completed SP additionally requires SCF convergence, finite final energy,
normal termination and validated artifact receipts. Recovery requires that
evidence, one attempt, a fresh generation and the intended seed/guess policy.

| Observation | Narrow interpretation |
| --- | --- |
| V400 EACCES, V600 reaches SCF | Mode affects this engine/path |
| Both V pass, both P fail after successful open | Content/readiness or compatibility implicated |
| V400 fails, V600 passes, P600 still fails | Both permission and content may contribute |
| V600 fails | Positive control failed; comparison inconclusive |
| All four pass | Original failure not reproduced |

Exit zero from the baseline-only `matrix` means evidence collection completed, not that a permission
change is accepted. Review all four result files and traces. Neither this matrix
nor the GUESS gate proves an arbitrary GBW's structure or chemical compatibility.


## Candidate production path after the observed baseline

The retained 2026-11-14 baseline already supplies the permission comparison:
V400/P400 failed O_RDWR with EACCES, V600 consumed MOREAD and entered SCF,
and P600 opened O_RDWR but failed before completed GUESS. All four were
terminal failures with valid machine/artifact receipts, neither timeout fired,
and none supplied completed-SP evidence. Keep that evidence and its original
helper/input hashes separate. Do not reinterpret it as a candidate run.

`matrix` is restricted to `stage: baseline`. Candidate plans use `production`,
which runs V-private and P-private without any mode intervention. It requires:

- unchanged source donor and immutable bound-input bytes, modes, inodes and mtimes;
- the exact MOInp basename in the bound input, an independent single-link 0600
  engine copy and a live single-link 0400 immutable source with the expected hash;
- a successful real O_RDWR open of that exact copy, with no EACCES in its trace;
- valid machine and artifact receipts, one attempt, and committed scratch
  publication when scratch is enabled;
- V-private: exact-name MOREAD, completed GUESS and subsequent SCF entry;
  P-private: successful open but no completed GUESS/SCF entry.

Engine-copy bytes may change. They are not compared to the immutable input hash
after execution. A completed status still requires actual completed-SP evidence;
MaxIter 1 failure is an expected diagnostic outcome, not scientific completion.
A missing live observation, changed negative behavior, capacity refusal or timeout
stops the session for review. The supervisor/watchdog STOP latch remains active.

Transfer the authored tools only to the experiment control directory and install
an owner-verified candidate runtime separately after Linux validation. In the
ordinary Linux shell, collect its identity and create new plans from the existing
complete candidate plan. Keep the prepared 59-atom family and donor descriptors
unchanged. These commands do not overwrite baseline plans/evidence:

```bash
set -euo pipefail
SCIENCE=/home/daehyupsohn/orca_review_fix_20261114/science-validation
HARNESS="$SCIENCE/tools/checkpoint_acceptance.py"
: "${CANDIDATE_PREFIX:?Set the new installed environment prefix containing bin/python}"
: "${CANDIDATE_REVISION:?Set the exact 40-hex source revision attestation}"
: "${ACCEPTANCE_TAG:?Set a fresh short session tag using letters digits underscore hyphen}"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" identity > "$SCIENCE/private-runtime-identity.json"
python3 - "$SCIENCE" "$CANDIDATE_PREFIX" "$CANDIDATE_REVISION" "$ACCEPTANCE_TAG" <<'PY'
import json, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
prefix, revision, tag = sys.argv[2:]
assert re.fullmatch(r"[0-9a-f]{40}", revision)
assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", tag)
base = json.loads((root / "candidate.json").read_text())
identity = json.loads((root / "private-runtime-identity.json").read_text())
assert identity["prefix"] == str(pathlib.Path(prefix).resolve())
for mode, enabled in (("scratch", True), ("noscratch", False)):
    plan = dict(base, stage="candidate", source_revision=revision,
                runtime_prefix=identity["prefix"],
                runtime_tree_sha256=identity["package_tree_sha256"],
                session=f"{tag}-{mode}", use_scratch=enabled)
    with (root / f"{tag}-{mode}.json").open("x") as handle:
        json.dump(plan, handle, indent=2)
PY
FAMILY=cp_apical_optts_hc37_20261110
for MODE in scratch noscratch; do
  PLAN="$SCIENCE/$ACCEPTANCE_TAG-$MODE.json"
  "$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" preflight --for production --plan "$PLAN" --family "$FAMILY"
  "$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" production --plan "$PLAN" --family "$FAMILY"
done
```

Inspect both result pairs and live copy/open/publication evidence before moving
to recovery. Do not increase a timeout or discard a STOP receipt to obtain a pass.
Full-family recovery uses the original `recovery_default`/`recovery_explicit`
inputs and the existing `recovery` command in a fresh reviewed session. It needs
completed SP within 180 seconds; the 59-atom family's SCF may exceed that bound.

## Separate bounded small-system recovery mechanics

If full-family completion is not feasible within the approved bound, use a
separate water control. This preserves B3LYP D3BJ def2-SVP CPCM(THF), TightSCF
DefGrid3, four processes, maxcore 2048 and MaxIter 300. The neutral singlet water
geometry/state is explicitly different from the anionic 59-atom family; no
family inputs are edited. The generated derivation review lists every setting
and the sole default/explicit change (PModel). It is a mechanics acceptance,
not a substitute for family convergence, SCF-state equivalence, OptTS or Freq.
The preparer only writes input/review/hash files. It does not generate GBWs or
launch workers/engines. Run it at its final destination so descriptors are exact:

```bash
set -euo pipefail
python3 "$SCIENCE/tools/prepare_checkpoint_mechanics.py" "$SCIENCE/$ACCEPTANCE_TAG-water"
(cd "$SCIENCE/$ACCEPTANCE_TAG-water" && sha256sum --check SHA256SUMS)
python3 - "$SCIENCE" "$ACCEPTANCE_TAG" <<'PY'
import json, pathlib, sys
root, tag = pathlib.Path(sys.argv[1]), sys.argv[2]
fragment = json.loads((root / f"{tag}-water/plan-family.json").read_text())
for mode in ("scratch", "noscratch"):
    plan = json.loads((root / f"{tag}-{mode}.json").read_text())
    plan.update(session=f"{tag}-water-{mode}", families=fragment["families"])
    with (root / f"{tag}-water-{mode}.json").open("x") as handle:
        json.dump(plan, handle, indent=2)
PY
for MODE in scratch noscratch; do
  PLAN="$SCIENCE/$ACCEPTANCE_TAG-water-$MODE.json"
  "$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" preflight --for recovery --plan "$PLAN" --family water_recovery_mechanics
  "$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" recovery --plan "$PLAN" --family water_recovery_mechanics
done
```

Each plan permits only the three existing interruption/rebind pairs:
pre-default skips orbitals, post-default injects and consumes the private copy,
and post-explicit preserves PModel. All second launches still require a real
converged, normally terminated SP, finite final energy, one attempt, preserved
previous-generation artifacts and valid publication/receipts. No mock readiness,
engine wrapper, sleep in the engine, shortened method, NoIter or edited output
is allowed. A system that is too quick to interrupt at the intended phase is
inconclusive; stop without an automatic retry. `acceptance_scope` in each result
identifies `small_system_mechanics`. Full-family science remains unverified until
it has its own completed-SP evidence under an explicitly reviewed execution budget.

## Reviewed no-scratch water follow-up, 2026-11-15

Retain `private-r3-water-noscratch` and its STOP. Its first launch triggered on
startup, but the frozen output contains `Finished Guess after 0.7 sec`, following
a reported 1.0-second SHARK setup. It is still `inconclusive_phase_miss`; there
was no accepted pre-default rebind and neither post case ran. The retained
scratch results and both production-control modes need no repetition here.

The previous helper SHA-256 was
`15a4af30ddf9d413ba220638d580ef7c600f65c9d360793c933dca2e2dcb9b89`.
It polled output every 50 ms, but its ordinary worker used a five-second
`time.sleep`. The worker's normal SIGTERM handler only sets a flag, and that
sleep continues before the shutdown sweep signals the child. The runner then
checks shutdown between waits of up to 0.2 seconds. A native Python signal
probe reproduced the avoidable parent delay; the retained result JSONs lack
signal timestamps, so they do not establish the exact old signal-to-stop delay.
The sleep hook above removes that five-second wait after the shutdown flag is
set. It changes only this external helper; installed package bytes, scientific
inputs, trigger criteria, frozen-phase and completed-SP gates remain unchanged.

After the owner replays Linux `make check` in the isolated source worktree,
place the corrected helper at
`followup-20261115/tools/checkpoint_acceptance.py` under the science root.
Keep the old helper/evidence in place. Execute the following **once** on node01
as the ordinary engine user in the same verified ORCA/MPI environment. It uses
the now-shared production/candidate runtime and the existing `private-r2-water`
descriptors, with a new private session under the authorized follow-up root.
Preparation checks the old row is cancelled and its observed ownership has
cleared; it never resumes, repairs or edits that row. A failed check stops here.

```bash
set -euo pipefail
set -o noclobber
SCIENCE=/home/daehyupsohn/orca_review_fix_20261114/science-validation
FOLLOWUP="$SCIENCE/followup-20261115"
HARNESS="$FOLLOWUP/tools/checkpoint_acceptance.py"
CANDIDATE_PREFIX=/home/daehyupsohn/.local/share/orca_auto/deployments/review-fixes-20261114/candidate-r3-final/runtimes/10.2.4-905655190a01d835/.venv
PRODUCTION_PREFIX="$CANDIDATE_PREFIX"
PLAN="$FOLLOWUP/water-noscratch.json"
printf '%s  %s\n' b433f69afc839902fca522667c5f3d7eb79bfd41520d245bb2fc485cede0a45e "$HARNESS" | sha256sum --check
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" identity > "$FOLLOWUP/runtime-identity.json"
"$CANDIDATE_PREFIX/bin/python" -I - "$SCIENCE" "$FOLLOWUP" "$CANDIDATE_PREFIX" "$PRODUCTION_PREFIX" <<'PY'
import json, pathlib, runpy, sys
from orca_auto.orca.queue.adapter import get_entry_by_id

science, followup = map(pathlib.Path, sys.argv[1:3])
candidate, production = sys.argv[3:]
h = runpy.run_path(str(followup / "tools/checkpoint_acceptance.py"))
runtime = json.loads((followup / "runtime-identity.json").read_text())
package_sha = "ee0a449d534b95a58f7f38c42c465c3115c2ac8a47b7a6f720dc071d7527f2b7"
assert candidate == production == runtime["prefix"]
assert runtime["package_tree_sha256"] == package_sha
assert runtime["installed_under_prefix"] and runtime["scoped_admission_calls"]
old_plan_path = science / "private-r3-water-noscratch.json"
plan = json.loads(old_plan_path.read_text())
old_session = pathlib.Path(plan["experiment_root"]) / plan["session"]
assert old_session == science / "private-r3-water-noscratch"
stop = old_session / "STOP.json"
assert stop.is_file()
result_path = old_session / "evidence/water_recovery_mechanics-pre-default-result.json"
result = json.loads(result_path.read_text())
assert result["verdict"] == "inconclusive_phase_miss"
assert result["first"]["triggered"] and result["stopped"]["observation"]["finished_guess"]
assert "second" not in result
queue_id = result["stopped"]["row"]["queue_id"]
row = get_entry_by_id(old_session / "runs", queue_id)
assert row is not None and row.status.value == "cancelled"
assert h["cleanup_complete"](old_session / "runs", queue_id, result["first"]["observed_slots"])
assert len(plan["families"]) == 1
family = plan["families"][0]
assert family["name"] == "water_recovery_mechanics"
assert family["acceptance_scope"] == "small_system_mechanics"
originals = [old_plan_path, stop, result_path]
for key in ("reference_input", "derivation_review", "recovery_default", "recovery_explicit"):
    original = h["verified_file"](family[key])
    assert original.parent == science / "private-r2-water"
    originals.append(original)
assert not (followup / "water-noscratch").exists()
originals += [pathlib.Path(item["path"]) for item in result["stopped"]["artifacts"].values()]
with (followup / "preserved-old-identities.json").open("x") as handle:
    json.dump({str(p): h["identity"](p) for p in originals}, handle, indent=2)
plan.update(stage="candidate", experiment_root=str(followup), session="water-noscratch",
            use_scratch=False, runtime_prefix=candidate, runtime_tree_sha256=package_sha,
            production_runtime_prefix=production, production_runtime_tree_sha256=package_sha)
with (followup / "water-noscratch.json").open("x") as handle:
    json.dump(plan, handle, indent=2)
PY
MPI_BIN="$("$CANDIDATE_PREFIX/bin/python" -I -c 'import json,pathlib,sys; print(pathlib.Path(json.load(open(sys.argv[1]))["mpi"]["path"]).parent)' "$PLAN")"
export PATH="$MPI_BIN:$PATH"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" preflight --for recovery --plan "$PLAN" --family water_recovery_mechanics > "$FOLLOWUP/preflight.json"
"$CANDIDATE_PREFIX/bin/python" -I "$HARNESS" recovery --plan "$PLAN" --family water_recovery_mechanics > "$FOLLOWUP/recovery.stdout" 2> "$FOLLOWUP/recovery.stderr"
"$CANDIDATE_PREFIX/bin/python" -I - "$FOLLOWUP" <<'PY'
import json, pathlib, runpy, sys
root = pathlib.Path(sys.argv[1])
h = runpy.run_path(str(root / "tools/checkpoint_acceptance.py"))
for name, before in json.loads((root / "preserved-old-identities.json").read_text()).items():
    assert h["identity"](pathlib.Path(name)) == before, name
session = root / "water-noscratch"
assert not (session / "STOP.json").exists()
assert json.loads((session / "session.json").read_text())["recovery_launches"] == 6
for label in ("pre-default", "post-default", "post-explicit"):
    result = json.loads((session / "evidence" / f"water_recovery_mechanics-{label}-result.json").read_text())
    assert result["scientific_acceptance_pass"]
    assert result["acceptance_scope"] == "small_system_mechanics"
    assert h["cleanup_complete"](session / "runs", result["final"]["row"]["queue_id"],
                                 result["first"]["observed_slots"] + result["second"]["observed_slots"])
    print(label, result["final"]["observation"]["energies_eh"],
          result["final"]["snapshot"]["recovery"])
PY
```

The maximum remains six launches, 180 seconds per invocation and 35 minutes for
the session. Any miss, timeout, publication/copy failure or unresolved ownership
stops the sequence with no retry. Keep every new and old STOP/result file. On
failure, inspect only; do not rerun this block or start another session.

Accept only all three passing result receipts: pre-default has frozen ineligible
GUESS evidence and no rebound orbital seed; post-default has eligible frozen
GUESS evidence and consumes the verified private MORead copy; post-explicit
preserves PModel in the bound input and engine output. All replacement runs must
converge and terminate normally with finite SP energy, exactly one attempt,
fresh generation and valid machine/artifact receipts. Previous scientific
artifacts must be unchanged and experiment ownership clear. Inspect the
`interruption-observed.out`, frozen outputs, ordinary worker logs and timestamped
`opens.*` SIGTERM records to compare trigger, parent/child shutdown and actual
phase. These results establish water recovery mechanics only; full 59-atom
chemistry remains outside this follow-up.
