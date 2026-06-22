from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.queue import list_queue
from orca_auto.flow.adapters.xtb import load_xtb_artifact_contract
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_cmd
from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_cmd
from orca_auto.flow.orchestration import advance_workflow, create_reaction_ts_search_workflow
from orca_auto.flow.registry import sync_workflow_registry
from orca_auto.flow.state import load_workflow_payload, resolve_workflow_workspace, workflow_summary
from tests.engine_process_helpers import process_one_crest_for_test, process_one_xtb_for_test


def _write_xyz(path: Path, *, comment: str, bond: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "2",
                comment,
                "H 0.0 0.0 0.0",
                f"H 0.0 0.0 {bond:.2f}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_orca_config(path: Path, *, allowed_root: Path, organized_root: Path) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            payload = dict(loaded)
    payload["orca"] = {
        "runtime": {
            "allowed_root": str(allowed_root.resolve()),
            "organized_root": str(organized_root.resolve()),
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
class ReactionWorkflowSmokeCase:
    workflow_root: Path
    workflow_id: str
    workspace_dir: Path
    crest_root: Path
    xtb_root: Path
    orca_root: Path
    config_path: Path


def _create_reaction_workflow_smoke_case(smoke_workspace: Any) -> ReactionWorkflowSmokeCase:
    # Use the workflow root from the shared config so direct engine enqueue
    # validation sees workflow-local paths as <workflow.root>/<workflow_id>/...
    workflow_root = smoke_workspace.root / "workflow_root"
    workflow_root.mkdir(parents=True, exist_ok=True)

    orca_allowed_root = smoke_workspace.root / "reaction_orca_runs"
    orca_organized_root = smoke_workspace.root / "reaction_orca_outputs"
    orca_allowed_root.mkdir(parents=True, exist_ok=True)
    orca_organized_root.mkdir(parents=True, exist_ok=True)
    config_path = smoke_workspace.config_path
    _write_orca_config(
        config_path, allowed_root=orca_allowed_root, organized_root=orca_organized_root
    )

    reactant_xyz = smoke_workspace.root / "reaction_inputs" / "reactant.xyz"
    product_xyz = smoke_workspace.root / "reaction_inputs" / "product.xyz"
    _write_xyz(reactant_xyz, comment="reaction reactant", bond=0.74)
    _write_xyz(product_xyz, comment="reaction product", bond=0.82)

    created = create_reaction_ts_search_workflow(
        reactant_xyz=str(reactant_xyz),
        product_xyz=str(product_xyz),
        workflow_root=workflow_root,
        priority=5,
        max_cores=2,
        max_memory_gb=2,
        max_crest_candidates=1,
        max_xtb_stages=1,
        max_xtb_handoff_retries=1,
        max_orca_stages=1,
    )
    workflow_id = str(created["workflow_id"])
    workspace_dir = workflow_root / workflow_id
    return ReactionWorkflowSmokeCase(
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        workspace_dir=workspace_dir,
        crest_root=workspace_dir / "01_crest",
        xtb_root=workspace_dir / "02_xtb",
        orca_root=workspace_dir / "03_orca",
        config_path=config_path,
    )


def _assert_initial_reaction_plan(case: ReactionWorkflowSmokeCase) -> None:
    payload = load_workflow_payload(
        resolve_workflow_workspace(target=case.workflow_id, workflow_root=case.workflow_root)
    )
    crest_stages = _engine_stages(payload, "crest")
    assert [stage["stage_id"] for stage in crest_stages] == [
        "crest_reactant_01",
        "crest_product_01",
    ]
    assert all(stage["status"] == "planned" for stage in crest_stages)
    assert _engine_stages(payload, "xtb") == []
    assert _engine_stages(payload, "orca") == []


def _submit_reaction_crest_stages(
    case: ReactionWorkflowSmokeCase, smoke_workspace: Any
) -> dict[str, Any]:
    payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        xtb_config=str(smoke_workspace.xtb_config_path),
        submit_ready=True,
    )
    crest_stages = _engine_stages(payload, "crest")
    assert len(crest_stages) == 2
    queue_entries = list_queue(case.crest_root)
    assert len(queue_entries) == 2
    assert {_queue_status(entry) for entry in queue_entries} == {"pending"}
    child_job_ids = {
        stage["metadata"]["child_job_id"]
        for stage in crest_stages
        if isinstance(stage.get("metadata"), dict)
    }
    assert {entry.task_id for entry in queue_entries} == child_job_ids
    return payload


def _process_reaction_crest_queue(smoke_workspace: Any, capsys: Any) -> None:
    cfg = crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path))
    assert process_one_crest_for_test(crest_queue_cmd, cfg) == "processed"
    assert process_one_crest_for_test(crest_queue_cmd, cfg) == "processed"
    output = capsys.readouterr().out
    assert output.count("status: completed") == 2


def _advance_to_xtb_submission(
    case: ReactionWorkflowSmokeCase,
    smoke_workspace: Any,
) -> dict[str, Any]:
    payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        xtb_config=str(smoke_workspace.xtb_config_path),
        orca_config=str(case.config_path),
        submit_ready=True,
    )
    assert all(stage["status"] == "completed" for stage in _engine_stages(payload, "crest"))
    xtb_stages = _engine_stages(payload, "xtb")
    assert len(xtb_stages) == 1
    xtb_stage = xtb_stages[0]
    assert xtb_stage["stage_id"] == "xtb_path_search_01"
    assert xtb_stage["status"] in {"queued", "running"}
    assert xtb_stage["metadata"]["queue_id"]
    assert xtb_stage["metadata"]["child_job_id"]

    queue_entries = list_queue(case.xtb_root)
    assert len(queue_entries) == 1
    assert queue_entries[0].task_id == xtb_stage["metadata"]["child_job_id"]
    assert queue_entries[0].queue_id == xtb_stage["metadata"]["queue_id"]
    assert _queue_status(queue_entries[0]) == "pending"
    return payload


def _process_reaction_xtb_queue(smoke_workspace: Any, capsys: Any) -> None:
    cfg = xtb_queue_cmd.load_config(str(smoke_workspace.xtb_config_path))
    assert process_one_xtb_for_test(xtb_queue_cmd, cfg) == "processed"
    output = capsys.readouterr().out
    assert "status: completed" in output


def _advance_to_orca_handoff(
    case: ReactionWorkflowSmokeCase,
    smoke_workspace: Any,
) -> dict[str, Any]:
    payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        xtb_config=str(smoke_workspace.xtb_config_path),
        orca_config=str(case.config_path),
        submit_ready=False,
    )
    xtb_stages = _engine_stages(payload, "xtb")
    assert len(xtb_stages) == 1
    xtb_stage = xtb_stages[0]
    assert xtb_stage["status"] == "completed"
    assert xtb_stage["metadata"]["reaction_handoff_status"] == "ready"

    xtb_contract = load_xtb_artifact_contract(
        xtb_index_root=case.xtb_root,
        target=xtb_stage["metadata"]["child_job_id"],
    )
    assert xtb_contract.status == "completed"
    assert xtb_contract.job_type == "path_search"
    assert xtb_contract.selected_candidate_paths
    assert any(detail.kind == "ts_guess" for detail in xtb_contract.candidate_details)

    orca_stages = _engine_stages(payload, "orca")
    assert len(orca_stages) == 1
    orca_stage = orca_stages[0]
    task_payload = orca_stage["task"]["payload"]
    reaction_dir = Path(task_payload["reaction_dir"])
    selected_input_xyz = Path(task_payload["selected_input_xyz"])
    selected_inp = Path(task_payload["selected_inp"])
    assert orca_stage["stage_id"] == "orca_optts_freq_01"
    assert orca_stage["status"] == "planned"
    assert reaction_dir.exists()
    assert reaction_dir.is_relative_to(case.orca_root)
    assert selected_input_xyz.exists()
    assert selected_inp.exists()
    assert (reaction_dir / "source_candidate.json").exists()
    assert (reaction_dir / "enqueue_payload.json").exists()
    assert "r2scan-3c OptTS Freq TightSCF" in selected_inp.read_text(encoding="utf-8")
    return payload


def _assert_reaction_workflow_persisted(
    case: ReactionWorkflowSmokeCase,
    payload: dict[str, Any],
) -> None:
    persisted_workspace = resolve_workflow_workspace(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
    )
    persisted_payload = load_workflow_payload(persisted_workspace)
    assert persisted_payload["workflow_id"] == payload["workflow_id"]
    assert persisted_payload["status"] == payload["status"]
    assert [stage["stage_id"] for stage in persisted_payload["stages"]] == [
        stage["stage_id"] for stage in payload["stages"]
    ]
    persisted_summary = workflow_summary(persisted_workspace, persisted_payload)
    persisted_record = sync_workflow_registry(
        case.workflow_root,
        persisted_workspace,
        persisted_payload,
    )
    assert persisted_summary["workflow_id"] == case.workflow_id
    assert persisted_record.stage_count == 4
    assert persisted_record.status in {"planned", "running"}
    admission_path = smoke_workspace_path(case) / "admission" / "admission_slots.json"
    if admission_path.exists():
        assert json.loads(admission_path.read_text(encoding="utf-8")) == []


def smoke_workspace_path(case: ReactionWorkflowSmokeCase) -> Path:
    return case.workflow_root.parent


def test_reaction_ts_workflow_executes_fake_crest_and_xtb_before_orca_handoff(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    case = _create_reaction_workflow_smoke_case(smoke_workspace)
    _assert_initial_reaction_plan(case)
    _submit_reaction_crest_stages(case, smoke_workspace)
    _process_reaction_crest_queue(smoke_workspace, capsys)
    _advance_to_xtb_submission(case, smoke_workspace)
    _process_reaction_xtb_queue(smoke_workspace, capsys)
    payload = _advance_to_orca_handoff(case, smoke_workspace)
    _assert_reaction_workflow_persisted(case, payload)
