from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import RUN_REPORT_JSON_FILE, RUN_STATE_FILE
from orca_auto.core.engine_runner import confined_output_identity, executable_identity
from orca_auto.core.machine_observation import machine_json_bytes
from orca_auto.orca import state as orca_state
from tests.engine_artifact_helpers import bind_report_generation

_ENGRAD_TEMPLATE = """#
# Number of atoms
#
 3
#
# The current total energy in Eh
#
  {energy}
#
# The current gradient in Eh/bohr
#
       0.000085816662
"""


def _write_multi_xyz(path: Path, frames: int) -> None:
    blocks = []
    for index in range(frames):
        blocks.append(f"3\n -100.{index:04d}\nH 0 0 0\nO 1 0 0\nO 3 0 0\n")
    path.write_text("".join(blocks), encoding="utf-8")


def _validate_common_machine(path: Path) -> None:
    validator = os.environ.get("FACTORY_MACHINE_CONTRACT_VALIDATOR")
    if validator:
        subprocess.run([sys.executable, validator, "--machine", str(path)], check=True)


def _write_orca_generation_report(job_dir: Path, report: dict[str, Any]) -> Path:
    generation, provenance = _orca_generation(job_dir, route_line="! Opt")
    report["schema_version"] = 1
    report["engine"] = "orca"
    report["input"] = {"primary_path": provenance["bound_selected_identity"]["path"]}
    report["execution_provenance"] = provenance
    status = report.setdefault("status", {"state": "completed", "reason": "normal_termination"})
    if isinstance(status, dict) and status.get("state") == "completed":
        output = generation / "orca.out"
        output.write_text(
            _completed_opt_output_text(
                route_line="! Opt",
                charge=0,
                multiplicity=1,
                atom_label="H",
                energy=-100.0,
            ),
            encoding="utf-8",
        )
        engine_payload = report.setdefault("engine_payload", {})
        assert isinstance(engine_payload, dict)
        attempts = engine_payload.setdefault("attempts", [{"index": 1}])
        assert isinstance(attempts, list) and attempts and isinstance(attempts[-1], dict)
        attempts[-1].update(
            {
                "out_path": str(output),
                "output_identity": confined_output_identity(generation, output),
            }
        )
        final_result = engine_payload.setdefault("final_result", {"reason": "normal_termination"})
        assert isinstance(final_result, dict)
        final_result["last_out_path"] = str(output)
    return _publish_orca_machine(generation, report)


def _publish_orca_machine(generation: Path, report: dict[str, Any]) -> Path:
    (generation / RUN_STATE_FILE).write_text(json.dumps(report), encoding="utf-8")
    observation = orca_state._machine_observation(generation, report)
    path = generation / RUN_REPORT_JSON_FILE
    path.write_bytes(machine_json_bytes(observation))
    return path


def _orca_generation(
    job_dir: Path,
    *,
    route_line: str = "! SP",
    charge: int = 0,
    multiplicity: int = 1,
    extra_directives: str = "",
    atom_label: str = "H",
    xyzfile: bool = False,
) -> tuple[Path, dict[str, Any]]:
    selected = job_dir / "current.inp"
    directives = f"{extra_directives.strip()}\n" if extra_directives.strip() else ""
    if xyzfile:
        (job_dir / "input.xyz").write_text(
            f"1\nbound geometry\n{atom_label} 0 0 0\n",
            encoding="utf-8",
        )
        geometry = f"* xyzfile {charge} {multiplicity} input.xyz\n"
    else:
        geometry = f"* xyz {charge} {multiplicity}\n{atom_label} 0 0 0\n*\n"
    selected.write_text(
        f"{route_line}\n{directives}{geometry}",
        encoding="utf-8",
    )
    state: dict[str, Any] = {"selected_inp": str(selected)}
    generation = bind_report_generation(job_dir, state)
    provenance = state["execution_provenance"]
    if xyzfile:
        materialized_geometry = generation / "input.xyz"
        materialized_geometry.write_bytes((job_dir / "input.xyz").read_bytes())
        provenance["materialized_inputs"] = {
            "dependency_000000": executable_identity(materialized_geometry)
        }
    return generation, provenance


def _completed_opt_output_text(
    *,
    route_line: str,
    charge: int,
    multiplicity: int,
    atom_label: str,
    energy: float,
    version: str = "6.0.1",
) -> str:
    return "\n".join(
        (
            f"Program Version {version}",
            f"|  1> {route_line}",
            f"|  2> * xyz {charge} {multiplicity}",
            "",
            "CARTESIAN COORDINATES (ANGSTROEM)",
            "---------------------------------",
            f"  {atom_label:<2}      0.000000     0.000000     0.000000",
            "",
            f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
            "THE OPTIMIZATION HAS CONVERGED",
            "****ORCA TERMINATED NORMALLY****",
            "",
        )
    )


def _ts_freq_output_text(
    *,
    imaginary: int,
    energy: float = -1.1,
    superseded: bool = False,
    route_line: str = "! HF OptTS Freq",
) -> str:
    """A normally terminated output whose last Hessian has ``imaginary`` modes.

    ``superseded`` prints one more final energy after the frequency section:
    the shape of an OptTS run whose only Hessian belongs to an earlier
    geometry and therefore characterizes nothing.
    """
    modes = [f"   {index}:      {-500.0 - index * 10:.2f} cm**-1" for index in range(imaginary)]
    modes.append("   9:       412.55 cm**-1")
    lines = [
        "Program Version 6.0.1",
        f"|  1> {route_line}",
        f"FINAL SINGLE POINT ENERGY     {energy:.12f}",
        "",
        "VIBRATIONAL FREQUENCIES",
        "-----------------------",
        *modes,
        "",
    ]
    if superseded:
        lines.append(f"FINAL SINGLE POINT ENERGY     {energy:.12f}")
    lines.extend(("****ORCA TERMINATED NORMALLY****", ""))
    return "\n".join(lines)


def _orca_stage_dir(
    root: Path,
    name: str,
    *,
    energy: float,
    reason: str,
    route_line: str = "! Opt",
    charge: int = 0,
    multiplicity: int = 1,
    extra_directives: str = "",
    atom_label: str = "H",
    xyzfile: bool = False,
    version: str = "6.0.1",
    output_text: str | None = None,
    status_state: str = "completed",
    last_out_name: str | None = "orca.out",
) -> Path:
    job_dir = root / name
    job_dir.mkdir(parents=True)
    generation, provenance = _orca_generation(
        job_dir,
        route_line=route_line,
        charge=charge,
        multiplicity=multiplicity,
        extra_directives=extra_directives,
        atom_label=atom_label,
        xyzfile=xyzfile,
    )
    (generation / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy=f"{energy:.12f}"), encoding="utf-8"
    )
    output = generation / "orca.out"
    output.write_text(
        output_text
        if output_text is not None
        else _completed_opt_output_text(
            route_line=route_line,
            charge=charge,
            multiplicity=multiplicity,
            atom_label=atom_label,
            energy=energy,
            version=version,
        ),
        encoding="utf-8",
    )
    output_identity = confined_output_identity(generation, output)
    # ``last_out_name`` names the terminal output the state records: the
    # written one by default, a never-written name to exercise a missing
    # output, and ``None`` for a run that recorded no terminal output at all.
    final_result: dict[str, Any] = {"reason": reason}
    if last_out_name is not None:
        final_result["last_out_path"] = str(generation / last_out_name)
    report = {
        "schema_version": 1,
        "engine": "orca",
        "job": {"id": name},
        "status": {"state": status_state, "reason": reason},
        "input": {"primary_path": provenance["bound_selected_identity"]["path"]},
        "execution_provenance": provenance,
        "engine_payload": {
            "attempts": [
                {
                    "index": 1,
                    "out_path": str(output),
                    "output_identity": output_identity,
                    "markers": {"imaginary_frequency_count": 0},
                }
            ],
            "final_result": final_result,
        },
    }
    _publish_orca_machine(generation, report)
    (generation / "job_report.html").write_text("<html></html>", encoding="utf-8")
    return generation


def _orca_stage(
    stage_id: str,
    stage_dir: Path,
    *,
    status: str,
    label: str,
    task_kind: str = "opt",
) -> dict[str, Any]:
    report = json.loads((stage_dir / RUN_STATE_FILE).read_text(encoding="utf-8"))
    report["job"] = {"id": stage_id}
    engine_payload = report.setdefault("engine_payload", {})
    run_id = str(engine_payload.get("run_id") or f"run-{stage_id}")
    engine_payload["run_id"] = run_id
    _publish_orca_machine(stage_dir, report)
    return {
        "stage_id": stage_id,
        "stage_kind": "orca_stage",
        "status": status,
        "task": {"engine": "orca", "task_kind": task_kind},
        "metadata": {
            "selected_input_label": label,
            "child_job_id": stage_id,
            "run_id": run_id,
        },
        "output_artifacts": [
            {"kind": "orca_output_dir", "path": str(stage_dir)},
            {"kind": "orca_report_json", "path": str(stage_dir / RUN_REPORT_JSON_FILE)},
        ],
    }


def _orca_output_report(out_path: Path) -> dict[str, Any]:
    return {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(out_path)}],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(out_path),
            },
        }
    }


def _payload(workspace: Path, stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "workflow_id": "wf_test",
        "template_name": "conformer_screening",
        "status": "completed",
        "reaction_key": "input",
        "requested_at": "2026-07-03T01:00:00+00:00",
        "metadata": {
            "workspace_dir": str(workspace),
            "last_advanced_at": "2026-07-03T05:30:00+00:00",
        },
        "stages": stages,
    }


def _energy_chain_payload(
    tmp_path: Path,
    name: str,
    out_text: str,
    *,
    engrad_energy: str | None = None,
) -> dict[str, Any]:
    """One completed ORCA stage whose energy comes from the .out/.engrad chain."""
    job_dir = tmp_path / name
    job_dir.mkdir()
    route_line = next(
        (
            line.split(">", 1)[1].strip()
            for line in out_text.splitlines()
            if line.lstrip().startswith("|  1>") and ">" in line
        ),
        "! r2scan-3c Opt TightSCF",
    )
    stage_dir, provenance = _orca_generation(job_dir, route_line=route_line)
    if engrad_energy is not None:
        (stage_dir / "opt.engrad").write_text(
            _ENGRAD_TEMPLATE.format(energy=engrad_energy), encoding="utf-8"
        )
    out_path = stage_dir / "opt.out"
    body = out_text.replace("****ORCA TERMINATED NORMALLY****", "").rstrip()
    out_path.write_text(
        "\n".join(
            (
                "Program Version 6.0.1",
                body,
                "|  2> * xyz 0 1",
                "",
                "CARTESIAN COORDINATES (ANGSTROEM)",
                "---------------------------------",
                "  H       0.000000     0.000000     0.000000",
                "",
                "THE OPTIMIZATION HAS CONVERGED",
                "****ORCA TERMINATED NORMALLY****",
                "",
            )
        ),
        encoding="utf-8",
    )
    output_identity = confined_output_identity(stage_dir, out_path)
    report = {
        "schema_version": 1,
        "engine": "orca",
        "job": {"id": "orca_conformer_01"},
        "status": {"state": "completed", "reason": "normal_termination"},
        "input": {"primary_path": provenance["bound_selected_identity"]["path"]},
        "execution_provenance": provenance,
        "engine_payload": {
            "attempts": [
                {
                    "index": 1,
                    "out_path": str(out_path),
                    "output_identity": output_identity,
                    "markers": {"imaginary_frequency_count": 0},
                }
            ],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(out_path),
            },
        },
    }
    _publish_orca_machine(stage_dir, report)
    (stage_dir / "job_report.html").write_text("<html></html>", encoding="utf-8")
    return _payload(
        tmp_path,
        [_orca_stage("orca_conformer_01", stage_dir, status="completed", label="conf_01")],
    )
