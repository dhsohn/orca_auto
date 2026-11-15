#!/usr/bin/env python3
"""Write a reviewed small-system recovery-mechanics family; never launch an engine."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# Preserve the incident family's electronic method and numerical/resource settings.
# A separate neutral singlet water geometry is deliberate, not a family derivative.
_DEFAULT = """# Separate water recovery-mechanics control; no incident-family science claim.
! B3LYP D3BJ def2-SVP CPCM(THF) TightSCF DefGrid3 SP
%pal nprocs 4 end
%maxcore 2048
%scf MaxIter 300 end
* xyz 0 1
O  0.000000  0.000000  0.000000
H  0.758602  0.000000  0.504284
H -0.758602  0.000000  0.504284
*
"""
_REVIEW = """# Water recovery mechanics control

This is a separate three-atom neutral singlet (10-electron) water SP control.
It is not a derivative or substitute for the 59-atom anionic singlet family.
No original family geometry, charge, multiplicity, input or donor is edited.
The fixed coordinates in reference.inp are an ordinary bent water geometry;
they are not an optimized structure and no geometry-property claim is made.

B3LYP D3BJ def2-SVP CPCM(THF), TightSCF DefGrid3, four processes, maxcore 2048
and SCF MaxIter 300 are retained from the reviewed family recovery inputs.
There are no ECP, relativistic, root, occupation or spin-treatment overrides.
There is no Opt, Freq or NoIter request. Inline coordinates eliminate external
dependencies. reference.inp and recovery_default.inp are identical. The only
active change in recovery_explicit.inp is adding route PModel; its expected
initial-guess label is MODEL POTENTIAL. No engine-generated GBW is supplied or
manufactured by preparation: the interrupted ordinary worker generates it.

Run only the harness recovery action, in a fresh candidate plan/session with
acceptance_scope small_system_mechanics. Keep the 180-second launch limit,
watchdog/STOP latch, shared admission and three interruption/rebind cases.
Pre-GUESS capture may be missed if this system runs too quickly; a miss or a
timeout is inconclusive and stops the session without automatic retries.
Do not delay/wrap the engine, edit live output or manufacture phase evidence.
A successful second launch must actually converge SCF, produce a finite SP
energy, terminate normally, complete exactly one attempt and validate the
machine/artifact receipts and publication. The post-default case must consume
the private MORead copy; pre-default must skip it and post-explicit must retain
PModel. Original generation bytes/modes must remain unchanged.

Passing these cases establishes small-system recovery mechanics only. It does
not establish convergence, identical SCF states, optimization, frequencies,
barriers, donor compatibility or completed science for the incident family.
Runtime/engine/MPI identities and real results are supplied by owner execution.
No engine has been run by this preparer.
"""


def prepare(destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    if destination.is_symlink() or destination.resolve() != destination:
        raise ValueError("Use a real absolute preparation directory")
    destination.mkdir(parents=True, exist_ok=False)
    contents = {
        "reference.inp": _DEFAULT,
        "recovery_default.inp": _DEFAULT,
        "recovery_explicit.inp": _DEFAULT.replace("DefGrid3 SP\n", "DefGrid3 SP PModel\n"),
        "derivation-review.md": _REVIEW,
    }
    descriptors = {}
    for name, text in contents.items():
        path = destination / name
        path.write_text(text)
        descriptors[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    family = {
        "name": "water_recovery_mechanics",
        "acceptance_scope": "small_system_mechanics",
        "reference_input": descriptors["reference.inp"],
        "derivation_review": descriptors["derivation-review.md"],
        "recovery_default": {**descriptors["recovery_default.inp"], "dependencies": []},
        "recovery_explicit": {**descriptors["recovery_explicit.inp"], "dependencies": []},
        "explicit_guess_output": "MODEL POTENTIAL",
    }
    plan = destination / "plan-family.json"
    plan.write_text(json.dumps({"families": [family]}, indent=2) + "\n")
    (destination / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(destination.iterdir())
            if path.is_file()
        )
    )
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(prepare(parser.parse_args().destination))


if __name__ == "__main__":
    main()
