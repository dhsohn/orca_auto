from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from orca_auto.core.queue import list_queue
from tests.integration.smoke_support import (
    assert_orca_job_publications,
    assert_workflow_publications,
    configure_fake_orca,
    orca_job_directories,
    orca_job_generation_dir,
    pump_workflow,
    submit_public_workflow,
    write_fake_orca,
    write_h2,
)


def _engine_stages(payload: dict[str, Any], engine: str) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        task = raw_stage.get("task")
        if isinstance(task, dict) and str(task.get("engine") or "") == engine:
            stages.append(raw_stage)
    return stages


def _public_scan_case(
    smoke_workspace: Any,
    capsys: Any,
    *,
    scan_profile: Literal["barrier", "barrierless"],
) -> tuple[dict[str, Any], Path, Path]:
    fake_orca = smoke_workspace.root / "bin" / f"fake_orca_scan_{scan_profile}"
    write_fake_orca(fake_orca, scan_profile=scan_profile)
    configure_fake_orca(smoke_workspace.config_path, fake_orca)
    input_dir = smoke_workspace.root / "workflow_root" / f"scan_{scan_profile}_input"
    write_h2(input_dir / "input.xyz", comment=f"scan {scan_profile}")
    (input_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: scan_ts_search",
                'scan_coordinate: "B 0 1 = 0.70, 1.00, 4"',
                "barrier_threshold_kcal: 0.5",
                "max_scan_extensions: 0",
                "max_orca_stages: 1",
                "resources:",
                "  max_cores: 1",
                "  max_memory_gb: 1",
                "orca:",
                '  route_line: "! Opt r2scan-3c TightSCF"',
                'orca_optts_route_line: "! OptTS Freq r2scan-3c TightSCF"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    created = submit_public_workflow(input_dir, smoke_workspace.config_path, capsys)
    workspace_dir = Path(created["metadata"]["workspace_dir"])
    workflow_root = smoke_workspace.xtb_allowed_root.parents[1]
    payload = pump_workflow(
        workflow_root=workflow_root,
        workspace_dir=workspace_dir,
        config_path=smoke_workspace.config_path,
        admission_root=smoke_workspace.admission_root,
    )
    return payload, workspace_dir, workflow_root


def test_scan_ts_workflow_completes_fake_orca_lifecycle(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    payload, workspace_dir, workflow_root = _public_scan_case(
        smoke_workspace,
        capsys,
        scan_profile="barrier",
    )

    assert payload["status"] == "completed"
    stages = _engine_stages(payload, "orca")
    assert [stage["stage_id"] for stage in stages] == [
        "orca_scan_01",
        "orca_optts_freq_01",
    ]
    assert all(stage["status"] == "completed" for stage in stages)
    job_dirs = orca_job_directories(payload)
    assert len(job_dirs) == 2
    assert_orca_job_publications(job_dirs[0], expected_status="completed", expect_si=False)
    optts_state = assert_orca_job_publications(
        job_dirs[1], expected_status="completed", expect_si=True
    )
    assert optts_state["engine_payload"]["final_result"]["reason"] == "ts_criteria_met"
    optts_report = (orca_job_generation_dir(optts_state) / "job_report.html").read_text(
        encoding="utf-8"
    )
    assert "Frequency values were parsed" in optts_report
    assert "No frequency calculation found" not in optts_report
    assert len(list_queue(workflow_root)) == 2
    assert_workflow_publications(workspace_dir, payload)
    workflow_si = (workspace_dir / "workflow_si.md").read_text(encoding="utf-8")
    assert "! OptTS Freq r2scan-3c TightSCF" in workflow_si


def test_scan_ts_workflow_terminalizes_barrierless_profile(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    payload, workspace_dir, workflow_root = _public_scan_case(
        smoke_workspace,
        capsys,
        scan_profile="barrierless",
    )

    assert payload["status"] == "failed"
    error = payload["metadata"]["workflow_error"]
    assert error["scope"] == "scan_ts_search_no_barrier"
    assert error["reason"] == "scan_profile_no_barrier"
    stages = _engine_stages(payload, "orca")
    assert [stage["stage_id"] for stage in stages] == ["orca_scan_01"]
    assert stages[0]["status"] == "completed"
    job_dirs = orca_job_directories(payload)
    assert len(job_dirs) == 1
    assert_orca_job_publications(job_dirs[0], expected_status="completed", expect_si=False)
    assert len(list_queue(workflow_root)) == 1
    assert_workflow_publications(workspace_dir, payload)
