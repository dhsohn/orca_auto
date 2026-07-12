from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto import cli_handlers as cli_run_dir
from orca_auto.flow.cli import run_dir as flow_cli
from orca_auto.flow.restart import restart_failed_workflow


def _write_workflow(workspace: Path, payload: dict[str, object]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "workflow.json").write_text(json.dumps(payload), encoding="utf-8")


def _failed_orca_restart_stage(stage_id: str, reaction_dir: Path) -> dict[str, object]:
    selected_inp = reaction_dir / "input.inp"
    selected_xyz = reaction_dir / "input.xyz"
    return {
        "stage_id": stage_id,
        "status": "failed",
        "task": {
            "engine": "orca",
            "status": "failed",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "selected_input_xyz": str(selected_xyz),
            },
            "enqueue_payload": {
                "submitter": "orca_auto_orca",
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
        },
        "metadata": {},
    }


def test_restart_failed_workflow_resets_failed_and_cancelled_stages(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_failed"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_failed",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_done",
                    "status": "completed",
                    "output_artifacts": [{"kind": "crest_conformer", "path": "/tmp/done.xyz"}],
                    "task": {
                        "engine": "crest",
                        "status": "completed",
                        "payload": {},
                        "enqueue_payload": {},
                    },
                    "metadata": {"queue_id": "q_done"},
                },
                {
                    "stage_id": "orca_failed",
                    "status": "failed",
                    "output_artifacts": [{"kind": "orca_last_out", "path": "/tmp/old.out"}],
                    "task": {
                        "engine": "orca",
                        "status": "failed",
                        "submission_result": {"status": "submitted", "queue_id": "q_old"},
                        "payload": {"reaction_dir": "/tmp/rxn", "last_out_path": "/tmp/old.out"},
                        "enqueue_payload": {
                            "submitter": "orca_auto_orca",
                            "reaction_dir": "/tmp/rxn",
                            "priority": 10,
                            "force": False,
                        },
                    },
                    "metadata": {
                        "queue_id": "q_old",
                        "run_id": "run_old",
                        "reason": "orca_crash",
                        "latest_known_path": "/tmp/rxn",
                    },
                },
                {
                    "stage_id": "crest_cancelled",
                    "status": "cancelled",
                    "task": {
                        "engine": "crest",
                        "status": "cancelled",
                        "cancel_result": {"status": "cancelled"},
                        "payload": {"job_dir": "/tmp/crest"},
                        "enqueue_payload": {"job_dir": "/tmp/crest", "priority": 10},
                    },
                    "metadata": {"queue_id": "q_cancelled", "child_job_id": "crest_old"},
                },
            ],
            "metadata": {
                "workflow_error": {"status": "failed", "reason": "boom"},
                "final_child_sync_pending": True,
                "phase_notifications": {
                    "crest_summary": {"sent_at": "2026-04-27T00:00:00+00:00"},
                    "xtb_summary": {"sent_at": "2026-04-27T01:00:00+00:00"},
                },
            },
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert result["status"] == "restarted"
    assert result["workflow_status"] == "planned"
    assert result["restarted_count"] == 2
    assert saved["status"] == "planned"
    assert "workflow_error" not in saved["metadata"]
    assert saved["metadata"]["restart_summary"]["restarted_count"] == 2
    assert saved["metadata"]["phase_notifications"] == {
        "xtb_summary": {"sent_at": "2026-04-27T01:00:00+00:00"}
    }
    assert saved["stages"][0]["status"] == "completed"
    assert saved["stages"][0]["output_artifacts"] == [
        {"kind": "crest_conformer", "path": "/tmp/done.xyz"}
    ]

    restarted_orca = saved["stages"][1]
    assert restarted_orca["status"] == "planned"
    assert restarted_orca["task"]["status"] == "planned"
    assert "submission_result" not in restarted_orca["task"]
    assert restarted_orca["output_artifacts"] == []
    assert restarted_orca["task"]["enqueue_payload"]["force"] is True
    assert "queue_id" not in restarted_orca["metadata"]
    assert "last_out_path" not in restarted_orca["task"]["payload"]

    restarted_crest = saved["stages"][2]
    assert restarted_crest["status"] == "planned"
    assert restarted_crest["task"]["status"] == "planned"
    assert "cancel_result" not in restarted_crest["task"]
    assert "child_job_id" not in restarted_crest["metadata"]
    assert restarted_crest["task"]["payload"]["job_dir"] == "/tmp/crest"
    assert restarted_crest["task"]["enqueue_payload"]["job_dir"] == "/tmp/crest"

    registry = json.loads((root / "workflow_registry.json").read_text(encoding="utf-8"))
    assert registry[0]["workflow_id"] == "wf_failed"
    assert registry[0]["status"] == "planned"
    journal = (root / "workflow_registry.journal.jsonl").read_text(encoding="utf-8")
    assert "workflow_restarted" in journal


def test_restart_failed_workflow_rejects_active_sibling_before_cancellation_finishes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_half_failed"
    original_payload: dict[str, object] = {
        "workflow_id": "wf_half_failed",
        "template_name": "reaction_ts_search",
        "status": "failed",
        "requested_at": "2026-04-27T00:00:00+00:00",
        "stages": [
            {
                "stage_id": "crest_product",
                "status": "failed",
                "task": {
                    "engine": "crest",
                    "status": "failed",
                    "payload": {"job_dir": "/tmp/product"},
                    "enqueue_payload": {"job_dir": "/tmp/product", "priority": 10},
                },
                "metadata": {"queue_id": "q_product"},
            },
            {
                "stage_id": "crest_reactant",
                "status": "running",
                "task": {
                    "engine": "crest",
                    "status": "running",
                    "payload": {"job_dir": "/tmp/reactant"},
                    "enqueue_payload": {"job_dir": "/tmp/reactant", "priority": 10},
                },
                "metadata": {"queue_id": "q_reactant"},
            },
        ],
        "metadata": {
            "workflow_error": {"status": "failed", "reason": "product_failed"},
        },
    }
    _write_workflow(workspace, original_payload)

    with pytest.raises(ValueError, match="workflow still has active stages"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert saved == original_payload
    assert not (root / "workflow_registry.json").exists()
    assert not (root / "workflow_registry.journal.jsonl").exists()


@pytest.mark.parametrize(
    ("directory_name", "workflow_id", "error_text"),
    [
        ("TS8(wf)", "TS8(wf)", "cannot contain parentheses"),
        ("TS8_wf", "TS8(wf)", "does not match persisted workflow_id"),
    ],
)
def test_restart_rejects_parenthesized_or_renamed_workflow_without_mutation(
    tmp_path: Path,
    directory_name: str,
    workflow_id: str,
    error_text: str,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / directory_name
    original_payload: dict[str, object] = {
        "workflow_id": workflow_id,
        "template_name": "reaction_ts_search",
        "status": "failed",
        "stages": [],
        "metadata": {},
    }
    _write_workflow(workspace, original_payload)
    queue_path = workspace / "01_crest" / "queue.json"
    queue_path.parent.mkdir()
    queue_path.write_text('[{"queue_id":"q_existing","status":"failed"}]', encoding="utf-8")
    workflow_before = (workspace / "workflow.json").read_bytes()
    queue_before = queue_path.read_bytes()

    with pytest.raises(ValueError, match=error_text):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    assert (workspace / "workflow.json").read_bytes() == workflow_before
    assert queue_path.read_bytes() == queue_before
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))["status"] == (
        "failed"
    )
    assert not (root / "workflow_registry.json").exists()
    assert not (root / "workflow_registry.journal.jsonl").exists()


def test_flow_run_dir_reports_renamed_existing_workflow_without_restarting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "TS8_wf"
    _write_workflow(
        workspace,
        {
            "workflow_id": "TS8(wf)",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "stages": [],
            "metadata": {},
        },
    )
    workflow_before = (workspace / "workflow.json").read_bytes()

    rc = flow_cli.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workspace),
            workflow_root=str(root),
            force=False,
            json=False,
        )
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "does not match persisted workflow_id 'TS8(wf)'" in captured.err
    assert "Renaming an existing workflow directory is not supported" in captured.err
    assert (workspace / "workflow.json").read_bytes() == workflow_before
    assert not (root / "workflow_registry.json").exists()
    assert not (root / "workflow_registry.journal.jsonl").exists()


def test_restart_cancelled_workflow_resets_cancelled_stages(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_cancelled"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_cancelled",
            "template_name": "reaction_ts_search",
            "status": "cancelled",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_product",
                    "status": "cancelled",
                    "task": {
                        "engine": "crest",
                        "status": "cancelled",
                        "cancel_result": {"status": "cancelled"},
                        "payload": {"job_dir": "/tmp/product"},
                        "enqueue_payload": {"job_dir": "/tmp/product", "priority": 10},
                    },
                    "metadata": {"queue_id": "q_product", "child_job_id": "crest_product_old"},
                },
                {
                    "stage_id": "crest_reactant",
                    "status": "completed",
                    "task": {
                        "engine": "crest",
                        "status": "completed",
                        "payload": {"job_dir": "/tmp/reactant"},
                        "enqueue_payload": {"job_dir": "/tmp/reactant", "priority": 10},
                    },
                    "metadata": {"queue_id": "q_reactant"},
                    "output_artifacts": [
                        {"kind": "crest_conformer", "path": "/tmp/reactant/conf.xyz"}
                    ],
                },
            ],
            "metadata": {"final_child_sync_pending": False},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert result["status"] == "restarted"
    assert result["previous_status"] == "cancelled"
    assert result["restarted_count"] == 1
    restarted_stage = saved["stages"][0]
    assert restarted_stage["status"] == "planned"
    assert restarted_stage["task"]["status"] == "planned"
    assert "cancel_result" not in restarted_stage["task"]
    assert "queue_id" not in restarted_stage["metadata"]
    assert saved["stages"][1]["status"] == "completed"
    assert saved["metadata"]["restart_summary"]["previous_status"] == "cancelled"


def test_restart_failed_workflow_reloads_flow_yaml_for_crest_stage(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_flow_yaml_refresh"
    (workspace / "old_crest").mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "crest_mode: nci",
                "priority: 4",
                "boltzmann_temperature_k: 310.0",
                "resources:",
                "  max_cores: 3",
                "  max_memory_gb: 11",
                "crest:",
                "  gfn: ff",
                "  no_preopt: true",
                "  noreftopo: true",
                "  notopo: true",
                "  nocbonds: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_flow_yaml_refresh",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_product_01",
                    "stage_kind": "crest_stage",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "resource_request": {"max_cores": 8, "max_memory_gb": 32},
                        "payload": {
                            "source_input_xyz": str(
                                workspace / "inputs" / "products" / "product.xyz"
                            ),
                            "selected_input_xyz": str(workspace / "old_crest" / "input.xyz"),
                            "job_dir": str(workspace / "old_crest"),
                            "mode": "standard",
                            "job_manifest_overrides": {"rthr": 0.3},
                        },
                        "enqueue_payload": {
                            "job_dir": str(workspace / "old_crest"),
                            "priority": 10,
                        },
                        "metadata": {"mode": "standard", "job_manifest_overrides": {"rthr": 0.3}},
                    },
                    "metadata": {
                        "mode": "standard",
                        "job_manifest_overrides": {"rthr": 0.3},
                        "queue_id": "q_old",
                    },
                    "output_artifacts": [],
                }
            ],
            "metadata": {
                "request": {
                    "parameters": {
                        "crest_mode": "standard",
                        "priority": 10,
                        "max_cores": 8,
                        "max_memory_gb": 32,
                        "crest_job_manifest": {"rthr": 0.3},
                    }
                }
            },
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    stage = saved["stages"][0]
    task = stage["task"]
    expected_overrides = {
        "rthr": 0.3,
        "gfn": "ff",
        "no_preopt": True,
        "noreftopo": True,
        "notopo": True,
        "nocbonds": True,
    }
    assert result["status"] == "restarted"
    assert saved["metadata"]["restart_summary"]["flow_manifest_applied"] is True
    assert task["resource_request"] == {"max_cores": 3, "max_memory_gb": 11}
    assert task["enqueue_payload"]["priority"] == 4
    assert task["enqueue_payload"]["job_dir"] == ""
    assert task["payload"]["job_dir"] == ""
    assert task["payload"]["selected_input_xyz"] == ""
    assert task["payload"]["mode"] == "nci"
    assert task["payload"]["job_manifest_overrides"] == expected_overrides
    assert task["metadata"]["mode"] == "nci"
    assert task["metadata"]["job_manifest_overrides"] == expected_overrides
    assert stage["metadata"]["mode"] == "nci"
    assert stage["metadata"]["job_manifest_overrides"] == expected_overrides
    params = saved["metadata"]["request"]["parameters"]
    assert params["crest_mode"] == "nci"
    assert params["priority"] == 4
    assert params["max_cores"] == 3
    assert params["max_memory_gb"] == 11
    assert params["boltzmann_temperature_k"] == pytest.approx(310.0)
    assert params["crest_job_manifest"] == expected_overrides


def test_restart_failed_workflow_reloads_xtb_orca_and_endpoint_manifest_settings(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_flow_yaml_xtb_orca"
    (workspace / "controls").mkdir(parents=True)
    (workspace / "controls" / "path.inp").write_text("$path\n$end\n", encoding="utf-8")
    old_orca = workspace / "old_orca"
    old_orca.mkdir(parents=True)
    old_orca_xyz = old_orca / "input.xyz"
    old_orca_inp = old_orca / "input.inp"
    old_orca_xyz.write_text("2\nold\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    old_orca_inp.write_text(
        "\n".join(
            [
                "! OLD-METHOD Opt",
                "%pal",
                "  nprocs 1",
                "end",
                "%maxcore 1024",
                "%geom",
                "  MaxIter 99",
                "end",
                "* xyzfile 0 1 input.xyz",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (old_orca / "input.out").write_text("old output", encoding="utf-8")
    (old_orca / "job_state.json").write_text('{"old": true}', encoding="utf-8")
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "priority: 5",
                "max_crest_candidates: 4",
                "max_xtb_stages: 3",
                "max_xtb_handoff_retries: 2",
                "max_orca_stages: 6",
                "resources:",
                "  max_cores: 7",
                "  max_memory_gb: 21",
                "xtb:",
                "  gfn: 2",
                "  xcontrol_file: controls/path.inp",
                "  endpoint_pairing:",
                "    strategy: from_xtb_section",
                "    max_pairs: 2",
                "endpoint_pairing:",
                "  max_pairs: 5",
                "  direction: both",
                "orca:",
                "  route_line: '! PBE0 def2-SVP'",
                "  multiplicity: 2",
                "charge: -1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_flow_yaml_xtb_orca",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "xtb_failed",
                    "status": "failed",
                    "task": {
                        "engine": "xtb",
                        "status": "failed",
                        "resource_request": {"max_cores": 1},
                        "payload": {
                            "job_dir": str(workspace / "old_xtb"),
                            "selected_input_xyz": str(workspace / "old_xtb" / "reactant.xyz"),
                            "secondary_input_xyz": str(workspace / "old_xtb" / "product.xyz"),
                            "job_manifest_overrides": {"gfn": 1},
                        },
                        "enqueue_payload": {
                            "job_dir": str(workspace / "old_xtb"),
                            "priority": 10,
                            "command": (
                                "orca_auto.flow.engines.xtb.submission.direct_enqueue "
                                "config=<xtb_config> job_dir=<job_dir> priority=10"
                            ),
                            "command_argv": [
                                "orca_auto.flow.engines.xtb.submission.direct_enqueue",
                                "config=<xtb_config>",
                                "job_dir=<job_dir>",
                                "priority=10",
                            ],
                        },
                    },
                    "metadata": {"queue_id": "q_xtb"},
                    "output_artifacts": [{"kind": "xtb_result", "path": "/tmp/old_xtb"}],
                },
                {
                    "stage_id": "orca_failed",
                    "status": "failed",
                    "task": {
                        "engine": "orca",
                        "status": "failed",
                        "resource_request": {"max_cores": 1, "max_memory_gb": 1},
                        "payload": {
                            "reaction_dir": str(old_orca),
                            "selected_inp": str(old_orca_inp),
                            "selected_input_xyz": str(old_orca_xyz),
                        },
                        "enqueue_payload": {
                            "submitter": "orca_auto_orca",
                            "reaction_dir": str(old_orca),
                            "selected_inp": str(old_orca_inp),
                            "priority": 10,
                            "command": f"orca_auto run-dir '{old_orca}' --priority 10",
                            "command_argv": [
                                "python",
                                "-m",
                                "orca_auto",
                                "run-dir",
                                str(old_orca),
                                "--priority",
                                "10",
                            ],
                        },
                        "metadata": {
                            "reaction_dir": str(old_orca),
                            "selected_inp": str(old_orca_inp),
                        },
                    },
                    "metadata": {"queue_id": "q_orca", "reaction_dir": str(old_orca)},
                    "output_artifacts": [{"kind": "orca_out", "path": "/tmp/old.out"}],
                },
            ],
            "metadata": {"request": {"parameters": {}}},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    xtb_stage = saved["stages"][0]
    xtb_task = xtb_stage["task"]
    orca_task = saved["stages"][1]["task"]
    params = saved["metadata"]["request"]["parameters"]
    xcontrol_path = str((workspace / "controls" / "path.inp").resolve())
    xtb_overrides = {"gfn": 2, "xcontrol_file": xcontrol_path}
    # Rematerialized stages must carry the electronic state: a restart that
    # replaced the stage overrides with the raw flow.yaml xtb section would
    # rerun the charged doublet as a neutral singlet (Codex P2 on #55).
    xtb_stage_overrides = {"charge": -1, "uhf": 1, **xtb_overrides}

    assert result["restarted_count"] == 2
    assert saved["metadata"]["restart_summary"]["flow_manifest_applied"] is True
    assert xtb_task["resource_request"] == {"max_cores": 7, "max_memory_gb": 21}
    assert xtb_task["enqueue_payload"]["priority"] == 5
    assert xtb_task["enqueue_payload"]["command"] == (
        "orca_auto.flow.engines.xtb.submission.direct_enqueue "
        "config=<xtb_config> job_dir=<job_dir> priority=5"
    )
    assert xtb_task["enqueue_payload"]["command_argv"] == [
        "orca_auto.flow.engines.xtb.submission.direct_enqueue",
        "config=<xtb_config>",
        "job_dir=<job_dir>",
        "priority=5",
    ]
    assert xtb_task["enqueue_payload"]["job_dir"] == ""
    assert xtb_task["payload"]["job_dir"] == ""
    assert xtb_task["payload"]["selected_input_xyz"] == ""
    assert xtb_task["payload"]["secondary_input_xyz"] == ""
    assert xtb_task["payload"]["job_manifest_overrides"] == xtb_stage_overrides
    assert xtb_task["metadata"]["job_manifest_overrides"] == xtb_stage_overrides
    assert xtb_stage["metadata"]["job_manifest_overrides"] == xtb_stage_overrides
    assert orca_task["resource_request"] == {"max_cores": 7, "max_memory_gb": 21}
    assert orca_task["enqueue_payload"]["priority"] == 5
    restarted_orca = workspace / "old_orca.restart-001"
    restarted_inp = restarted_orca / "input.inp"
    restarted_xyz = restarted_orca / "input.xyz"
    assert orca_task["enqueue_payload"]["reaction_dir"] == str(restarted_orca)
    assert orca_task["enqueue_payload"]["selected_inp"] == str(restarted_inp)
    assert orca_task["enqueue_payload"]["max_cores"] == 7
    assert orca_task["enqueue_payload"]["max_memory_gb"] == 21
    assert orca_task["enqueue_payload"]["command_argv"] == [
        "python",
        "-m",
        "orca_auto",
        "run-dir",
        str(restarted_orca),
        "--priority",
        "5",
    ]
    assert orca_task["enqueue_payload"]["force"] is True
    assert orca_task["payload"]["reaction_dir"] == str(restarted_orca)
    assert orca_task["payload"]["selected_inp"] == str(restarted_inp)
    assert orca_task["payload"]["selected_input_xyz"] == str(restarted_xyz)
    restarted_text = restarted_inp.read_text(encoding="utf-8")
    assert "! PBE0 def2-SVP" in restarted_text
    assert "nprocs 7" in restarted_text
    assert "%maxcore 3072" in restarted_text
    assert "%geom\n  MaxIter 99\nend" in restarted_text
    assert "* xyzfile -1 2 input.xyz" in restarted_text
    assert restarted_xyz.read_text(encoding="utf-8") == old_orca_xyz.read_text(encoding="utf-8")
    assert not (restarted_orca / "input.out").exists()
    assert not (restarted_orca / "job_state.json").exists()
    assert "! OLD-METHOD Opt" in old_orca_inp.read_text(encoding="utf-8")
    provenance = json.loads((restarted_orca / "source_candidate.json").read_text())[
        "restart_provenance"
    ]
    assert provenance["previous_reaction_dir"] == str(old_orca)
    persisted_enqueue = json.loads((restarted_orca / "enqueue_payload.json").read_text())
    assert persisted_enqueue["force"] is True
    assert persisted_enqueue["reaction_dir"] == str(restarted_orca)

    assert params["priority"] == 5
    assert params["max_cores"] == 7
    assert params["max_memory_gb"] == 21
    assert params["max_crest_candidates"] == 4
    assert params["max_xtb_stages"] == 3
    assert params["max_xtb_handoff_retries"] == 2
    assert params["max_orca_stages"] == 6
    assert params["xtb_job_manifest"] == xtb_overrides
    assert "crest_job_manifest" not in params
    assert params["endpoint_pairing"] == {
        "strategy": "from_xtb_section",
        "max_pairs": 5,
        "direction": "both",
    }
    assert params["orca_route_line"] == "! PBE0 def2-SVP"
    assert params["charge"] == -1
    assert params["multiplicity"] == 2


def test_restart_cleans_created_orca_dir_when_workflow_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_commit_failure"
    reaction_dir = workspace / "orca_stage"
    reaction_dir.mkdir(parents=True)
    (reaction_dir / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (reaction_dir / "input.inp").write_text("! OLD\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    (workspace / "flow.yaml").write_text(
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW'\n",
        encoding="utf-8",
    )
    original: dict[str, object] = {
        "workflow_id": "wf_commit_failure",
        "template_name": "reaction_ts_search",
        "status": "failed",
        "stages": [_failed_orca_restart_stage("orca_failed", reaction_dir)],
        "metadata": {},
    }
    _write_workflow(workspace, original)

    def fail_commit(_workspace: Path, _payload: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("orca_auto.flow.restart.mutation.write_workflow_payload", fail_commit)

    with pytest.raises(OSError, match="disk full"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    assert not (workspace / "orca_stage.restart-001").exists()
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == original


def test_restart_rejects_orca_reaction_dir_outside_workflow_workspace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_external_orca"
    outside = tmp_path / "external_orca"
    outside.mkdir()
    (outside / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (outside / "input.inp").write_text(
        "! OLD\n* xyzfile 0 1 input.xyz\n",
        encoding="utf-8",
    )
    workspace.mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW'\n",
        encoding="utf-8",
    )
    original: dict[str, object] = {
        "workflow_id": "wf_external_orca",
        "template_name": "reaction_ts_search",
        "status": "failed",
        "stages": [_failed_orca_restart_stage("orca_failed", outside)],
        "metadata": {},
    }
    _write_workflow(workspace, original)

    with pytest.raises(ValueError, match="reaction directory escapes"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    assert not (tmp_path / "external_orca.restart-001").exists()
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == original


def test_restart_preserves_orca_dir_when_workflow_commit_visibility_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.flow.state import write_workflow_payload as real_write_workflow_payload

    root = tmp_path / "workflow_runs"
    workspace = root / "wf_ambiguous_commit"
    reaction_dir = workspace / "orca_stage"
    reaction_dir.mkdir(parents=True)
    (reaction_dir / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (reaction_dir / "input.inp").write_text(
        "! OLD\n* xyzfile 0 1 input.xyz\n",
        encoding="utf-8",
    )
    (workspace / "flow.yaml").write_text(
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW'\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_ambiguous_commit",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "stages": [_failed_orca_restart_stage("orca_failed", reaction_dir)],
            "metadata": {},
        },
    )

    def visible_then_fail(commit_workspace: Path, payload: dict[str, object]) -> None:
        real_write_workflow_payload(commit_workspace, payload)
        raise OSError("parent fsync failed after replace")

    def fail_visibility_check(_workspace: Path) -> dict[str, object]:
        raise OSError("transient read failure")

    monkeypatch.setattr(
        "orca_auto.flow.restart.mutation.write_workflow_payload",
        visible_then_fail,
    )
    monkeypatch.setattr(
        "orca_auto.flow.restart.mutation.load_workflow_payload",
        fail_visibility_check,
    )

    with pytest.raises(OSError, match="parent fsync failed after replace"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    restarted = workspace / "orca_stage.restart-001"
    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert saved["stages"][0]["task"]["payload"]["reaction_dir"] == str(restarted)
    assert restarted.is_dir()
    assert (restarted / "input.inp").is_file()


def test_restart_cleans_prior_orca_dirs_when_later_rematerialization_fails(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_later_failure"
    valid_dir = workspace / "orca_valid"
    valid_dir.mkdir(parents=True)
    (valid_dir / "input.xyz").write_text("1\nsource\nH 0 0 0\n", encoding="utf-8")
    (valid_dir / "input.inp").write_text("! OLD\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    missing_dir = workspace / "orca_missing"
    (workspace / "flow.yaml").write_text(
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW'\n",
        encoding="utf-8",
    )
    original: dict[str, object] = {
        "workflow_id": "wf_later_failure",
        "template_name": "reaction_ts_search",
        "status": "failed",
        "stages": [
            _failed_orca_restart_stage("orca_valid", valid_dir),
            _failed_orca_restart_stage("orca_missing", missing_dir),
        ],
        "metadata": {},
    }
    _write_workflow(workspace, original)

    with pytest.raises(FileNotFoundError, match="reaction_dir not found"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    assert not (workspace / "orca_valid.restart-001").exists()
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == original


def test_restart_failed_workflow_rejects_non_mapping_flow_yaml(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_bad_manifest"
    workspace.mkdir(parents=True)
    (workspace / "flow.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_bad_manifest",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "xtb_failed",
                    "status": "failed",
                    "task": {
                        "engine": "xtb",
                        "status": "failed",
                        "payload": {},
                        "enqueue_payload": {},
                    },
                    "metadata": {},
                }
            ],
            "metadata": {},
        },
    )

    with pytest.raises(ValueError, match="Workflow manifest must contain a mapping"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)


def test_flow_run_dir_reports_invalid_flow_yaml_during_restart(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_invalid_manifest"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_invalid_manifest",
            "template_name": "conformer_screening",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_failed",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {},
                        "enqueue_payload": {},
                    },
                    "metadata": {},
                }
            ],
            "metadata": {},
        },
    )
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "# orca_auto workflow scaffold manifest",
                "workflow_type: conformer_screening",
                "crest_mode: nci",
                "# Optional CREST job overrides.",
                " crest:",
                "   gfn: ff",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = flow_cli.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workspace),
            workflow_root=None,
            force=False,
            json=False,
        )
    )

    assert rc == 1
    stderr = capsys.readouterr().err
    assert "Invalid Workflow manifest" in stderr
    assert "flow.yaml" in stderr
    assert "line 5, column 2" in stderr


def test_flow_run_dir_restarts_existing_workflow_workspace_without_flow_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_existing"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_existing",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_failed",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {"job_dir": "/tmp/crest"},
                        "enqueue_payload": {"job_dir": "/tmp/crest", "priority": 10},
                    },
                    "metadata": {"queue_id": "q_failed"},
                }
            ],
            "metadata": {},
        },
    )

    rc = flow_cli.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workspace),
            workflow_root=None,
            force=False,
            json=False,
        )
    )

    assert rc == 0
    stdout = capsys.readouterr().out
    assert "workflow_id: wf_existing" in stdout
    assert "status: restarted" in stdout
    assert "restarted_count: 1" in stdout


def test_unified_run_dir_detects_existing_workflow_json_without_flow_yaml(tmp_path: Path) -> None:
    workspace = tmp_path / "wf_existing"
    _write_workflow(workspace, {"workflow_id": "wf_existing", "status": "failed", "stages": []})

    assert cli_run_dir._detect_run_dir_app(Namespace(path=str(workspace))) == "workflow"


def test_restart_applies_electronic_state_change_without_engine_sections(tmp_path: Path) -> None:
    # A flow.yaml that changes ONLY the electronic state (the scaffolded
    # layout needs no crest:/xtb: section) must still reach the engine
    # stages: without the electronic_state gate the presence flags stay
    # False, the old overrides survive, the job dir is not rebuilt, and the
    # restarted stages rerun the previous neutral/singlet manifest (Codex P2
    # follow-up on #55).
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_charge_only_restart"
    (workspace / "old_crest").mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "charge: -1",
                "orca:",
                "  multiplicity: 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_charge_only_restart",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_reactant_01",
                    "stage_kind": "crest_stage",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {
                            "job_dir": str(workspace / "old_crest"),
                            "selected_input_xyz": str(workspace / "old_crest" / "input.xyz"),
                            "mode": "standard",
                            "job_manifest_overrides": {"rthr": 0.3, "ewin": 8},
                        },
                        "metadata": {
                            "mode": "standard",
                            "job_manifest_overrides": {"rthr": 0.3, "ewin": 8},
                        },
                        "enqueue_payload": {"job_dir": str(workspace / "old_crest")},
                    },
                    "metadata": {
                        "mode": "standard",
                        "job_manifest_overrides": {"rthr": 0.3, "ewin": 8},
                    },
                },
                {
                    "stage_id": "xtb_path_01",
                    "status": "failed",
                    "task": {
                        "engine": "xtb",
                        "status": "failed",
                        "payload": {
                            "job_dir": str(workspace / "old_xtb"),
                            "job_manifest_overrides": {"gfn": 1},
                        },
                        "metadata": {"job_manifest_overrides": {"gfn": 1}},
                        "enqueue_payload": {"job_dir": str(workspace / "old_xtb")},
                    },
                    "metadata": {"job_manifest_overrides": {"gfn": 1}},
                },
            ],
            "metadata": {"request": {"parameters": {}}},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    crest_stage, xtb_stage = saved["stages"]
    crest_task = crest_stage["task"]
    xtb_task = xtb_stage["task"]
    params = saved["metadata"]["request"]["parameters"]

    assert result["restarted_count"] == 2
    assert params["charge"] == -1
    assert params["multiplicity"] == 2

    # Existing overrides keep their keys; only the electronic state is added.
    crest_overrides = {"charge": -1, "uhf": 1, "rthr": 0.3, "ewin": 8}
    assert crest_task["payload"]["job_manifest_overrides"] == crest_overrides
    assert crest_task["metadata"]["job_manifest_overrides"] == crest_overrides
    assert crest_stage["metadata"]["job_manifest_overrides"] == crest_overrides

    xtb_overrides = {"charge": -1, "uhf": 1, "gfn": 1}
    assert xtb_task["payload"]["job_manifest_overrides"] == xtb_overrides
    assert xtb_task["metadata"]["job_manifest_overrides"] == xtb_overrides
    assert xtb_stage["metadata"]["job_manifest_overrides"] == xtb_overrides

    # The manifest changed, so both stages rebuild their job dirs.
    assert crest_task["payload"]["job_dir"] == ""
    assert xtb_task["payload"]["job_dir"] == ""


def test_restart_electronic_state_when_workflow_json_has_no_request_block(
    tmp_path: Path,
) -> None:
    # An older/hand-edited workflow.json may lack metadata.request.parameters.
    # A charge-only flow.yaml must still create the params and inject the
    # electronic state into rematerialized stages (Codex P2 on #55): otherwise
    # the missing params default to charge 0 / uhf 0 and strip it.
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_no_request_block"
    (workspace / "old_xtb").mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "charge: -1",
                "orca:",
                "  multiplicity: 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_no_request_block",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "xtb_path_01",
                    "status": "failed",
                    "task": {
                        "engine": "xtb",
                        "status": "failed",
                        "payload": {
                            "job_dir": str(workspace / "old_xtb"),
                            "job_manifest_overrides": {"gfn": 1},
                        },
                        "metadata": {"job_manifest_overrides": {"gfn": 1}},
                        "enqueue_payload": {"job_dir": str(workspace / "old_xtb")},
                    },
                    "metadata": {"job_manifest_overrides": {"gfn": 1}},
                },
            ],
            "metadata": {},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    xtb_task = saved["stages"][0]["task"]
    params = saved["metadata"]["request"]["parameters"]

    assert result["restarted_count"] == 1
    # The params structure is created and carries the electronic state, so
    # later appends see it too.
    assert params["charge"] == -1
    assert params["multiplicity"] == 2
    # And the rematerialized xTB stage keeps its manifest key plus the state.
    assert xtb_task["payload"]["job_manifest_overrides"] == {"charge": -1, "uhf": 1, "gfn": 1}
    assert xtb_task["payload"]["job_dir"] == ""
