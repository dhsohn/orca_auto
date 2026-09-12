from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.admission import list_slots
from orca_auto.core.queue import list_queue
from orca_auto.core.queue.processes import worker_pid_file_path
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_cmd
from orca_auto.flow.engines.crest.engine import ENGINE_DEFINITION as CREST_ENGINE_DEFINITION
from orca_auto.flow.orchestration import advance_workflow
from orca_auto.flow.registry import sync_workflow_registry
from orca_auto.flow.state import load_workflow_payload, resolve_workflow_workspace, workflow_summary
from orca_auto.orca.config import load_config as load_orca_config
from orca_auto.orca.engine import ENGINE_DEFINITION as ORCA_ENGINE_DEFINITION
from orca_auto.orca.queue import worker as orca_queue_cmd
from tests.integration.smoke_support import (
    assert_orca_job_publications,
    assert_workflow_publications,
    orca_job_directories,
    orca_job_generation_dir,
    pump_workflow,
    submit_public_workflow,
)


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


def _write_fake_orca(binary_path: Path, counter_path: Path) -> None:
    binary_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                "from pathlib import Path",
                f"counter = Path({str(counter_path)!r})",
                "count = 0",
                "if counter.exists():",
                "    try:",
                "        count = int(counter.read_text(encoding='utf-8').strip() or '0')",
                "    except ValueError:",
                "        count = 0",
                "counter.write_text(str(count + 1), encoding='utf-8')",
                "inp_name = Path(sys.argv[1]).name if len(sys.argv) > 1 else '<missing>'",
                "print(f'Fake ORCA consumed {inp_name}')",
                "print('Program Version 6.0.1 - RELEASE -')",
                "print('|  1> ! r2scan-3c Opt Freq TightSCF')",
                "print('|  2> * xyz 0 1')",
                "print('|  3> H 0 0 0')",
                "print('|  4> H 0 0 0.74')",
                "print('|  5> *')",
                "print('CARTESIAN COORDINATES (ANGSTROEM)')",
                "print('---------------------------------')",
                "print(' H 0.000000 0.000000 0.000000')",
                "print(' H 0.000000 0.000000 0.740000')",
                "print('')",
                "print('FINAL SINGLE POINT ENERGY -1.100000000000')",
                "print('THE OPTIMIZATION HAS CONVERGED')",
                "print('VIBRATIONAL FREQUENCIES')",
                "print('  1:  512.34 cm**-1')",
                "print('  2:  120.00 cm**-1')",
                "print('TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds')",
                "print('****ORCA TERMINATED NORMALLY****')",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    binary_path.chmod(0o755)


def _write_orca_config(
    path: Path,
    *,
    orca_executable: Path,
) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            payload = dict(loaded)
    payload["orca"] = {
        "runtime": {},
        "paths": {
            "orca_executable": str(orca_executable.resolve()),
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
    xtb_root: Path
    orca_root: Path
    orca_queue_root: Path
    fake_orca_counter: Path
    config_path: Path


def _create_conformer_workflow_smoke_case(
    smoke_workspace: Any,
    capsys: Any,
) -> ConformerWorkflowSmokeCase:
    fake_orca_counter = smoke_workspace.root / "fake_orca_counter.txt"
    fake_orca = smoke_workspace.root / "bin" / "fake_orca"
    _write_fake_orca(fake_orca, fake_orca_counter)
    config_path = smoke_workspace.config_path
    _write_orca_config(
        config_path,
        orca_executable=fake_orca,
    )

    input_dir = smoke_workspace.root / "workflow_root" / "reaction_success_input"
    _write_xyz(input_dir / "input.xyz", comment="conformer", bond=0.74)
    (input_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "priority: 5",
                "max_orca_stages: 1",
                "resources:",
                "  max_cores: 2",
                "  max_memory_gb: 2",
                "orca:",
                '  route_line: "! r2scan-3c Opt Freq TightSCF"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    created = submit_public_workflow(input_dir, smoke_workspace.config_path, capsys)
    workflow_id = str(created["workflow_id"])
    workspace_dir = Path(created["metadata"]["workspace_dir"])
    # The workspace is a generation directory inside the submitted scaffold.
    workflow_root = smoke_workspace.root / "workflow_root"
    return ConformerWorkflowSmokeCase(
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        workspace_dir=workspace_dir,
        crest_root=workspace_dir / "01_crest",
        xtb_root=workspace_dir / "02_xtb",
        orca_root=workspace_dir / "03_orca",
        orca_queue_root=workflow_root,
        fake_orca_counter=fake_orca_counter,
        config_path=config_path,
    )


def _assert_initial_conformer_plan(case: ConformerWorkflowSmokeCase) -> None:
    payload = load_workflow_payload(
        resolve_workflow_workspace(target=case.workflow_id, workflow_root=case.workflow_root)
    )
    crest_stages = _engine_stages(payload, "crest")
    assert [stage["stage_id"] for stage in crest_stages] == [
        "crest_conformer_01",
    ]
    assert all(stage["status"] == "planned" for stage in crest_stages)
    assert _engine_stages(payload, "xtb") == []
    assert _engine_stages(payload, "orca") == []


def _submit_conformer_crest_stage(
    case: ConformerWorkflowSmokeCase, smoke_workspace: Any
) -> dict[str, Any]:
    payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        xtb_config=str(smoke_workspace.xtb_config_path),
        submit_ready=True,
    )
    crest_stages = _engine_stages(payload, "crest")
    assert len(crest_stages) == 1
    queue_entries = list_queue(case.crest_root)
    assert len(queue_entries) == 1
    assert {_queue_status(entry) for entry in queue_entries} == {"pending"}
    child_job_ids = {
        stage["metadata"]["child_job_id"]
        for stage in crest_stages
        if isinstance(stage.get("metadata"), dict)
    }
    assert {entry.task_id for entry in queue_entries} == child_job_ids
    return payload


def _process_conformer_crest_queue(case: ConformerWorkflowSmokeCase, smoke_workspace: Any) -> None:
    cfg = crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path))
    worker = crest_queue_cmd.QueueWorker(
        cfg,
        str(smoke_workspace.crest_config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0
    assert list_slots(smoke_workspace.admission_root) == []
    assert not worker_pid_file_path(
        case.crest_root,
        CREST_ENGINE_DEFINITION.queue_functions.worker_pid_file_name,
    ).exists()


def _advance_to_orca_handoff(
    case: ConformerWorkflowSmokeCase, smoke_workspace: Any
) -> dict[str, Any]:
    payload = advance_workflow(
        target=case.workflow_id,
        workflow_root=case.workflow_root,
        crest_config=str(smoke_workspace.crest_config_path),
        orca_config=str(case.config_path),
        submit_ready=False,
    )
    assert all(stage["status"] == "completed" for stage in _engine_stages(payload, "crest"))
    orca_stages = _engine_stages(payload, "orca")
    assert len(orca_stages) == 1
    stage = orca_stages[0]
    task_payload = stage["task"]["payload"]
    reaction_dir = Path(task_payload["reaction_dir"])
    assert stage["stage_id"] == "orca_conformer_01"
    assert stage["status"] == "planned"
    assert reaction_dir.is_relative_to(case.orca_root)
    assert Path(task_payload["selected_input_xyz"]).exists()
    selected_inp = Path(task_payload["selected_inp"])
    assert selected_inp.exists()
    assert (reaction_dir / "source_candidate.json").exists()
    assert (reaction_dir / "enqueue_payload.json").exists()
    assert "r2scan-3c Opt Freq TightSCF" in selected_inp.read_text(encoding="utf-8")
    return payload


def _submit_conformer_orca_stage(
    case: ConformerWorkflowSmokeCase,
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
    orca_stages = _engine_stages(payload, "orca")
    assert len(orca_stages) == 1
    orca_stage = orca_stages[0]
    assert orca_stage["status"] in {"queued", "running"}
    assert orca_stage["metadata"]["queue_id"]

    queue_entries = list_queue(case.orca_queue_root)
    assert len(queue_entries) == 1
    assert queue_entries[0].queue_id == orca_stage["metadata"]["queue_id"]
    assert _queue_status(queue_entries[0]) == "pending"
    assert not case.fake_orca_counter.exists()
    return payload


def _process_conformer_orca_queue(case: ConformerWorkflowSmokeCase) -> None:
    cfg = load_orca_config(str(case.config_path))
    worker = orca_queue_cmd.QueueWorker(
        cfg,
        str(case.config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0
    assert case.fake_orca_counter.read_text(encoding="utf-8") == "1"
    assert list_slots(smoke_workspace_path(case) / "admission") == []
    assert not worker_pid_file_path(
        case.orca_queue_root,
        ORCA_ENGINE_DEFINITION.queue_functions.worker_pid_file_name,
    ).exists()


def _sync_completed_orca_stage(
    case: ConformerWorkflowSmokeCase,
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
    orca_stages = _engine_stages(payload, "orca")
    assert len(orca_stages) == 1
    orca_stage = orca_stages[0]
    assert orca_stage["status"] == "completed"
    assert orca_stage["metadata"]["attempt_count"] == 1
    artifact_kinds = {
        str(artifact.get("kind"))
        for artifact in orca_stage.get("output_artifacts", [])
        if isinstance(artifact, dict)
    }
    assert {"orca_run_state", "orca_report_json"} <= artifact_kinds
    queue_entries = list_queue(case.orca_queue_root)
    assert len(queue_entries) == 1
    assert _queue_status(queue_entries[0]) == "completed"
    return payload


def _assert_conformer_workflow_persisted(
    case: ConformerWorkflowSmokeCase,
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
    assert persisted_record.stage_count == 2
    assert persisted_record.status == "completed"
    admission_path = smoke_workspace_path(case) / "admission" / "admission_slots.json"
    if admission_path.exists():
        assert json.loads(admission_path.read_text(encoding="utf-8")) == []


def smoke_workspace_path(case: ConformerWorkflowSmokeCase) -> Path:
    return case.workflow_root.parent


def test_conformer_workflow_executes_fake_crest_and_orca_full_lifecycle(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    case = _create_conformer_workflow_smoke_case(smoke_workspace, capsys)
    _assert_initial_conformer_plan(case)
    _submit_conformer_crest_stage(case, smoke_workspace)
    _process_conformer_crest_queue(case, smoke_workspace)
    _advance_to_orca_handoff(case, smoke_workspace)
    _submit_conformer_orca_stage(case, smoke_workspace)
    _process_conformer_orca_queue(case)
    payload = _sync_completed_orca_stage(case, smoke_workspace)
    _assert_conformer_workflow_persisted(case, payload)
    job_dirs = orca_job_directories(payload)
    assert len(job_dirs) == 1
    orca_state = assert_orca_job_publications(
        job_dirs[0], expected_status="completed", expect_si=True
    )
    job_report = (orca_job_generation_dir(orca_state) / "job_report.html").read_text(
        encoding="utf-8"
    )
    assert "Frequency values were parsed" in job_report
    assert "No frequency calculation found" not in job_report
    assert_workflow_publications(case.workspace_dir, payload)


def test_conformer_workflow_terminalizes_failed_crest_without_orca_submission(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    case = _create_conformer_workflow_smoke_case(smoke_workspace, capsys)
    failed_crest = smoke_workspace.root / "bin" / "failed_crest"
    failed_crest.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    failed_crest.chmod(0o755)
    config = yaml.safe_load(case.config_path.read_text(encoding="utf-8"))
    config["workflow"]["paths"]["crest_executable"] = str(failed_crest)
    case.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    payload = pump_workflow(
        workflow_root=case.workflow_root,
        workspace_dir=case.workspace_dir,
        config_path=case.config_path,
        admission_root=smoke_workspace.admission_root,
        max_cycles=16,
    )
    assert payload["status"] == "failed"
    assert len(_engine_stages(payload, "crest")) == 1
    assert _engine_stages(payload, "crest")[0]["status"] == "failed"
    assert _engine_stages(payload, "orca") == []
    assert list_queue(case.orca_queue_root) == []
    assert not case.fake_orca_counter.exists()
    report_html = (case.workspace_dir / "workflow_report.html").read_text(encoding="utf-8")
    assert case.workflow_id in report_html
    assert "conformer_screening" in report_html
    assert ">failed<" in report_html
    assert _engine_stages(payload, "crest")[0]["metadata"]["reason"] in report_html
    assert not (case.workspace_dir / "workflow_si.md").exists()
