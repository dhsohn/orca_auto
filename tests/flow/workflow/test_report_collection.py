from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from orca_auto.core.artifacts import (
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
    WORKFLOW_SI_MD_FILE,
)
from orca_auto.core.engine_runner import confined_output_identity
from orca_auto.flow.workflow import report_collection as workflow_report_collection
from orca_auto.flow.workflow import report_energy_evidence
from orca_auto.flow.workflow.machine import write_workflow_machine_observation
from orca_auto.flow.workflow.report_collection import collect_workflow_report_data
from orca_auto.flow.workflow.report_rendering import write_workflow_html_report
from orca_auto.flow.workflow.stage_summary import count_xyz_frames
from orca_auto.orca.parser import KCAL_PER_HARTREE
from tests.flow.workflow_report_helpers import (
    _ENGRAD_TEMPLATE,
    _energy_chain_payload,
    _orca_stage,
    _orca_stage_dir,
    _payload,
    _publish_orca_machine,
    _validate_common_machine,
    _write_multi_xyz,
    _write_orca_generation_report,
)


def test_count_xyz_frames(tmp_path: Path) -> None:
    xyz = tmp_path / "crest_conformers.xyz"
    _write_multi_xyz(xyz, frames=4)
    assert count_xyz_frames(xyz) == 4


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
    assert data.orca_results[0].imaginary_count is None
    assert data.orca_results[0].report_href is not None
    assert "orca_b" in data.orca_results[0].report_href


def test_single_completed_candidate_without_science_identity_has_no_relative_energy() -> None:
    candidate = workflow_report_collection.OrcaStageResult(
        stage_id="orca_unbound",
        label="unbound",
        status="completed",
        reason="",
        energy=-100.0,
        rel_kcal=None,
        imaginary_count=0,
        attempt_count=1,
        report_href=None,
        science_identity=None,
    )

    assert workflow_report_collection._with_relative_energies([candidate]) == (candidate,)


def test_workflow_report_ignores_resource_only_directive_differences(
    tmp_path: Path,
) -> None:
    first = _orca_stage_dir(
        tmp_path,
        "orca_resource_first",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF Opt",
        extra_directives="%pal\n  nprocs 4\nend\n%maxcore 2048",
    )
    second = _orca_stage_dir(
        tmp_path,
        "orca_resource_second",
        energy=-100.005,
        reason="normal_termination",
        route_line="! hf opt",
        extra_directives="%pal nprocs 16 end\n%maxcore 8192",
    )
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_resource_first", first, status="completed", label="first"),
            _orca_stage("orca_resource_second", second, status="completed", label="second"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.energy for entry in data.orca_results] == pytest.approx([-100.005, -100.001])
    assert [entry.rel_kcal for entry in data.orca_results] == pytest.approx(
        [0.0, (-100.001 + 100.005) * KCAL_PER_HARTREE]
    )


def test_workflow_report_ignores_pal_route_resource_shorthand_differences(
    tmp_path: Path,
) -> None:
    first = _orca_stage_dir(
        tmp_path,
        "orca_pal_first",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF Opt PAL4",
    )
    second = _orca_stage_dir(
        tmp_path,
        "orca_pal_second",
        energy=-100.005,
        reason="normal_termination",
        route_line="! HF Opt PAL8",
    )
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_pal_first", first, status="completed", label="first"),
            _orca_stage("orca_pal_second", second, status="completed", label="second"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.energy for entry in data.orca_results] == pytest.approx([-100.005, -100.001])
    assert [entry.rel_kcal for entry in data.orca_results] == pytest.approx(
        [0.0, (-100.001 + 100.005) * KCAL_PER_HARTREE]
    )


@pytest.mark.parametrize("xyzfile", [False, True])
def test_workflow_report_omits_relative_energies_for_mixed_atom_sequences(
    tmp_path: Path,
    xyzfile: bool,
) -> None:
    first = _orca_stage_dir(
        tmp_path,
        "orca_atoms_first",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF Opt",
        atom_label="H",
        xyzfile=xyzfile,
    )
    second = _orca_stage_dir(
        tmp_path,
        "orca_atoms_second",
        energy=-100.005,
        reason="normal_termination",
        route_line="! HF Opt",
        atom_label="He",
        xyzfile=xyzfile,
    )
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_atoms_first", first, status="completed", label="first"),
            _orca_stage("orca_atoms_second", second, status="completed", label="second"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.energy for entry in data.orca_results] == pytest.approx([-100.001, -100.005])
    assert all(entry.rel_kcal is None for entry in data.orca_results)


def test_workflow_report_does_not_hide_primary_task_kind_with_spoofed_interaction_role(
    tmp_path: Path,
) -> None:
    stage_dir = _orca_stage_dir(
        tmp_path,
        "orca_spoofed_interaction_role",
        energy=-100.001,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_spoofed_interaction_role",
        stage_dir,
        status="completed",
        label="primary candidate",
    )
    stage["task"] = {"engine": "orca", "task_kind": "opt", "status": "completed"}
    stage["metadata"].update({"role": "interaction_fragment", "parent_stage_id": "orca_parent"})

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert [entry.label for entry in data.orca_results] == ["primary candidate"]


@pytest.mark.parametrize(
    ("engine", "task_kind"),
    (("xtb", "opt"), ("orca", "unknown"), ("orca", "sp"), ("orca", "relaxed_scan")),
)
def test_workflow_report_candidate_admission_requires_exact_stationary_orca_task(
    tmp_path: Path,
    engine: str,
    task_kind: str,
) -> None:
    stage_dir = _orca_stage_dir(
        tmp_path,
        f"orca_candidate_{engine}_{task_kind}",
        energy=-100.001,
        reason="normal_termination",
    )
    stage = _orca_stage(
        f"orca_candidate_{engine}_{task_kind}",
        stage_dir,
        status="completed",
        label="non-candidate",
    )
    stage["task"] = {"engine": engine, "task_kind": task_kind}

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()


def test_workflow_report_rejects_stationary_task_with_single_point_route(
    tmp_path: Path,
) -> None:
    stage_dir = _orca_stage_dir(
        tmp_path,
        "orca_route_role_mismatch",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF SP",
    )
    stage = _orca_stage(
        "orca_route_role_mismatch",
        stage_dir,
        status="completed",
        label="route mismatch",
    )

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()


def test_workflow_report_accepts_authoritative_optts_frequency_candidate(
    tmp_path: Path,
) -> None:
    stage_dir = _orca_stage_dir(
        tmp_path,
        "orca_optts_freq",
        energy=-100.125,
        reason="normal_termination",
        route_line="! HF OptTS Freq",
    )
    stage = _orca_stage(
        "orca_optts_freq",
        stage_dir,
        status="completed",
        label="transition state",
        task_kind="optts_freq",
    )

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert [entry.energy for entry in data.orca_results] == pytest.approx([-100.125])


def test_workflow_report_rejects_completed_stage_with_failed_task_state(
    tmp_path: Path,
) -> None:
    stage_dir = _orca_stage_dir(
        tmp_path,
        "orca_task_failed",
        energy=-100.125,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_task_failed",
        stage_dir,
        status="completed",
        label="contradictory",
    )
    stage["task"]["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()
    assert len(data.failure_rows) == 1
    assert "stage/task is not durably completed" in data.stage_rows[0].detail


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
    conformer_stage = _orca_stage("orca_conf", conf_dir, status="completed", label="conf_01")
    conformer_stage["task"] = {"engine": "orca", "task_kind": "opt", "status": "completed"}
    complex_stage = _orca_stage(
        "orca_int_complex", complex_dir, status="completed", label="complex_sp"
    )
    complex_stage["task"] = {"engine": "orca", "task_kind": "sp", "status": "completed"}
    complex_stage["metadata"].update(
        {"role": "interaction_complex_sp", "parent_stage_id": "orca_conf"}
    )
    fragment_stage = _orca_stage(
        "orca_int_frag", fragment_dir, status="completed", label="fragment_a"
    )
    fragment_stage["task"] = {"engine": "orca", "task_kind": "sp", "status": "completed"}
    fragment_stage["metadata"].update(
        {
            "role": "interaction_fragment",
            "parent_stage_id": "orca_conf",
            "fragment_index": 0,
        }
    )
    payload = _payload(
        tmp_path,
        [
            conformer_stage,
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
    # converged!)"), the fallback must publish no energy at all — an earlier
    # clean line belongs to a different geometry and must not set the ΔE
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

    assert data.orca_results == ()
    assert "job evidence could not be parsed into a complete SI block" in data.stage_rows[0].detail


def test_collect_refuses_engrad_energy_when_final_output_is_annotated(tmp_path: Path) -> None:
    # The .engrad is the primary energy channel, but it carries the same
    # unconverged SCF value and cannot be cross-checked on its own — the
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

    assert data.orca_results == ()
    assert "job evidence could not be parsed into a complete SI block" in data.stage_rows[0].detail


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
        + "x" * (report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES + 4096)
        + "\n****ORCA TERMINATED NORMALLY****\n",
        engrad_energy="-1.050000000000",
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results == ()
    assert "job evidence could not be parsed into a complete SI block" in data.stage_rows[0].detail


def test_collect_finds_clean_output_energy_beyond_scan_window(tmp_path: Path) -> None:
    # A clean final-energy line beyond the first window must still publish:
    # the backward scan may not trade the blind spot for over-refusal.
    payload = _energy_chain_payload(
        tmp_path,
        "orca_clean_beyond_window",
        "|  1> ! r2scan-3c Opt Freq TightSCF\n"
        "FINAL SINGLE POINT ENERGY -1.000000000000\n"
        "FINAL SINGLE POINT ENERGY -1.100000000000\n"
        + "x" * (report_energy_evidence._ORCA_ENERGY_SCAN_WINDOW_BYTES + 4096)
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


def test_collect_ignores_generation_engrad_mutated_after_publication(
    tmp_path: Path,
) -> None:
    generation = _orca_stage_dir(
        tmp_path,
        "orca_mutated_engrad",
        energy=-1.1,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_mutated_engrad",
        generation,
        status="completed",
        label="current",
    )
    (generation / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-999.000000000000"),
        encoding="utf-8",
    )

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].energy == pytest.approx(-1.1)
    assert data.orca_results[0].energy != pytest.approx(-999.0)


def test_completed_candidate_rejects_terminal_output_identity_mutation(
    tmp_path: Path,
) -> None:
    generation = _orca_stage_dir(
        tmp_path,
        "orca_mutated_output",
        energy=-1.1,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_mutated_output",
        generation,
        status="completed",
        label="mutated output",
    )
    output = generation / "orca.out"
    output.write_text(output.read_text(encoding="utf-8") + "post-publication mutation\n")

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()
    assert "no verified report generation recorded" in data.stage_rows[0].detail


def test_completed_candidate_rejects_state_report_identity_divergence(
    tmp_path: Path,
) -> None:
    generation = _orca_stage_dir(
        tmp_path,
        "orca_mutated_state",
        energy=-1.1,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_mutated_state",
        generation,
        status="completed",
        label="mutated state",
    )
    state = json.loads((generation / RUN_STATE_FILE).read_text(encoding="utf-8"))
    state["job"]["id"] = "different-job"
    (generation / RUN_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()
    assert "no verified report generation recorded" in data.stage_rows[0].detail


def test_completed_candidate_uses_verified_block_imaginary_count(
    tmp_path: Path,
) -> None:
    generation = _orca_stage_dir(
        tmp_path,
        "orca_forged_marker",
        energy=-1.1,
        reason="normal_termination",
    )
    stage = _orca_stage(
        "orca_forged_marker",
        generation,
        status="completed",
        label="forged marker",
    )
    state = json.loads((generation / RUN_STATE_FILE).read_text(encoding="utf-8"))
    state["engine_payload"]["attempts"][-1]["markers"]["imaginary_frequency_count"] = 7
    _publish_orca_machine(generation, state)

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].imaginary_count is None


@pytest.mark.parametrize("marker_count", [0, 1])
def test_uncompleted_stage_publishes_no_marker_imaginary_count(
    tmp_path: Path,
    marker_count: int,
) -> None:
    # A run that stopped short may have printed several Hessians, none of
    # which characterizes its final geometry; the analyzer's count for such a
    # run is not a Nimag and the stage table must not display it as one.
    generation = _orca_stage_dir(
        tmp_path,
        "orca_unfinished",
        energy=-1.1,
        reason="geometry_not_converged",
    )
    stage = _orca_stage(
        "orca_unfinished",
        generation,
        status="failed",
        label="unfinished",
    )
    state = json.loads((generation / RUN_STATE_FILE).read_text(encoding="utf-8"))
    state["engine_payload"]["attempts"][-1]["markers"]["imaginary_frequency_count"] = marker_count
    _publish_orca_machine(generation, state)

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].imaginary_count is None


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
            "task_kind": "opt",
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

    assert data.orca_results == ()
    assert "no verified report generation recorded" in data.stage_rows[0].detail


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
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "submission_result": {"job_id": "job-current"},
        },
        "metadata": metadata,
        "output_artifacts": [{"kind": "orca_report_json", "path": str(report_path)}],
    }

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()
    assert "no verified report generation recorded" in data.stage_rows[0].detail


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
            "task_kind": "opt",
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
                "task": {"engine": "orca", "task_kind": "opt"},
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

    assert data.orca_results == ()
    assert "no verified report generation recorded" in data.stage_rows[0].detail


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
                "task": {"engine": "orca", "task_kind": "opt"},
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


@pytest.mark.parametrize("status", ("failed", "cancelled"))
@pytest.mark.parametrize("annotated", (False, True))
def test_noncompleted_stage_energy_source_policy(
    tmp_path: Path,
    status: str,
    annotated: bool,
) -> None:
    stage_id = f"orca_{status}_{'annotated' if annotated else 'clean'}"
    generation = _orca_stage_dir(
        tmp_path,
        stage_id,
        energy=-100.0,
        reason="cancel_requested" if status == "cancelled" else "geometry_zero_distance",
    )
    (generation / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-99.000000000000"),
        encoding="utf-8",
    )
    if annotated:
        output = generation / "orca.out"
        output.write_text(
            output.read_text(encoding="utf-8").replace(
                "FINAL SINGLE POINT ENERGY     -100.000000000000",
                "FINAL SINGLE POINT ENERGY     -100.000000000000 (SCF not fully converged!)",
            ),
            encoding="utf-8",
        )
        state = json.loads((generation / RUN_STATE_FILE).read_text(encoding="utf-8"))
        state["engine_payload"]["attempts"][-1]["output_identity"] = confined_output_identity(
            generation, output
        )
        _publish_orca_machine(generation, state)

    stage = _orca_stage(stage_id, generation, status=status, label=status)
    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert len(data.orca_results) == 1
    assert data.orca_results[0].energy == (None if annotated else pytest.approx(-99.0))


def test_failed_stage_energy_uses_only_verified_current_generation(tmp_path: Path) -> None:
    job_dir = tmp_path / "orca_current_energy"
    generation = _orca_stage_dir(
        tmp_path,
        "orca_current_energy",
        energy=-100.0,
        reason="geometry_zero_distance",
    )
    (generation / "opt.engrad").unlink()
    (job_dir / "poison.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-999.000000000000"),
        encoding="utf-8",
    )
    previous = job_dir / "20000101-000000-deadbeef"
    previous.mkdir()
    (previous / "poison.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-999.000000000000"),
        encoding="utf-8",
    )
    (previous / "poison.out").write_text(
        "FINAL SINGLE POINT ENERGY -999.000000000000\n",
        encoding="utf-8",
    )
    stage = _orca_stage(
        "orca_current_energy",
        generation,
        status="failed",
        label="current",
    )

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results[0].energy == pytest.approx(-100.0)
    assert data.orca_results[0].energy != pytest.approx(-999.0)


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
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "payload": {"run_id": "run-current"},
        },
        "metadata": {
            "run_id": "run-current",
            "latest_known_path": str(job_dir),
        },
        "output_artifacts": [{"kind": "orca_report_json", "path": str(report_path)}],
    }

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))

    assert data.orca_results == ()
    assert "no verified report generation recorded" in data.stage_rows[0].detail


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
