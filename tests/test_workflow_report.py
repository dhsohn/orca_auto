from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.artifacts import (
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
    WORKFLOW_SI_MD_FILE,
)
from orca_auto.core.machine_observation import machine_json_bytes
from orca_auto.flow.workflow import report as workflow_report
from orca_auto.flow.workflow.machine import write_workflow_machine_observation
from orca_auto.flow.workflow.report import (
    _energy_axis_ticks,
    _tick_label,
    collect_workflow_report_data,
    count_xyz_frames,
    latest_engrad_energy,
    write_workflow_html_report,
)
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
    generation, provenance = _orca_generation(job_dir)
    report["schema_version"] = 1
    report["engine"] = "orca"
    report["input"] = {"primary_path": provenance["bound_selected_identity"]["path"]}
    report["execution_provenance"] = provenance
    return _publish_orca_machine(generation, report)


def _publish_orca_machine(generation: Path, report: dict[str, Any]) -> Path:
    (generation / RUN_STATE_FILE).write_text(json.dumps(report), encoding="utf-8")
    observation = orca_state._machine_observation(generation, report)
    path = generation / RUN_REPORT_JSON_FILE
    path.write_bytes(machine_json_bytes(observation))
    return path


def _orca_generation(job_dir: Path) -> tuple[Path, dict[str, Any]]:
    selected = job_dir / "current.inp"
    selected.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    state: dict[str, Any] = {"selected_inp": str(selected)}
    generation = bind_report_generation(job_dir, state)
    return generation, state["execution_provenance"]


def _orca_stage_dir(root: Path, name: str, *, energy: float, reason: str) -> Path:
    job_dir = root / name
    job_dir.mkdir(parents=True)
    generation, provenance = _orca_generation(job_dir)
    (generation / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy=f"{energy:.12f}"), encoding="utf-8"
    )
    output = generation / "orca.out"
    output.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "engine": "orca",
        "job": {"id": name},
        "status": {"state": "completed", "reason": reason},
        "input": {"primary_path": provenance["bound_selected_identity"]["path"]},
        "execution_provenance": provenance,
        "engine_payload": {
            "attempts": [
                {
                    "index": 1,
                    "markers": {"imaginary_frequency_count": 0},
                }
            ],
            "final_result": {
                "reason": reason,
                "last_out_path": str(output),
            },
        },
    }
    _publish_orca_machine(generation, report)
    (generation / "job_report.html").write_text("<html></html>", encoding="utf-8")
    return generation


def _orca_stage(stage_id: str, stage_dir: Path, *, status: str, label: str) -> dict[str, Any]:
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


def test_count_xyz_frames_and_engrad_energy(tmp_path: Path) -> None:
    xyz = tmp_path / "crest_conformers.xyz"
    _write_multi_xyz(xyz, frames=4)
    assert count_xyz_frames(xyz) == 4

    (tmp_path / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-100.123456789012"), encoding="utf-8"
    )
    assert latest_engrad_energy(tmp_path) == pytest.approx(-100.123456789012)


def test_engrad_energy_rejects_non_finite_values(tmp_path: Path) -> None:
    # A corrupt .engrad spelling nan would render as NaN in the report and
    # then crash the machine-observation writer (allow_nan=False) on every
    # advance; a non-finite energy must read as unavailable instead.
    (tmp_path / "opt.engrad").write_text(_ENGRAD_TEMPLATE.format(energy="nan"), encoding="utf-8")
    assert latest_engrad_energy(tmp_path) is None


@pytest.mark.parametrize("link_kind", ("symlink", "hardlink"))
def test_engrad_energy_rejects_linked_generation_file(
    tmp_path: Path,
    link_kind: str,
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir()
    foreign = tmp_path / "foreign.engrad"
    foreign.write_text(_ENGRAD_TEMPLATE.format(energy="-999.0"), encoding="utf-8")
    linked = generation / "linked.engrad"
    if link_kind == "symlink":
        linked.symlink_to(foreign)
    else:
        os.link(foreign, linked)

    assert latest_engrad_energy(generation) is None


def test_engrad_energy_rejects_oversized_generation_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engrad = tmp_path / "oversized.engrad"
    engrad.write_text(
        _ENGRAD_TEMPLATE.format(energy="-100.0") + "x" * 128,
        encoding="utf-8",
    )
    monkeypatch.setattr(workflow_report, "_MAX_ENGRAD_ENERGY_FILE_BYTES", 64)

    assert latest_engrad_energy(tmp_path) is None


def test_collect_ranks_orca_results_and_counts_funnel(tmp_path: Path) -> None:
    crest_dir = tmp_path / "01_crest"
    crest_dir.mkdir()
    conformers = crest_dir / "crest_conformers.xyz"
    _write_multi_xyz(conformers, frames=5)

    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="normal_termination")

    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_conformer_01",
                "stage_kind": "crest_stage",
                "status": "completed",
                "metadata": {"input_role": "molecule", "mode": "nci"},
                "output_artifacts": [{"kind": "crest_conformer", "path": str(conformers)}],
            },
            _orca_stage("orca_conformer_01", stage_a, status="completed", label="conf_01"),
            _orca_stage("orca_conformer_02", stage_b, status="completed", label="conf_02"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.crest_conformer_total == 5
    assert [row.stage_kind for row in data.stage_rows] == [
        "crest_stage",
        "orca_stage",
        "orca_stage",
    ]
    # Lowest energy ranks first; relative energy measured from it.
    assert [entry.label for entry in data.orca_results] == ["conf_02", "conf_01"]
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)
    assert data.orca_results[1].rel_kcal == pytest.approx(0.004 * 627.5094740631, rel=1e-6)
    assert data.orca_results[0].imaginary_count == 0
    assert data.orca_results[0].report_href is not None
    assert "orca_b" in data.orca_results[0].report_href


def test_relaxed_scan_without_html_is_preserved_in_workflow_lineage(
    tmp_path: Path,
) -> None:
    generation = _orca_stage_dir(
        tmp_path,
        "orca_relaxed_scan",
        energy=-100.001,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_relaxed_scan",
        generation,
        status="completed",
        label="relaxed_scan",
    )
    stage["task"] = {
        "engine": "orca",
        "status": "completed",
        "task_kind": "relaxed_scan",
    }
    (generation / "job_report.html").unlink()
    payload = _payload(tmp_path, [stage])

    data = collect_workflow_report_data(tmp_path, payload)

    upstream_machine = generation / RUN_REPORT_JSON_FILE
    assert [row.stage_id for row in data.stage_rows] == ["orca_relaxed_scan"]
    assert data.orca_results == ()
    assert data.consumed_orca_machine_paths == (upstream_machine,)
    assert write_workflow_html_report(tmp_path, payload) == tmp_path / "workflow_report.html"
    (tmp_path / WORKFLOW_SI_MD_FILE).write_text("# Supporting information\n", encoding="utf-8")

    workflow_machine = write_workflow_machine_observation(tmp_path, payload)

    assert workflow_machine == tmp_path / RUN_REPORT_JSON_FILE
    _validate_common_machine(workflow_machine)
    observation = json.loads(workflow_machine.read_text(encoding="utf-8"))
    upstream_observation = json.loads(upstream_machine.read_text(encoding="utf-8"))
    assert observation["payload"]["data"]["results"]["orca_results"] == []
    assert observation["lineage"]["upstream"] == [
        {
            "producer": upstream_observation["producer"],
            "operation_id": "orca_relaxed_scan",
            "byte_sha256": hashlib.sha256(upstream_machine.read_bytes()).hexdigest(),
        }
    ]
    assert not (generation / "job_report.html").exists()


def test_interaction_fanout_stages_stay_out_of_candidate_ranking(tmp_path: Path) -> None:
    conf_dir = _orca_stage_dir(tmp_path, "orca_conf", energy=-100.001, reason="normal_termination")
    complex_dir = _orca_stage_dir(
        tmp_path, "orca_int_complex", energy=-200.004, reason="normal_termination"
    )
    fragment_dir = _orca_stage_dir(
        tmp_path, "orca_int_frag", energy=-50.002, reason="normal_termination"
    )
    complex_stage = _orca_stage(
        "orca_int_complex", complex_dir, status="completed", label="complex_sp"
    )
    complex_stage["metadata"]["role"] = "interaction_complex_sp"
    fragment_stage = _orca_stage(
        "orca_int_frag", fragment_dir, status="completed", label="fragment_a"
    )
    fragment_stage["metadata"]["role"] = "interaction_fragment"
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_conf", conf_dir, status="completed", label="conf_01"),
            complex_stage,
            fragment_stage,
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    # Interaction fan-out single points stay in the stage chain and in
    # lineage, but their different-species/-level energies must neither rank
    # as candidates nor set the ΔE baseline.
    assert [row.stage_id for row in data.stage_rows] == [
        "orca_conf",
        "orca_int_complex",
        "orca_int_frag",
    ]
    assert [entry.label for entry in data.orca_results] == ["conf_01"]
    assert data.orca_results[0].energy == pytest.approx(-100.001)
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)
    assert set(data.consumed_orca_machine_paths) == {
        conf_dir / RUN_REPORT_JSON_FILE,
        complex_dir / RUN_REPORT_JSON_FILE,
        fragment_dir / RUN_REPORT_JSON_FILE,
    }
    assert write_workflow_html_report(tmp_path, payload) == tmp_path / "workflow_report.html"
    (tmp_path / WORKFLOW_SI_MD_FILE).write_text("# Supporting information\n", encoding="utf-8")

    workflow_machine = write_workflow_machine_observation(tmp_path, payload)

    assert workflow_machine == tmp_path / RUN_REPORT_JSON_FILE
    assert workflow_machine is not None
    _validate_common_machine(workflow_machine)
    observation = json.loads(workflow_machine.read_text(encoding="utf-8"))
    machine_results = observation["payload"]["data"]["results"]["orca_results"]
    assert [row["label"] for row in machine_results] == ["conf_01"]
    assert {row["operation_id"] for row in observation["lineage"]["upstream"]} == {
        "orca_conf",
        "orca_int_complex",
        "orca_int_frag",
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
    stage_dir, provenance = _orca_generation(job_dir)
    if engrad_energy is not None:
        (stage_dir / "opt.engrad").write_text(
            _ENGRAD_TEMPLATE.format(energy=engrad_energy), encoding="utf-8"
        )
    out_path = stage_dir / "opt.out"
    out_path.write_text(out_text, encoding="utf-8")
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


def test_collect_uses_final_orca_output_energy_when_engrad_is_absent(tmp_path: Path) -> None:
    payload = _energy_chain_payload(
        tmp_path,
        "orca_from_output",
        "|  1> ! r2scan-3c Opt TightSCF\n"
        "FINAL SINGLE POINT ENERGY -1.000000000000\n"
        "FINAL SINGLE POINT ENERGY -1.100000000000\n"
        "****ORCA TERMINATED NORMALLY****\n",
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].energy == pytest.approx(-1.1)
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)


def test_collect_refuses_annotated_final_output_energy(tmp_path: Path) -> None:
    # When the last final-energy line is annotated ("(SCF not fully
    # converged!)"), the fallback must publish no energy at all - an earlier
    # clean line belongs to a different geometry and must not set the dE
    # baseline.
    payload = _energy_chain_payload(
        tmp_path,
        "orca_annotated_output",
        "|  1> ! r2scan-3c Opt TightSCF\n"
        "FINAL SINGLE POINT ENERGY -1.000000000000\n"
        "FINAL SINGLE POINT ENERGY -1.100000000000 (SCF not fully converged!)\n"
        "****ORCA TERMINATED NORMALLY****\n",
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].energy is None
    assert data.orca_results[0].rel_kcal is None


def test_collect_refuses_engrad_energy_when_final_output_is_annotated(tmp_path: Path) -> None:
    # The .engrad is the primary energy channel, but it carries the same
    # unconverged SCF value and cannot be cross-checked on its own - the
    # annotation exists only in the .out. A stage whose final output line is
    # annotated publishes no energy even when its .engrad is retained.
    payload = _energy_chain_payload(
        tmp_path,
        "orca_annotated_engrad",
        "|  1> ! r2scan-3c Opt TightSCF\n"
        "FINAL SINGLE POINT ENERGY -1.000000000000\n"
        "FINAL SINGLE POINT ENERGY -1.100000000000 (SCF not fully converged!)\n"
        "****ORCA TERMINATED NORMALLY****\n",
        engrad_energy="-1.050000000000",
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].energy is None
    assert data.orca_results[0].rel_kcal is None


def test_collect_refuses_engrad_energy_when_annotation_is_beyond_scan_window(
    tmp_path: Path,
) -> None:
    # Freq-bearing outputs routinely print hundreds of KiB of modes after the
    # last final-energy line. The annotation must still refuse the retained
    # .engrad when that line lies beyond the first backward scan window.
    payload = _energy_chain_payload(
        tmp_path,
        "orca_annotated_beyond_window",
        "|  1> ! r2scan-3c Opt Freq TightSCF\n"
        "FINAL SINGLE POINT ENERGY -1.000000000000\n"
        "FINAL SINGLE POINT ENERGY -1.100000000000 (SCF not fully converged!)\n"
        + "x" * (workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES + 4096)
        + "\n****ORCA TERMINATED NORMALLY****\n",
        engrad_energy="-1.050000000000",
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].energy is None
    assert data.orca_results[0].rel_kcal is None


def test_collect_finds_clean_output_energy_beyond_scan_window(tmp_path: Path) -> None:
    # A clean final-energy line beyond the first window must still publish:
    # the backward scan may not trade the blind spot for over-refusal.
    payload = _energy_chain_payload(
        tmp_path,
        "orca_clean_beyond_window",
        "|  1> ! r2scan-3c Opt Freq TightSCF\n"
        "FINAL SINGLE POINT ENERGY -1.000000000000\n"
        "FINAL SINGLE POINT ENERGY -1.100000000000\n"
        + "x" * (workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES + 4096)
        + "\n****ORCA TERMINATED NORMALLY****\n",
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].energy == pytest.approx(-1.1)
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)


def test_collect_ignores_unbound_root_engrad_for_verified_orca_generation(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "orca_reused_root"
    generation = _orca_stage_dir(
        tmp_path,
        "orca_reused_root",
        energy=-100.25,
        reason="normal_termination",
    )
    (job_dir / "unbound.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-999.000000000000"),
        encoding="utf-8",
    )
    stage = _orca_stage(
        "orca_current_generation",
        generation,
        status="completed",
        label="current",
    )
    for artifact in stage["output_artifacts"]:
        if artifact["kind"] == "orca_output_dir":
            artifact["path"] = str(job_dir)

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].energy == pytest.approx(-100.25)
    assert data.orca_results[0].energy != pytest.approx(-999.0)


@pytest.mark.parametrize(
    ("stage_job_id", "stage_run_id", "report_job_id", "report_run_id"),
    (
        ("job-new", "run-shared", "job-old", "run-shared"),
        ("job-shared", "run-new", "job-shared", "run-old"),
    ),
)
def test_orca_stage_report_rejects_conflicting_partial_identity(
    tmp_path: Path,
    stage_job_id: str,
    stage_run_id: str,
    report_job_id: str,
    report_run_id: str,
) -> None:
    job_dir = tmp_path / f"orca_identity_{stage_job_id}_{stage_run_id}"
    job_dir.mkdir()
    report_path = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": report_job_id},
            "engine_payload": {
                "run_id": report_run_id,
                "attempts": [{"index": 1}],
                "final_result": {"reason": "stale_generation_reason"},
            },
        },
    )
    stage = {
        "stage_id": "orca_identity_conflict",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {
            "engine": "orca",
            "submission_result": {"job_id": stage_job_id},
            "payload": {"run_id": stage_run_id},
        },
        "metadata": {
            "child_job_id": stage_job_id,
            "run_id": stage_run_id,
            "latest_known_path": str(job_dir),
        },
        "output_artifacts": [
            {"kind": "orca_report_json", "path": str(report_path)},
        ],
    }

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].reason == ""
    assert data.orca_results[0].attempt_count == 0
    assert data.orca_results[0].energy is None
    assert data.orca_results[0].report_href is None


def test_orca_stage_report_requires_declared_run_identity(tmp_path: Path) -> None:
    job_dir = tmp_path / "orca_missing_run"
    job_dir.mkdir()
    report_path = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": "job-current"},
            "engine_payload": {
                "attempts": [{"index": 1}],
                "final_result": {"reason": "incomplete_identity_reason"},
            },
        },
    )
    metadata = {
        "child_job_id": "job-current",
        "run_id": "run-current",
        "latest_known_path": str(job_dir),
    }
    stage = {
        "stage_id": "orca_missing_run",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {"engine": "orca", "submission_result": {"job_id": "job-current"}},
        "metadata": metadata,
        "output_artifacts": [{"kind": "orca_report_json", "path": str(report_path)}],
    }

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].reason == ""
    assert data.orca_results[0].attempt_count == 0
    assert data.orca_results[0].report_href is None


def test_orca_stage_report_allows_writer_without_queue_identity(tmp_path: Path) -> None:
    job_dir = tmp_path / "orca_queue_backed"
    job_dir.mkdir()
    report_path = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": "job-current", "queue_id": ""},
            "engine_payload": {
                "run_id": "run-current",
                "attempts": [{"index": 1}],
                "final_result": {"reason": "normal_termination"},
            },
        },
    )
    stage = {
        "stage_id": "orca_queue_backed",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {
            "engine": "orca",
            "submission_result": {"job_id": "job-current", "queue_id": "queue-current"},
            "payload": {"run_id": "run-current"},
        },
        "metadata": {
            "child_job_id": "job-current",
            "run_id": "run-current",
            "queue_id": "queue-current",
            "latest_known_path": str(job_dir),
        },
        "output_artifacts": [{"kind": "orca_report_json", "path": str(report_path)}],
    }

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].reason == "normal_termination"
    assert data.orca_results[0].attempt_count == 1


def test_completed_orca_stage_rejects_explicit_root_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "orca_root_explicit"
    job_dir.mkdir()
    report_path = job_dir / "job_report.json"
    report_path.write_text(
        json.dumps(
            {
                "job": {"id": "orca_root_explicit"},
                "engine_payload": {
                    "attempts": [{"index": 1}],
                    "final_result": {"reason": "root_report_reason"},
                },
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_root_explicit",
                "stage_kind": "orca_stage",
                "status": "completed",
                "metadata": {
                    "child_job_id": "orca_root_explicit",
                    "latest_known_path": str(job_dir),
                    "selected_input_label": "root",
                },
                "output_artifacts": [
                    {"kind": "orca_output_dir", "path": str(job_dir)},
                    {"kind": "orca_report_json", "path": str(report_path)},
                ],
            }
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].reason == ""
    assert data.orca_results[0].attempt_count == 0
    assert data.orca_results[0].report_href is None


def test_completed_orca_stage_rejects_noncanonical_generation_json(tmp_path: Path) -> None:
    job_dir = tmp_path / "orca_noncanonical_json"
    job_dir.mkdir()
    canonical = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": "orca_noncanonical_json"},
            "engine_payload": {
                "run_id": "run-noncanonical-json",
                "attempts": [{"index": 1}],
                "final_result": {"reason": "wrong_filename_reason"},
            },
        },
    )
    planted = canonical.with_name("other.json")
    planted_payload = json.loads(canonical.read_text(encoding="utf-8"))
    planted_payload["payload"]["data"]["summary"]["reason"] = "planted_reason"
    planted.write_text(json.dumps(planted_payload), encoding="utf-8")
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_noncanonical_json",
                "stage_kind": "orca_stage",
                "status": "completed",
                "metadata": {
                    "child_job_id": "orca_noncanonical_json",
                    "run_id": "run-noncanonical-json",
                    "latest_known_path": str(job_dir),
                },
                "output_artifacts": [
                    {"kind": "orca_report_json", "path": str(planted)},
                ],
            }
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    # The planted path is ignored; the verified canonical fallback remains usable.
    assert data.orca_results[0].reason == "wrong_filename_reason"
    assert data.orca_results[0].attempt_count == 1


def test_orca_output_energy_reads_only_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = tmp_path / "orca_large_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_bytes(
        b"FINAL SINGLE POINT ENERGY -9.000000000000\n"
        + b"x" * (workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES + 4096)
        + b"\nFINAL SINGLE POINT ENERGY -2.500000000000\n"
    )
    bytes_requested = 0
    original_pread = workflow_report.os.pread

    def tracked_pread(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal bytes_requested
        bytes_requested += count
        return original_pread(descriptor, count, offset)

    monkeypatch.setattr(workflow_report.os, "pread", tracked_pread)

    _annotated, energy = workflow_report._orca_report_output_energy_state(
        stage_dir, _orca_output_report(out_path)
    )

    assert out_path.stat().st_size > workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES
    assert bytes_requested == workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES
    assert energy == pytest.approx(-2.5)


def test_orca_output_energy_sees_annotated_line_cut_at_window_start(tmp_path: Path) -> None:
    # A line whose first byte lands exactly on a window's start byte is
    # skipped there as possibly truncated; the next window's overlap must
    # re-read it whole and still report the annotation.
    stage_dir = tmp_path / "orca_window_boundary"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    prefix = b"|  1> ! r2scan-3c Opt Freq TightSCF\n"
    annotated_line = b"FINAL SINGLE POINT ENERGY -1.100000000000 (SCF not fully converged!)\n"
    out_path.write_bytes(
        prefix
        + annotated_line
        + b"x" * (workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES - len(annotated_line))
    )
    assert out_path.stat().st_size - workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES == len(prefix)

    annotated_state, energy = workflow_report._orca_report_output_energy_state(
        stage_dir, _orca_output_report(out_path)
    )

    assert annotated_state is True
    assert energy is None


def test_orca_output_energy_skips_false_match_at_mid_line_window_start(tmp_path: Path) -> None:
    # A window can begin mid-line. When the cut lands right before an
    # energy-line echo embedded in a longer line, the buffer-position-0
    # match is a complete-looking impostor the full file never matches;
    # the skip rule must reject it so the true line's value publishes.
    stage_dir = tmp_path / "orca_mid_line_cut"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    real_line = b"FINAL SINGLE POINT ENERGY -1.100000000000\n"
    echo_head = b"| 27> "
    fake_tail = b"FINAL SINGLE POINT ENERGY -9.900000000000\n"
    out_path.write_bytes(
        real_line
        + echo_head
        + fake_tail
        + b"x" * (workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES - len(fake_tail))
    )
    assert out_path.stat().st_size - workflow_report._ORCA_ENERGY_SCAN_WINDOW_BYTES == len(
        real_line
    ) + len(echo_head)

    annotated_state, energy = workflow_report._orca_report_output_energy_state(
        stage_dir, _orca_output_report(out_path)
    )

    assert annotated_state is False
    assert energy == pytest.approx(-1.1)


@pytest.mark.parametrize("final_state", ("missing", "no_energy_line"))
def test_orca_output_energy_refuses_earlier_attempt_for_recorded_final(
    tmp_path: Path, final_state: str
) -> None:
    # A recorded final output is authoritative. The verified report
    # resolution already rejects a report whose bound final output is
    # missing, so this chain sees that shape only in the window between
    # verification and the scan — and an earlier attempt's clean value must
    # not stand in for the final geometry there, nor when the final is
    # readable but prints no final energy line.
    stage_dir = tmp_path / "orca_final_authority"
    stage_dir.mkdir()
    attempt_out = stage_dir / "attempt1.out"
    attempt_out.write_text("FINAL SINGLE POINT ENERGY -1.000000000000\n", encoding="utf-8")
    final_out = stage_dir / "final.out"
    if final_state == "no_energy_line":
        final_out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    payload = {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(attempt_out)}],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(final_out),
            },
        }
    }

    annotated_state, energy = workflow_report._orca_report_output_energy_state(stage_dir, payload)

    assert annotated_state is False
    assert energy is None


def test_orca_output_energy_keeps_attempt_annotation_evidence_for_recorded_final(
    tmp_path: Path,
) -> None:
    # The conservative edge stays: with the recorded final unreadable, an
    # annotated earlier attempt still taints the chain, so the retained
    # engrad is refused rather than published unverifiable.
    stage_dir = tmp_path / "orca_final_authority_annotated"
    stage_dir.mkdir()
    attempt_out = stage_dir / "attempt1.out"
    attempt_out.write_text(
        "FINAL SINGLE POINT ENERGY -1.000000000000 (SCF not fully converged!)\n",
        encoding="utf-8",
    )
    payload = {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(attempt_out)}],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(stage_dir / "vanished.out"),
            },
        }
    }

    annotated_state, energy = workflow_report._orca_report_output_energy_state(stage_dir, payload)

    assert annotated_state is True
    assert energy is None


def test_orca_output_energy_scans_attempts_when_no_final_was_recorded(tmp_path: Path) -> None:
    # Records that never captured a final output path keep the attempt scan,
    # exactly like the per-job rule.
    stage_dir = tmp_path / "orca_never_recorded"
    stage_dir.mkdir()
    attempt_out = stage_dir / "attempt1.out"
    attempt_out.write_text("FINAL SINGLE POINT ENERGY -1.000000000000\n", encoding="utf-8")
    payload = {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(attempt_out)}],
            "final_result": {"reason": "normal_termination"},
        }
    }

    annotated_state, energy = workflow_report._orca_report_output_energy_state(stage_dir, payload)

    assert annotated_state is False
    assert energy == pytest.approx(-1.0)


def test_orca_output_energy_rejects_file_changed_during_tail_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = tmp_path / "orca_changing_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_text(
        "FINAL SINGLE POINT ENERGY -1.100000000000\n",
        encoding="utf-8",
    )
    original_pread = workflow_report.os.pread
    changed = False

    def mutating_pread(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal changed
        chunk = original_pread(descriptor, count, offset)
        if not changed:
            changed = True
            with out_path.open("ab") as handle:
                handle.write(b"changed\n")
        return chunk

    monkeypatch.setattr(workflow_report.os, "pread", mutating_pread)

    assert workflow_report._orca_report_output_energy_state(
        stage_dir, _orca_output_report(out_path)
    ) == (False, None)


def test_orca_output_energy_rejects_nonregular_multilink_or_unconfined_paths(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "orca_untrusted_output"
    stage_dir.mkdir()
    target = stage_dir / "target.out"
    target.write_text(
        "FINAL SINGLE POINT ENERGY -1.100000000000\n",
        encoding="utf-8",
    )
    symlink = stage_dir / "symlink.out"
    symlink.symlink_to(target.name)
    hardlink = stage_dir / "hardlink.out"
    os.link(target, hardlink)
    fifo = stage_dir / "fifo.out"
    os.mkfifo(fifo)
    outside = tmp_path / "outside.out"
    outside.write_text(
        "FINAL SINGLE POINT ENERGY -2.200000000000\n",
        encoding="utf-8",
    )

    for candidate in (symlink, hardlink, fifo, outside):
        assert workflow_report._orca_report_output_energy_state(
            stage_dir, _orca_output_report(candidate)
        ) == (False, None)


def test_write_workflow_html_report_renders_sections(tmp_path: Path) -> None:
    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="ts_criteria_met")
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_optts_freq_01", stage_a, status="completed", label="ts_guess_a"),
            _orca_stage("orca_optts_freq_02", stage_b, status="completed", label="ts_guess_b"),
        ],
    )
    payload["template_name"] = "reaction_ts_search"

    path = write_workflow_html_report(tmp_path, payload)

    assert path == tmp_path / "workflow_report.html"
    text = path.read_text(encoding="utf-8")
    assert "workflow report" in text
    assert "TS candidates" in text
    assert "Stage chain" in text
    assert "ts_guess_a" in text
    assert 'href="orca_b' in text
    assert "<circle" in text
    assert "<polyline" not in text
    assert "kcal mol⁻¹" in text
    assert "total wall time" in text


def test_failed_stage_energy_excluded_from_ranking_baseline(tmp_path: Path) -> None:
    # The failed stage's .engrad holds a lower transient energy; it must not
    # become the ΔE reference nor outrank the completed candidate.
    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="geometry_zero_distance")
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_conformer_01", stage_a, status="completed", label="conf_ok"),
            _orca_stage("orca_conformer_02", stage_b, status="failed", label="conf_failed"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.label for entry in data.orca_results] == ["conf_ok", "conf_failed"]
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)
    assert data.orca_results[1].rel_kcal is None
    assert data.orca_results[1].energy == pytest.approx(-100.005)


def test_write_workflow_html_report_handles_empty_payload(tmp_path: Path) -> None:
    path = write_workflow_html_report(tmp_path, {"workflow_id": "wf_empty"})

    assert path == tmp_path / "workflow_report.html"
    assert "wf_empty" in path.read_text(encoding="utf-8")


def test_failed_crest_topology_change_is_explained_in_workflow_report(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "01_crest" / "crest_reactant_01"
    job_dir.mkdir(parents=True)
    (job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "crest",
                "job": {"id": "crest-current"},
                "status": {
                    "state": "failed",
                    "reason": "crest_exit_code_156",
                    "exit_code": 156,
                },
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "crest.stdout.log").write_text(
        "\n".join(
            [
                "*WARNING* Change in topology detected!",
                "Topology change compared to the input affects atoms:",
                "21(P) 35(O) 42(C)",
                "A topology change was seen in the initial geometry optimization.",
                "Safety termination of CREST.",
            ]
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_reactant_01",
                "stage_kind": "crest_stage",
                "status": "failed",
                "task": {"engine": "crest", "status": "failed", "payload": {}},
                "metadata": {
                    "input_role": "reactant",
                    "child_job_id": "crest-current",
                    "latest_known_path": str(job_dir),
                },
            },
            {
                "stage_id": "crest_product_01",
                "stage_kind": "crest_stage",
                "status": "cancelled",
                "task": {
                    "engine": "crest",
                    "status": "cancelled",
                    "payload": {},
                    "cancel_result": {"reason": "cancel_requested"},
                },
                "metadata": {"input_role": "product"},
            },
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert len(data.failure_rows) == 1
    assert data.failure_rows[0].reason == "crest_exit_code_156"
    assert "21(P) 35(O) 42(C)" in data.failure_rows[0].explanation
    assert data.failure_rows[0].details_href == ("01_crest/crest_reactant_01/crest.stdout.log")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Why it failed" in text
    assert "changed molecular topology" in text
    assert "crest_exit_code_156" in text
    assert "crest.noreftopo: true" in text
    assert "can retain artifacts" in text
    assert 'href="01_crest/crest_reactant_01/crest.stdout.log"' in text
    assert "crest_product_01" in text
    assert "cancel_requested" in text


def test_restarted_stage_does_not_show_stale_failure_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "01_crest" / "crest_reactant_01"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "crest-old"},
                "status": {"state": "failed", "reason": "crest_exit_code_156"},
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "crest.stdout.log").write_text(
        "Change in topology detected!\n"
        "A topology change was seen in the initial geometry optimization.\n",
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_reactant_01",
                "stage_kind": "crest_stage",
                "status": "queued",
                "task": {"engine": "crest", "status": "submitted", "payload": {}},
                "metadata": {
                    "child_job_id": "crest-new",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "running"

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert data.failure_rows == ()
    assert data.stage_rows[0].detail == ""
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Why it failed" not in text
    assert "crest_exit_code_156" not in text
    assert "changed molecular topology" not in text


@pytest.mark.parametrize(
    ("engine", "stage_kind", "stage_parent"),
    (("xtb", "xtb_stage", "02_xtb"), ("crest", "crest_stage", "01_crest")),
)
def test_internal_stage_does_not_fall_back_to_report(
    tmp_path: Path,
    engine: str,
    stage_kind: str,
    stage_parent: str,
) -> None:
    child_job_id = f"{engine}-current"
    job_dir = tmp_path / stage_parent / f"{engine}_report_only"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": child_job_id},
                "status": {"state": "failed", "reason": "retired_report_reason"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": f"{engine}_report_only",
                "stage_kind": stage_kind,
                "status": "failed",
                "task": {"engine": engine, "status": "failed", "payload": {}},
                "metadata": {
                    "child_job_id": child_job_id,
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == ""
    assert data.failure_rows[0].details_href is None


@pytest.mark.parametrize("task_engine", ("", "orca"))
def test_internal_stage_kind_never_falls_back_when_task_engine_is_invalid(
    tmp_path: Path,
    task_engine: str,
) -> None:
    job_dir = tmp_path / "02_xtb" / "xtb_invalid_task_engine"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "xtb-current"},
                "status": {"state": "failed", "reason": "retired_report_reason"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "xtb_invalid_task_engine",
                "stage_kind": "xtb_stage",
                "status": "failed",
                "task": {"engine": task_engine, "status": "failed", "payload": {}},
                "metadata": {
                    "child_job_id": "xtb-current",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == ""
    assert data.failure_rows[0].details_href is None


def test_internal_stage_rejects_foreign_engine_state(tmp_path: Path) -> None:
    job_dir = tmp_path / "02_xtb" / "xtb_foreign_state"
    job_dir.mkdir(parents=True)
    (job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "crest",
                "job": {"id": "xtb-current"},
                "status": {"state": "failed", "reason": "foreign_engine_reason"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "xtb_foreign_state",
                "stage_kind": "xtb_stage",
                "status": "failed",
                "task": {"engine": "xtb", "status": "failed", "payload": {}},
                "metadata": {
                    "child_job_id": "xtb-current",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == ""
    assert data.failure_rows[0].details_href is None


def test_failed_stage_without_current_identity_does_not_use_old_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "03_orca" / "orca_submission_failed"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "orca-old"},
                "status": {"state": "failed", "reason": "old_runner_error"},
                "engine_payload": {"run_id": "run-old"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_submission_failed",
                "stage_kind": "orca_stage",
                "status": "submission_failed",
                "task": {"engine": "orca", "status": "submission_failed", "payload": {}},
                "metadata": {"latest_known_path": str(job_dir)},
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert data.failure_rows[0].reason == ""
    assert data.failure_rows[0].details_href is None
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "old_runner_error" not in text
    assert "job_report.json" not in text


def test_stage_report_identity_ignores_legacy_flat_identity_keys(tmp_path: Path) -> None:
    job_dir = tmp_path / "02_xtb" / "xtb_attempt"
    job_dir.mkdir(parents=True)
    # Flat top-level identity keys belong to a pre-schema artifact layout. They
    # carry conflicting values here on purpose: the matcher must read only the
    # nested identities, so these are ignored rather than poisoning the
    # conflict guard and rejecting a state that plainly matches the stage.
    (job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "xtb",
                "job": {"id": "xtb-current", "task_id": "xtb-current"},
                "job_id": "xtb-foreign",
                "run_id": "run-foreign",
                "queue_id": "q-foreign",
                "engine_payload": {"job_id": "xtb-foreign", "queue_id": "q-foreign"},
                "status": {"state": "failed", "reason": "xtb_failure_with_legacy_keys"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "xtb_path",
                "stage_kind": "xtb_stage",
                "status": "failed",
                "task": {
                    "engine": "xtb",
                    "status": "failed",
                    "payload": {"job_dir": str(job_dir)},
                },
                "metadata": {"child_job_id": "xtb-current"},
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "xtb_failure_with_legacy_keys"
    assert data.failure_rows[0].details_href == "02_xtb/xtb_attempt/job_state.json"


def test_xtb_retry_prefers_refreshed_latest_path_over_original_task_job_dir(
    tmp_path: Path,
) -> None:
    old_job_dir = tmp_path / "02_xtb" / "xtb_old_attempt"
    old_job_dir.mkdir(parents=True)
    (old_job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "xtb",
                "job": {"id": "xtb-current"},
                "status": {"state": "failed", "reason": "old_xtb_failure"},
            }
        ),
        encoding="utf-8",
    )
    current_job_dir = tmp_path / "02_xtb" / "xtb_retry_attempt"
    current_job_dir.mkdir(parents=True)
    (current_job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "xtb",
                "job": {"id": "xtb-current", "queue_id": "xtb-q-current"},
                "status": {"state": "failed", "reason": "current_xtb_failure"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "xtb_path_retry",
                "stage_kind": "xtb_stage",
                "status": "submission_failed",
                "task": {
                    "engine": "xtb",
                    "status": "submission_failed",
                    "payload": {"job_dir": str(old_job_dir)},
                    "submission_result": {"queue_id": "xtb-q-current"},
                },
                "metadata": {
                    "child_job_id": "xtb-current",
                    "latest_known_path": str(current_job_dir),
                    "queue_id": "xtb-q-current",
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "current_xtb_failure"
    assert data.failure_rows[0].details_href == "02_xtb/xtb_retry_attempt/job_state.json"


def test_stage_state_prefers_latest_path_over_original_task_job_dir(
    tmp_path: Path,
) -> None:
    stale_job_dir = tmp_path / "01_crest" / "crest_stale"
    stale_job_dir.mkdir(parents=True)
    (stale_job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "crest",
                "job": {"id": "crest-current"},
                "status": {"state": "failed", "reason": "stale_crest_failure"},
            }
        ),
        encoding="utf-8",
    )
    current_job_dir = tmp_path / "01_crest" / "crest_current"
    current_job_dir.mkdir(parents=True)
    (current_job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "crest",
                "job": {"id": "crest-current"},
                "status": {"state": "failed", "reason": "current_crest_failure"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_repaired",
                "stage_kind": "crest_stage",
                "status": "failed",
                "task": {
                    "engine": "crest",
                    "status": "failed",
                    "payload": {"job_dir": str(stale_job_dir)},
                },
                "metadata": {
                    "child_job_id": "crest-current",
                    "latest_known_path": str(current_job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "current_crest_failure"
    assert data.failure_rows[0].details_href == "01_crest/crest_current/job_state.json"


def test_orca_run_identity_allows_current_report_diagnostic(tmp_path: Path) -> None:
    job_dir = tmp_path / "03_orca" / "orca_current"
    job_dir.mkdir(parents=True)
    report_path = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": "orca-child"},
            "status": {"state": "failed", "reason": "runner_exception"},
            "engine_payload": {"run_id": "run-current"},
        },
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_current",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {"engine": "orca", "status": "failed", "payload": {}},
                "metadata": {
                    "child_job_id": "orca-child",
                    "run_id": "run-current",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "runner_exception"
    assert data.failure_rows[0].details_href == os.path.relpath(report_path, tmp_path)


def test_orca_stage_report_requires_declared_job_identity(tmp_path: Path) -> None:
    job_dir = tmp_path / "orca_missing_job"
    job_dir.mkdir()
    report_path = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": "job-current"},
            "engine_payload": {
                "run_id": "run-current",
                "attempts": [{"index": 1}],
                "final_result": {"reason": "incomplete_identity_reason"},
            },
        },
    )
    stage = {
        "stage_id": "orca_missing_job",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {"engine": "orca", "payload": {"run_id": "run-current"}},
        "metadata": {
            "run_id": "run-current",
            "latest_known_path": str(job_dir),
        },
        "output_artifacts": [{"kind": "orca_report_json", "path": str(report_path)}],
    }

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].reason == ""
    assert data.orca_results[0].attempt_count == 0
    assert data.orca_results[0].report_href is None


def test_orca_diagnostic_falls_back_to_report_json_without_html(tmp_path: Path) -> None:
    job_dir = tmp_path / "03_orca" / "orca_json_fallback"
    job_dir.mkdir(parents=True)
    report_path = _write_orca_generation_report(
        job_dir,
        {
            "job": {"id": "orca-child"},
            "status": {"state": "failed", "reason": "runner_exception"},
            "engine_payload": {"run_id": "run-current"},
        },
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_current",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {"engine": "orca", "status": "failed", "payload": {}},
                "metadata": {
                    "child_job_id": "orca-child",
                    "run_id": "run-current",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].details_href == os.path.relpath(report_path, tmp_path)


def test_orca_current_identity_root_report_is_not_a_diagnostic_source(tmp_path: Path) -> None:
    job_dir = tmp_path / "03_orca" / "orca_root_report"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "orca-child"},
                "status": {"state": "failed", "reason": "root_report_reason"},
                "engine_payload": {"run_id": "run-current"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_root_report",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {"engine": "orca", "status": "failed", "payload": {}},
                "metadata": {
                    "run_id": "run-current",
                    "child_job_id": "orca-child",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == ""
    assert data.failure_rows[0].details_href is None


def test_workflow_error_message_is_primary_and_escaped(tmp_path: Path) -> None:
    payload = _payload(tmp_path, [])
    payload["status"] = "failed"
    payload["metadata"]["workflow_error"] = {
        "status": "failed",
        "reason": "no_endpoint_pairs",
        "message": "No pair passed <endpoint> filters.",
        "scope": "reaction_ts_search_endpoint_pairing",
        "stage_id": "crest_pair_01",
    }

    path = write_workflow_html_report(tmp_path, payload)

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "No pair passed &lt;endpoint&gt; filters." in text
    assert "code: no_endpoint_pairs" in text
    assert "stage: crest_pair_01" in text
    assert "scope: reaction_ts_search_endpoint_pairing" in text
    assert "No pair passed <endpoint> filters." not in text


def test_nonfatal_stage_failure_has_no_workflow_failure_verdict(tmp_path: Path) -> None:
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_candidate_bad",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "status": "failed", "payload": {}},
                "metadata": {"reason": "ts_criteria_failed"},
            }
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert data.failure_rows[0].status == "failed"
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Stage failures" in text
    assert "Why it failed" not in text


def test_energy_axis_tick_labels_stay_exact_for_quarter_steps() -> None:
    # All candidates within 1 kcal/mol → 0.25-wide ticks; one-decimal labels
    # would render the 0.25 tick as "0.2".
    ticks = _energy_axis_ticks(1.0)

    assert ticks == (0.0, 0.25, 0.5, 0.75, 1.0)
    step = ticks[1] - ticks[0]
    assert [_tick_label(tick, step) for tick in ticks] == ["0", "0.25", "0.50", "0.75", "1"]
    assert _tick_label(2.5, 2.5) == "2.5"
