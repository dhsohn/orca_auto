from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from orca_auto.core.indexing import get_job_location
from orca_auto.core.queue import list_queue
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_cmd
from orca_auto.flow.orchestration import (
    advance_workflow,
    create_conformer_screening_workflow,
)
from orca_auto.flow.registry import sync_workflow_registry
from orca_auto.flow.state import load_workflow_payload, resolve_workflow_workspace, workflow_summary
from tests.engine_process_helpers import process_one_crest_for_test
from tests.integration.smoke_support import (
    assert_orca_job_publications,
    assert_workflow_publications,
    configure_fake_orca,
    orca_job_directories,
    pump_workflow,
    submit_public_workflow,
    write_fake_orca,
    write_h2,
)


def _write_xyz(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "2",
                "workflow input",
                "H 0.0 0.0 0.0",
                "H 0.0 0.0 0.74",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_orca_config(path: Path, *, allowed_root: Path) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            payload = dict(loaded)
    payload["orca"] = {
        "runtime": {
            "allowed_root": str(allowed_root.resolve()),
        },
        "paths": {
            "orca_executable": "/opt/orca/orca",
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _engine_stages(payload: dict[str, Any], engine: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stage in payload.get("stages", []):
        if not isinstance(stage, dict):
            continue
        task = stage.get("task")
        if not isinstance(task, dict):
            continue
        if str(task.get("engine", "")).strip() == engine:
            rows.append(stage)
    return rows


def _queue_status(entry: Any) -> str:
    return str(getattr(getattr(entry, "status", None), "value", "")).strip()


@dataclass(frozen=True)
class ConformerWorkflowSmokeCase:
    workflow_root: Path
    workflow_id: str
    workspace_dir: Path
    crest_root: Path
    orca_config_path: Path


def _create_conformer_workflow_smoke_case(smoke_workspace: Any) -> ConformerWorkflowSmokeCase:
    workflow_root = smoke_workspace.root / "workflow_root"
    workflow_root.mkdir(parents=True, exist_ok=True)

    orca_allowed_root = smoke_workspace.root / "orca_runs"
    orca_allowed_root.mkdir(parents=True, exist_ok=True)

    orca_config_path = smoke_workspace.config_path
    _write_orca_config(
        orca_config_path,
        allowed_root=orca_allowed_root,
    )

    input_xyz = smoke_workspace.root / "workflow_inputs" / "input.xyz"
    _write_xyz(input_xyz)

    created = create_conformer_screening_workflow(
        input_xyz=str(input_xyz),
        workflow_root=workflow_root,
        priority=5,
        max_cores=2,
        max_memory_gb=2,
        max_orca_stages=2,
    )
    workflow_id = str(created["workflow_id"])
    workspace_dir = workflow_root / workflow_id
    return ConformerWorkflowSmokeCase(
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        workspace_dir=workspace_dir,
        crest_root=workspace_dir / "01_crest",
        orca_config_path=orca_config_path,
    )


def _assert_initial_crest_plan(case: ConformerWorkflowSmokeCase) -> None:
    initial_payload = load_workflow_payload(
        resolve_workflow_workspace(target=case.workflow_id, workflow_root=case.workflow_root)
    )
    initial_crest_stages = _engine_stages(initial_payload, "crest")
    assert len(initial_crest_stages) == 1
    assert initial_crest_stages[0]["status"] == "planned"
    assert initial_crest_stages[0]["task"]["status"] == "planned"
    assert _engine_stages(initial_payload, "orca") == []


def _submit_crest_stage(case: ConformerWorkflowSmokeCase, smoke_workspace: Any) -> dict[str, Any]:
    submitted_payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        submit_ready=True,
    )
    submitted_crest_stage = _engine_stages(submitted_payload, "crest")[0]
    submitted_metadata = dict(submitted_crest_stage.get("metadata") or {})
    submitted_task = dict(submitted_crest_stage.get("task") or {})
    assert submitted_task["submission_result"]["status"] == "submitted"
    assert submitted_metadata["queue_id"]
    assert submitted_metadata["child_job_id"]
    assert _engine_stages(submitted_payload, "orca") == []

    record = get_job_location(case.crest_root, submitted_metadata["child_job_id"])
    assert record is not None
    assert record.app_name == "orca_auto_crest"
    assert record.status in {"queued", "pending"}

    queue_entries = list_queue(case.crest_root)
    assert len(queue_entries) == 1
    assert queue_entries[0].task_id == submitted_metadata["child_job_id"]
    assert queue_entries[0].queue_id == submitted_metadata["queue_id"]
    assert _queue_status(queue_entries[0]) == "pending"
    return submitted_metadata


def _run_submitted_crest_stage(
    submitted_metadata: dict[str, Any],
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    assert (
        process_one_crest_for_test(
            crest_queue_cmd,
            crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path)),
        )
        == "processed"
    )
    worker_output = capsys.readouterr().out
    assert f"queue_id: {submitted_metadata['queue_id']}" in worker_output
    assert f"job_id: {submitted_metadata['child_job_id']}" in worker_output
    assert "status: completed" in worker_output


def _advance_completed_crest_handoff(
    case: ConformerWorkflowSmokeCase,
    submitted_metadata: dict[str, Any],
    smoke_workspace: Any,
) -> dict[str, Any]:
    handed_off_payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        orca_config=str(case.orca_config_path),
        submit_ready=False,
    )
    crest_stage = _engine_stages(handed_off_payload, "crest")[0]
    crest_metadata = dict(crest_stage.get("metadata") or {})
    assert crest_stage["status"] == "completed"
    assert crest_stage["task"]["status"] == "completed"
    assert crest_metadata["queue_id"] == submitted_metadata["queue_id"]
    assert crest_metadata["child_job_id"] == submitted_metadata["child_job_id"]
    assert crest_metadata["latest_known_path"]
    assert Path(crest_metadata["latest_known_path"]).exists()
    assert Path(crest_metadata["latest_known_path"]).is_relative_to(case.workspace_dir / "01_crest")
    assert Path(crest_metadata["latest_known_path"], "crest_conformers.xyz").exists()
    return handed_off_payload


def _assert_planned_orca_handoff_stages(
    handed_off_payload: dict[str, Any],
    case: ConformerWorkflowSmokeCase,
) -> None:
    orca_stages = _engine_stages(handed_off_payload, "orca")
    assert len(orca_stages) == 2
    for index, stage in enumerate(orca_stages, start=1):
        task = dict(stage.get("task") or {})
        payload = dict(task.get("payload") or {})
        reaction_dir = Path(payload["reaction_dir"])
        selected_inp = Path(payload["selected_inp"])
        selected_input_xyz = Path(payload["selected_input_xyz"])

        assert stage["stage_id"] == f"orca_conformer_{index:02d}"
        assert stage["status"] == "planned"
        assert task["status"] == "planned"
        assert reaction_dir.exists()
        assert reaction_dir.is_relative_to(case.workspace_dir / "02_orca")
        assert selected_inp.exists()
        assert selected_input_xyz.exists()
        assert (reaction_dir / selected_inp.name).exists()
        assert (reaction_dir / "source_candidate.json").exists()
        assert (reaction_dir / "enqueue_payload.json").exists()
        assert "r2scan-3c Opt TightSCF" in selected_inp.read_text(encoding="utf-8")


def _assert_persisted_handoff(case: ConformerWorkflowSmokeCase) -> None:
    persisted_workspace = resolve_workflow_workspace(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
    )
    persisted_payload = load_workflow_payload(persisted_workspace)
    persisted_summary = workflow_summary(persisted_workspace, persisted_payload)
    persisted_record = sync_workflow_registry(
        case.workflow_root,
        persisted_workspace,
        persisted_payload,
    )
    assert persisted_summary["workflow_id"] == case.workflow_id
    assert persisted_record.stage_count == 3
    assert len(_engine_stages(persisted_payload, "orca")) == 2


def test_conformer_screening_workflow_handoff_smoke(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    case = _create_conformer_workflow_smoke_case(smoke_workspace)
    _assert_initial_crest_plan(case)
    submitted_metadata = _submit_crest_stage(case, smoke_workspace)
    _run_submitted_crest_stage(submitted_metadata, smoke_workspace, capsys)
    handed_off_payload = _advance_completed_crest_handoff(
        case,
        submitted_metadata,
        smoke_workspace,
    )
    _assert_planned_orca_handoff_stages(handed_off_payload, case)
    _assert_persisted_handoff(case)


def _public_conformer_case(
    smoke_workspace: Any,
    capsys: Any,
    *,
    orca_mode: Literal["success", "nonconverged"],
) -> tuple[dict[str, Any], Path]:
    fake_orca = smoke_workspace.root / "bin" / f"fake_orca_{orca_mode}"
    write_fake_orca(fake_orca, mode=orca_mode)
    configure_fake_orca(smoke_workspace.config_path, fake_orca)
    input_dir = smoke_workspace.root / f"conformer_{orca_mode}_input"
    write_h2(input_dir / "input.xyz", comment=f"conformer {orca_mode}")
    (input_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "max_orca_stages: 1",
                "resources:",
                "  max_cores: 1",
                "  max_memory_gb: 1",
                "orca:",
                '  route_line: "! r2scan-3c Opt TightSCF"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    created = submit_public_workflow(input_dir, smoke_workspace.config_path, capsys)
    workspace_dir = Path(created["metadata"]["workspace_dir"])
    payload = pump_workflow(
        workflow_root=smoke_workspace.xtb_allowed_root.parents[1],
        workspace_dir=workspace_dir,
        config_path=smoke_workspace.config_path,
        admission_root=smoke_workspace.admission_root,
    )
    return payload, workspace_dir


def test_conformer_screening_workflow_completes_fake_engine_lifecycle(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    payload, workspace_dir = _public_conformer_case(
        smoke_workspace,
        capsys,
        orca_mode="success",
    )

    assert payload["status"] == "completed"
    crest_stages = _engine_stages(payload, "crest")
    orca_stages = _engine_stages(payload, "orca")
    assert len(crest_stages) == 1 and crest_stages[0]["status"] == "completed"
    assert len(orca_stages) == 1 and orca_stages[0]["status"] == "completed"
    job_dirs = orca_job_directories(payload)
    assert len(job_dirs) == 1
    state = assert_orca_job_publications(job_dirs[0], expected_status="completed", expect_si=True)
    assert state["engine_payload"]["final_result"]["reason"] == "normal_termination"
    assert_workflow_publications(workspace_dir, payload)
    workflow_si = (workspace_dir / "workflow_si.md").read_text(encoding="utf-8")
    assert "! r2scan-3c Opt TightSCF" in workflow_si


def test_conformer_screening_workflow_terminalizes_all_orca_failures(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    payload, workspace_dir = _public_conformer_case(
        smoke_workspace,
        capsys,
        orca_mode="nonconverged",
    )

    assert payload["status"] == "failed"
    workflow_error = payload["metadata"]["workflow_error"]
    assert workflow_error["scope"] == "conformer_screening_orca_conformers_exhausted"
    assert workflow_error["reason"] == "conformers_failed"
    orca_stages = _engine_stages(payload, "orca")
    assert len(orca_stages) == 1 and orca_stages[0]["status"] == "failed"
    job_dirs = orca_job_directories(payload)
    assert len(job_dirs) == 1
    state = assert_orca_job_publications(job_dirs[0], expected_status="failed", expect_si=False)
    assert state["engine_payload"]["attempts"][-1]["analyzer_reason"] == ("geometry_not_converged")
    assert_workflow_publications(workspace_dir, payload)
