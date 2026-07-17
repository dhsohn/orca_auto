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


def test_missing_scan_extension_budget_fails_closed(tmp_path: Path) -> None:
    # The persisted stage budgets are a required contract: a payload whose
    # max_scan_extensions was stripped must not silently regrow a default.
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -100.1, -100.2, -100.3])
    del payload["metadata"]["request"]["parameters"]["max_scan_extensions"]

    with pytest.raises(ValueError, match="max_scan_extensions"):
        append_scan_optts_stages_impl(payload, workspace_dir=workspace)


def test_barrierless_scan_records_no_barrier_error_without_extensions(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path, max_scan_extensions=0)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -100.1, -100.2, -100.3])

    created = append_scan_optts_stages_impl(payload, workspace_dir=workspace)

    assert not created
    error = payload["metadata"]["workflow_error"]
    assert error["scope"] == "scan_ts_search_no_barrier"
    assert error["reason"] == "scan_profile_no_barrier"


def test_barrierless_scan_extends_then_finds_barrier_in_extension(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    # Monotonic over the requested range: the maximum lies beyond the endpoint.
    _write_scan_results(scan_stage, [-100.3, -100.2, -100.1, -100.0])

    created = append_scan_optts_stages_impl(payload, workspace_dir=workspace)

    assert created
    extension = payload["stages"][1]
    assert extension["stage_id"] == "orca_scan_02"
    assert extension["metadata"]["scan_direction"] == "forward"
    assert extension["metadata"]["workflow_fatal"] is True
    # Original spec "B 0 1 = 1.20, 3.00, 10" -> step 0.2, extension of
    # max(6, round(9 * 0.2)) = 6 points continuing past the endpoint.
    assert extension["metadata"]["scan_coordinate"] == "B 0 1 = 3.2, 4.2, 6"
    extension_inp = Path(extension["task"]["payload"]["selected_inp"]).read_text(encoding="utf-8")
    assert "B 0 1 = 3.2, 4.2, 6" in extension_inp
    assert "workflow_error" not in payload.get("metadata", {})

    # The extension completes with a barrier: fan-out maps the maximum back to
    # the extension stage's numbered xyz on the COMBINED profile.
    extension["status"] = "completed"
    _write_scan_results(extension, [-99.9, -99.5, -99.8, -100.0, -100.1, -100.2])

    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    optts = [s for s in payload["stages"] if s["stage_id"].startswith("orca_optts_freq_")]
    assert len(optts) >= 1
    top_meta = optts[0]["input_artifacts"][0]["metadata"]
    assert top_meta["scan_stage_id"] == "orca_scan_02"
    assert top_meta["scan_direction"] == "forward"
    assert "scan.002.xyz" in optts[0]["input_artifacts"][0]["path"]


def test_extension_exhausted_records_no_barrier_error(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.3, -100.2, -100.1, -100.0])
    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    extension = payload["stages"][1]
    extension["status"] = "completed"
    _write_scan_results(extension, [-99.9, -99.8, -99.7, -99.6, -99.5, -99.4])

    created = append_scan_optts_stages_impl(payload, workspace_dir=workspace)

    assert not created
    assert payload["metadata"]["workflow_error"]["reason"] == "scan_profile_no_barrier"


def test_failed_forward_candidates_trigger_reverse_scan_and_fanout(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -99.5, -100.2, -99.85, -100.3, -99.3])
    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    forward_optts = [s for s in payload["stages"] if s["stage_id"].startswith("orca_optts_freq_")]
    for stage in forward_optts:
        stage["status"] = "failed"

    # All forward candidates failed verification -> reverse scan stage appended,
    # walking the full range back from the forward endpoint.
    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    reverse = next(s for s in payload["stages"] if s["stage_id"] == "orca_scan_reverse_01")
    assert reverse["metadata"]["scan_direction"] == "reverse"
    assert reverse["metadata"]["workflow_fatal"] is True
    assert reverse["metadata"]["scan_coordinate"] == "B 0 1 = 3, 1.2, 10"
    assert "workflow_error" not in payload.get("metadata", {})

    # Reverse scan completes with its own interior maximum -> reverse fan-out
    # with continued stage numbering.
    reverse["status"] = "completed"
    _write_scan_results(reverse, [-100.0, -99.6, -100.4, -100.5])

    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    reverse_optts = [
        s
        for s in payload["stages"]
        if s["stage_id"].startswith("orca_optts_freq_")
        and s["input_artifacts"][0]["metadata"]["scan_direction"] == "reverse"
    ]
    assert len(reverse_optts) == 1
    assert reverse_optts[0]["stage_id"] == f"orca_optts_freq_{len(forward_optts) + 1:02d}"
    assert reverse_optts[0]["input_artifacts"][0]["metadata"]["scan_stage_id"] == (
        "orca_scan_reverse_01"
    )

    # Reverse candidates also fail -> candidates exhausted.
    for stage in reverse_optts:
        stage["status"] = "failed"
    assert not append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    assert payload["metadata"]["workflow_error"]["scope"] == ("scan_ts_search_candidates_exhausted")
    assert payload["metadata"]["workflow_error"]["reason"] == "ts_candidates_exhausted"


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


def test_cancelled_forward_candidates_do_not_trigger_reverse_scan(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    scan_stage = payload["stages"][0]
    scan_stage["status"] = "completed"
    _write_scan_results(scan_stage, [-100.0, -99.5, -100.2, -99.85, -100.3, -99.3])
    assert append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    forward_optts = [s for s in payload["stages"] if s["stage_id"].startswith("orca_optts_freq_")]

    # Workflow cancellation transitions the forward candidates to cancelled;
    # this must NOT be treated as "every candidate failed to verify a TS", so
    # no reverse scan is appended and no exhaustion error is recorded.
    for stage in forward_optts:
        stage["status"] = "cancelled"

    assert not append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    assert not any(s["stage_id"] == "orca_scan_reverse_01" for s in payload["stages"])
    assert "workflow_error" not in payload.get("metadata", {})

    # And the workflow must not end COMPLETED off the back of it: with the
    # exhaustion recorder standing down, the status reducer is the only thing
    # standing between "every candidate cancelled" and a false success.
    scan_stage["task"]["status"] = "completed"
    for stage in forward_optts:
        stage["task"]["status"] = "cancelled"
    assert _recompute_status(payload) == "cancelled"


def test_incomplete_scan_does_not_fan_out(tmp_path: Path) -> None:
    payload = _create_workflow(tmp_path)
    workspace = tmp_path / "root" / "wf_scan_ts_test"
    _write_scan_results(payload["stages"][0], [-100.0, -99.5, -100.2])

    assert not append_scan_optts_stages_impl(payload, workspace_dir=workspace)
    assert len(payload["stages"]) == 1
    assert "workflow_error" not in payload.get("metadata", {})
