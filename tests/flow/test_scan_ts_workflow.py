from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.flow.orchestration import create_scan_ts_search_workflow
from orca_auto.flow.orchestration.lifecycle import recompute_workflow_status_impl
from orca_auto.flow.orchestration.scan_orca_materialization import (
    append_scan_optts_stages_impl,
)
from orca_auto.flow.workflow.report import collect_workflow_report_data
from orca_auto.orca.scants import scan_profile_interior_maxima


def _write_input_xyz(path: Path) -> None:
    path.write_text("3\ntest\nH 0 0 0\nO 1.2 0 0\nO 3.0 0 0\n", encoding="utf-8")


def _create_workflow(tmp_path: Path, **overrides: object) -> dict:
    input_xyz = tmp_path / "input.xyz"
    _write_input_xyz(input_xyz)
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    kwargs: dict = {
        "input_xyz": str(input_xyz),
        "scan_coordinate": "B 0 1 = 1.20, 3.00, 10",
        "workflow_root": str(root),
        "workflow_id": "wf_scan_ts_test",
    }
    kwargs.update(overrides)
    return create_scan_ts_search_workflow(**kwargs)


def _write_scan_results(scan_stage: dict, energies: list[float]) -> Path:
    """Mark the scan finished on disk: surface table + numbered point xyzs."""
    selected_inp = Path(scan_stage["task"]["payload"]["selected_inp"])
    lines = [
        "RELAXED SURFACE SCAN RESULTS",
        "The Calculated Surface using the 'Actual Energy'",
        *(f"   {1.2 + 0.2 * idx:.8f} {energy:.8f}" for idx, energy in enumerate(energies)),
        "The Calculated Surface using the SCF energy",
        "   1.20000000 -101.00000000",
        "",
    ]
    selected_inp.with_suffix(".out").write_text("\n".join(lines), encoding="utf-8")
    for index in range(1, len(energies) + 1):
        (selected_inp.parent / f"{selected_inp.stem}.{index:03d}.xyz").write_text(
            f"3\npoint {index}\nH 0 0 0\nO 1 0 0\nO 2 0 0\n",
            encoding="utf-8",
        )
    return selected_inp


def test_scan_profile_interior_maxima_ranks_and_excludes_endpoints() -> None:
    # Two interior maxima; the higher endpoint must not become a candidate.
    energies = [-100.0, -99.5, -100.2, -99.85, -100.3, -99.3]
    maxima = scan_profile_interior_maxima(energies)
    assert [idx for idx, _ in maxima] == [1, 3]
    assert maxima[0][1] > maxima[1][1]
    assert scan_profile_interior_maxima([-100.0, -100.1, -100.2]) == []


def test_creation_materializes_scan_stage_with_geom_block(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)

    assert payload["template_name"] == "scan_ts_search"
    stage = payload["stages"][0]
    assert stage["stage_id"] == "orca_scan_01"
    assert stage["task"]["payload"]["task_kind"] == "relaxed_scan"
    inp_text = Path(stage["task"]["payload"]["selected_inp"]).read_text(encoding="utf-8")
    assert "! Opt r2scan-3c TightSCF" in inp_text
    assert "%geom" in inp_text
    assert "B 0 1 = 1.20, 3.00, 10" in inp_text
    assert "* xyzfile 0 1 scan_input.xyz" in inp_text
    parameters = payload["metadata"]["request"]["parameters"]
    assert parameters["scan_coordinate"] == "B 0 1 = 1.20, 3.00, 10"
    assert parameters["barrier_threshold_kcal"] == pytest.approx(0.5)
    workflow_json = json.loads(
        (tmp_path / "root" / "wf_scan_ts_test" / "workflow.json").read_text(encoding="utf-8")
    )
    assert workflow_json["stages"][0]["stage_id"] == "orca_scan_01"


def test_creation_rejects_malformed_scan_coordinate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scan_coordinate"):
        _create_workflow(tmp_path, scan_coordinate="not a scan")


def test_completed_scan_fans_out_ranked_optts_candidates(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -99.5, -100.2, -99.85, -100.3, -99.3])

    created = append_scan_optts_stages_impl(payload, workspace_dir=workspace)

    assert created
    optts = payload["stages"][1:]
    assert [stage["stage_id"] for stage in optts] == ["orca_optts_freq_01", "orca_optts_freq_02"]
    first_meta = optts[0]["input_artifacts"][0]["metadata"]
    second_meta = optts[1]["input_artifacts"][0]["metadata"]
    # Ranked by prominence; surface point indices 2 and 4, endpoint 6 excluded.
    assert first_meta["surface_point_index"] == 2
    assert second_meta["surface_point_index"] == 4
    assert first_meta["prominence_kcal"] > second_meta["prominence_kcal"]
    inp_text = Path(optts[0]["task"]["payload"]["selected_inp"]).read_text(encoding="utf-8")
    assert "OptTS" in inp_text
    assert "Freq" in inp_text
    assert "%geom" not in inp_text
    assert "* xyzfile 0 1 ts_guess.xyz" in inp_text
    # Idempotent: a second advance does not duplicate candidates.
    assert not append_scan_optts_stages_impl(payload, workspace_dir=workspace)


def test_candidate_cap_respects_max_orca_stages(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path, max_orca_stages=1)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -99.5, -100.2, -99.85, -100.3, -99.3])

    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    assert [stage["stage_id"] for stage in payload["stages"][1:]] == ["orca_optts_freq_01"]


def test_barrierless_scan_records_no_barrier_error(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -100.1, -100.2, -100.3])

    created = append_scan_optts_stages_impl(payload, workspace_dir=workspace)

    assert not created
    error = payload["metadata"]["workflow_error"]
    assert error["scope"] == "scan_ts_search_no_barrier"
    assert error["reason"] == "scan_profile_no_barrier"


def _recompute_status(payload: dict) -> str:
    def normalize(value: object) -> str:
        return str(value or "").strip()

    def effective(stage: dict) -> str:
        task = stage.get("task")
        task_status = task.get("status") if isinstance(task, dict) else None
        return normalize(task_status or stage.get("status")).lower()

    return recompute_workflow_status_impl(
        payload,
        normalize_text_fn=normalize,
        effective_stage_status_fn=effective,
    )


def test_failed_relaxed_scan_fails_the_workflow(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "failed"
    scan_stage["task"]["status"] = "failed"

    # A failed ORCA candidate stage normally does not fail the workflow, but the
    # relaxed scan is a prerequisite marked workflow_fatal.
    assert scan_stage["metadata"]["workflow_fatal"] is True
    assert _recompute_status(payload) == "failed"


def test_relaxed_scan_excluded_from_ts_candidate_ranking(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -99.5, -100.2, -99.85, -100.3, -99.3])
    append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    for stage in payload["stages"][1:]:
        stage["status"] = "completed"

    data = collect_workflow_report_data(workspace, payload)

    # The scan stage stays in the stage chain but not the candidate ranking.
    assert any(row.stage_id == "orca_scan_01" for row in data.stage_rows)
    assert all(result.stage_id != "orca_scan_01" for result in data.orca_results)
    assert {result.stage_id for result in data.orca_results} == {
        "orca_optts_freq_01",
        "orca_optts_freq_02",
    }


def test_incomplete_scan_does_not_fan_out(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    _write_scan_results(payload["stages"][0], [-100.0, -99.5, -100.2])

    assert not append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    assert len(payload["stages"]) == 1
    assert "workflow_error" not in payload.get("metadata", {})
