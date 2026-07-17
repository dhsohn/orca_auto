from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orca_auto.core.admission import list_slots
from orca_auto.core.queue import list_queue
from orca_auto.core.queue.processes import worker_pid_file_path
from orca_auto.flow.adapters.xtb import load_xtb_artifact_contract
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_cmd
from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_cmd
from orca_auto.flow.orchestration import advance_workflow
from orca_auto.flow.registry import sync_workflow_registry
from orca_auto.flow.state import load_workflow_payload, resolve_workflow_workspace, workflow_summary
from orca_auto.orca.queue import worker as orca_queue_cmd
from tests.integration.smoke_support import (
    assert_orca_job_publications,
    assert_workflow_publications,
    assert_workflow_report,
    configure_fake_orca,
    orca_job_directories,
    orca_job_generation_dir,
    pump_workflow,
    submit_public_workflow,
)
from tests.integration.smoke_support import (
    write_fake_orca as write_public_fake_orca,
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
                "print('|  1> ! r2scan-3c OptTS Freq TightSCF')",
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
                "print('  1: -512.34 cm**-1')",
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
        "runtime": {
            "default_max_retries": 0,
        },
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
class ReactionWorkflowSmokeCase:
    workflow_root: Path
    workflow_id: str
    workspace_dir: Path
    crest_root: Path
    xtb_root: Path
    orca_root: Path
    orca_queue_root: Path
    fake_orca_counter: Path
    config_path: Path


def _create_reaction_workflow_smoke_case(
    smoke_workspace: Any,
    capsys: Any,
) -> ReactionWorkflowSmokeCase:
    fake_orca_counter = smoke_workspace.root / "fake_orca_counter.txt"
    fake_orca = smoke_workspace.root / "bin" / "fake_orca"
    _write_fake_orca(fake_orca, fake_orca_counter)
    config_path = smoke_workspace.config_path
    _write_orca_config(
        config_path,
        orca_executable=fake_orca,
    )

    input_dir = smoke_workspace.root / "workflow_root" / "reaction_success_input"
    reactant_xyz = input_dir / "reactant.xyz"
    product_xyz = input_dir / "product.xyz"
    _write_xyz(reactant_xyz, comment="reaction reactant", bond=0.74)
    _write_xyz(product_xyz, comment="reaction product", bond=0.82)
    (input_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "priority: 5",
                "max_crest_candidates: 1",
                "max_xtb_stages: 1",
                "max_xtb_handoff_retries: 1",
                "max_orca_stages: 1",
                "resources:",
                "  max_cores: 2",
                "  max_memory_gb: 2",
                "orca:",
                '  route_line: "! r2scan-3c OptTS Freq TightSCF"',
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
    return ReactionWorkflowSmokeCase(
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


def _process_reaction_crest_queue(case: ReactionWorkflowSmokeCase, smoke_workspace: Any) -> None:
    cfg = crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path))
    worker = crest_queue_cmd.QueueWorker(
        cfg,
        str(smoke_workspace.crest_config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0
    assert worker.run_once(idle_message=None, blocked_message=None) == 0
    assert list_slots(smoke_workspace.admission_root) == []
    assert not worker_pid_file_path(
        case.crest_root,
        crest_queue_cmd.WORKER_PID_FILE,
    ).exists()


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


def _process_reaction_xtb_queue(case: ReactionWorkflowSmokeCase, smoke_workspace: Any) -> None:
    cfg = xtb_queue_cmd.load_config(str(smoke_workspace.xtb_config_path))
    worker = xtb_queue_cmd.QueueWorker(
        cfg,
        str(smoke_workspace.xtb_config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0
    assert list_slots(smoke_workspace.admission_root) == []
    assert not worker_pid_file_path(
        case.xtb_root,
        xtb_queue_cmd.WORKER_PID_FILE,
    ).exists()


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


def _submit_reaction_orca_stage(
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


def _process_reaction_orca_queue(case: ReactionWorkflowSmokeCase) -> None:
    cfg = orca_queue_cmd.load_config(str(case.config_path))
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
        orca_queue_cmd.WORKER_PID_FILE,
    ).exists()


def _sync_completed_orca_stage(
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
    assert {"orca_run_state", "orca_report_json", "orca_report_md"} <= artifact_kinds
    queue_entries = list_queue(case.orca_queue_root)
    assert len(queue_entries) == 1
    assert _queue_status(queue_entries[0]) == "completed"
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
    assert persisted_record.status == "completed"
    admission_path = smoke_workspace_path(case) / "admission" / "admission_slots.json"
    if admission_path.exists():
        assert json.loads(admission_path.read_text(encoding="utf-8")) == []


def smoke_workspace_path(case: ReactionWorkflowSmokeCase) -> Path:
    return case.workflow_root.parent


def test_reaction_ts_workflow_executes_fake_crest_xtb_and_orca_full_lifecycle(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    case = _create_reaction_workflow_smoke_case(smoke_workspace, capsys)
    _assert_initial_reaction_plan(case)
    _submit_reaction_crest_stages(case, smoke_workspace)
    _process_reaction_crest_queue(case, smoke_workspace)
    _advance_to_xtb_submission(case, smoke_workspace)
    _process_reaction_xtb_queue(case, smoke_workspace)
    _advance_to_orca_handoff(case, smoke_workspace)
    _submit_reaction_orca_stage(case, smoke_workspace)
    _process_reaction_orca_queue(case)
    payload = _sync_completed_orca_stage(case, smoke_workspace)
    _assert_reaction_workflow_persisted(case, payload)
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


def _write_missing_ts_xtb(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'forward barrier (kcal) : 12.4\n'
            printf 'backward barrier (kcal) : 8.6\n'
            printf 'reaction energy (kcal) : -3.1\n'
            exit 0
            """
        ).lstrip(),
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_reaction_ts_workflow_terminalizes_failed_xtb_handoff(
    smoke_workspace: Any,
    capsys: Any,
) -> None:
    fake_orca = smoke_workspace.root / "bin" / "fake_orca_unused"
    write_public_fake_orca(fake_orca)
    configure_fake_orca(smoke_workspace.config_path, fake_orca)
    missing_ts_xtb = smoke_workspace.root / "bin" / "fake_xtb_missing_ts"
    _write_missing_ts_xtb(missing_ts_xtb)
    config = yaml.safe_load(smoke_workspace.config_path.read_text(encoding="utf-8"))
    config["workflow"]["paths"]["xtb_executable"] = str(missing_ts_xtb)
    smoke_workspace.config_path.write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )

    input_dir = smoke_workspace.root / "workflow_root" / "reaction_missing_ts_input"
    _write_xyz(input_dir / "reactant.xyz", comment="missing ts reactant", bond=0.74)
    _write_xyz(input_dir / "product.xyz", comment="missing ts product", bond=0.82)
    (input_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "max_crest_candidates: 1",
                "max_xtb_stages: 1",
                "max_orca_stages: 1",
                "resources:",
                "  max_cores: 1",
                "  max_memory_gb: 1",
                "orca:",
                '  route_line: "! r2scan-3c OptTS Freq TightSCF"',
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
        max_cycles=28,
    )

    assert payload["status"] == "failed"
    workflow_error = payload["metadata"]["workflow_error"]
    assert workflow_error["scope"] == "reaction_ts_search_xtb_handoff"
    assert workflow_error["reason"] == "xtb_ts_guess_missing"
    xtb_stages = _engine_stages(payload, "xtb")
    assert len(xtb_stages) == 1
    assert xtb_stages[0]["status"] == "completed"
    final_metadata = xtb_stages[0]["metadata"]
    assert final_metadata["reaction_handoff_status"] == "failed"
    assert final_metadata["reaction_handoff_reason"] == "xtb_ts_guess_missing"
    assert final_metadata["xtb_handoff_retries_used"] == 2
    assert final_metadata["xtb_handoff_retry_limit"] == 2
    assert _engine_stages(payload, "orca") == []
    assert len(list_queue(workspace_dir / "02_xtb")) == 3
    assert_workflow_report(workspace_dir, payload)
    assert not (workspace_dir / "workflow_si.md").exists()
    assert not (workspace_dir / "si_data.csv").exists()
