from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.flow.manifest import interaction_energy_config_fingerprint
from orca_auto.flow.restart import restart_failed_workflow
from tests.flow.restart_helpers import _failed_orca_restart_stage, _write_workflow


def test_restart_failed_workflow_resets_failed_and_cancelled_stages(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_failed"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_failed",
            "template_name": "conformer_screening",
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
                        "submission_intent_token": "stale-restart-intent",
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
                    "metadata": {
                        "queue_id": "q_cancelled",
                        "child_job_id": "crest_old",
                        "crest_rejected_retained_outputs": [
                            {"name": "crest_conformers.xyz", "reason": "no_valid_frames"}
                        ],
                        "crest_no_primary_ensemble_retained": True,
                    },
                },
            ],
            "metadata": {
                "workflow_error": {"status": "failed", "reason": "boom"},
                "final_child_sync_pending": True,
                "phase_notifications": {
                    "crest_summary": {"sent_at": "2026-04-27T00:00:00+00:00"},
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
    assert "phase_notifications" not in saved["metadata"]
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
    assert "submission_intent_token" not in restarted_orca["metadata"]
    assert "last_out_path" not in restarted_orca["task"]["payload"]

    restarted_crest = saved["stages"][2]
    assert restarted_crest["status"] == "planned"
    assert restarted_crest["task"]["status"] == "planned"
    assert "cancel_result" not in restarted_crest["task"]
    assert "child_job_id" not in restarted_crest["metadata"]
    # The refusal names a job directory the restart just rematerialized, so it
    # is cleared with the other contract-derived keys instead of describing the
    # new attempt until a fresh contract happens to load.
    assert "crest_rejected_retained_outputs" not in restarted_crest["metadata"]
    assert "crest_no_primary_ensemble_retained" not in restarted_crest["metadata"]
    assert restarted_crest["task"]["payload"]["job_dir"] == "/tmp/crest"
    assert restarted_crest["task"]["enqueue_payload"]["job_dir"] == "/tmp/crest"

    registry = json.loads((root / "workflow_registry.json").read_text(encoding="utf-8"))
    assert registry[0]["workflow_id"] == "wf_failed"
    assert registry[0]["status"] == "planned"
    journal = (root / "workflow_registry.journal.jsonl").read_text(encoding="utf-8")
    assert "workflow_restarted" in journal


def test_restart_preserves_interaction_fragment_sp_route_state_and_resources(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_ie_restart"
    reaction_dir = workspace / "03_orca" / "fragment"
    reaction_dir.mkdir(parents=True)
    (workspace / "input.xyz").write_text("2\ncomplex\nCl 0 0 0\nNa 2.5 0 0\n", encoding="utf-8")
    (reaction_dir / "input.xyz").write_text("1\nfragment\nCl 0 0 0\n", encoding="utf-8")
    (reaction_dir / "input.inp").write_text(
        "! r2scan-3c TightSCF\n* xyzfile -1 1 input.xyz\n", encoding="utf-8"
    )
    interaction = {
        "enabled": True,
        "sp_route_line": "! r2scan-3c TightSCF",
        "max_fragments": 2,
        "priority": 4,
        "max_cores": 3,
        "max_memory_gb": 9,
        "fragments": [
            {"atom_indices": [0], "charge": -1, "multiplicity": 1, "label": "anion"},
            {"atom_indices": [1], "charge": 1, "multiplicity": 1, "label": "cation"},
        ],
    }
    rmsd_dedup = {
        "enabled": True,
        "rmsd_threshold_angstrom": 0.18,
        "energy_window_kcal": 0.25,
        "heavy_atoms_only": False,
    }
    fingerprint = interaction_energy_config_fingerprint(
        interaction,
        complex_charge=0,
        complex_multiplicity=1,
        rmsd_dedup=rmsd_dedup,
    )
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "orca:",
                "  route_line: '! r2scan-3c Opt TightSCF'",
                "  charge: 0",
                "  multiplicity: 1",
                "interaction_energy:",
                "  enabled: true",
                "  sp_route_line: '! r2scan-3c TightSCF'",
                "  max_fragments: 2",
                "  priority: 4",
                "  max_cores: 3",
                "  max_memory_gb: 9",
                "  fragments:",
                "    - atom_indices: [0]",
                "      charge: -1",
                "      multiplicity: 1",
                "      label: anion",
                "    - atom_indices: [1]",
                "      charge: 1",
                "      multiplicity: 1",
                "      label: cation",
                "rmsd_dedup:",
                "  enabled: true",
                "  rmsd_threshold_angstrom: 0.18",
                "  energy_window_kcal: 0.25",
                "  heavy_atoms_only: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    stage = _failed_orca_restart_stage("ie_fragment", reaction_dir)
    stage["stage_kind"] = "orca_stage"
    stage_task = stage["task"]
    assert isinstance(stage_task, dict)
    stage_task["task_kind"] = "sp"
    stage["metadata"] = {
        "role": "interaction_fragment",
        "parent_stage_id": "orca_conf_01",
        "fragment_index": 0,
        "fragment_label": "anion",
        "fragment_charge": -1,
        "fragment_multiplicity": 1,
        "fragment_atom_indices": [0],
        "interaction_config_fingerprint": fingerprint,
    }
    parent_stage: dict[str, object] = {
        "stage_id": "orca_conf_01",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {"engine": "orca", "task_kind": "opt", "status": "completed"},
        "metadata": {},
    }
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_ie_restart",
            "template_name": "conformer_screening",
            "status": "failed",
            "stages": [parent_stage, stage],
            "metadata": {
                "request": {
                    "parameters": {
                        "charge": 0,
                        "multiplicity": 1,
                        "orca_route_line": "! r2scan-3c Opt TightSCF",
                        "interaction_energy": interaction,
                        "rmsd_dedup": rmsd_dedup,
                    }
                }
            },
        },
    )

    restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    restarted = saved["stages"][1]
    restarted_dir = workspace / "03_orca" / "fragment.restart-001"
    restarted_text = (restarted_dir / "input.inp").read_text(encoding="utf-8")
    assert "! r2scan-3c TightSCF" in restarted_text
    assert " Opt " not in restarted_text
    assert "* xyzfile -1 1 input.xyz" in restarted_text
    assert "nprocs 3" in restarted_text
    assert "%maxcore 3072" in restarted_text
    assert restarted["task"]["resource_request"] == {"max_cores": 3, "max_memory_gb": 9}
    assert restarted["task"]["enqueue_payload"]["priority"] == 4


@pytest.mark.parametrize(("fragment_multiplicity", "accepted"), [(1, False), (2, True)])
def test_force_restart_validates_fragment_electron_state_against_copied_input(
    tmp_path: Path,
    fragment_multiplicity: int,
    accepted: bool,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / f"wf_restart_h2_m{fragment_multiplicity}"
    copied_input = workspace / "inputs" / "molecule.xyz"
    copied_input.parent.mkdir(parents=True)
    copied_input.write_text("2\nH2\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "interaction_energy:",
                "  enabled: true",
                "  fragments:",
                "    - atom_indices: [0]",
                "      charge: 0",
                f"      multiplicity: {fragment_multiplicity}",
                "      label: h_a",
                "    - atom_indices: [1]",
                "      charge: 0",
                f"      multiplicity: {fragment_multiplicity}",
                "      label: h_b",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    original: dict[str, object] = {
        "workflow_id": workspace.name,
        "template_name": "conformer_screening",
        "status": "completed",
        "stages": [],
        "metadata": {
            "si_publish_blocked": True,
            "si_publish_attempts": 5,
            "request": {
                "parameters": {"charge": 0, "multiplicity": 1},
                "source_artifacts": [
                    {"kind": "input_xyz", "path": str(copied_input), "selected": True}
                ],
            },
        },
    }
    _write_workflow(workspace, original)

    if not accepted:
        with pytest.raises(ValueError, match="wrong parity"):
            restart_failed_workflow(workspace_dir=workspace, workflow_root=root, force=True)
        assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == original
        return

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root, force=True)
    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert result["restarted_stages"][0]["action"] == "rearmed_si_publication"
    fragments = saved["metadata"]["request"]["parameters"]["interaction_energy"]["fragments"]
    assert [fragment["multiplicity"] for fragment in fragments] == [2, 2]


def test_restart_cancelled_workflow_resets_cancelled_stages(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_cancelled"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_cancelled",
            "template_name": "conformer_screening",
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
                "workflow_type: conformer_screening",
                "crest_mode: nci",
                "priority: 0",
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
            "template_name": "conformer_screening",
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
        "gfn": "ff",
        "no_preopt": True,
        "noreftopo": True,
        "notopo": True,
        "nocbonds": True,
    }
    assert result["status"] == "restarted"
    assert saved["metadata"]["restart_summary"]["flow_manifest_applied"] is True
    assert task["resource_request"] == {"max_cores": 3, "max_memory_gb": 11}
    assert task["enqueue_payload"]["priority"] == 0
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
    assert params["priority"] == 0
    assert params["max_cores"] == 3
    assert params["max_memory_gb"] == 11
    assert params["boltzmann_temperature_k"] == pytest.approx(310.0)
    assert params["crest_job_manifest"] == expected_overrides


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
        "workflow_type: conformer_screening\norca:\n  route_line: '! NEW Opt Freq'\n",
        encoding="utf-8",
    )
    original: dict[str, object] = {
        "workflow_id": "wf_external_orca",
        "template_name": "conformer_screening",
        "status": "failed",
        "stages": [_failed_orca_restart_stage("orca_failed", outside)],
        "metadata": {},
    }
    _write_workflow(workspace, original)

    with pytest.raises(ValueError, match="reaction directory escapes"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    assert not (tmp_path / "external_orca.restart-001").exists()
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == original


def test_restart_applies_electronic_state_change_without_engine_sections(tmp_path: Path) -> None:
    # A flow.yaml that changes ONLY the electronic state (the scaffolded
    # layout needs no crest: section) must still reach the engine
    # stages: without the electronic_state gate the presence flags stay
    # False, the old overrides survive, the job dir is not rebuilt, and the
    # restarted stages rerun the previous neutral/singlet manifest.
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_charge_only_restart"
    (workspace / "old_crest").mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
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
            "template_name": "conformer_screening",
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
            ],
            "metadata": {"request": {"parameters": {}}},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    (crest_stage,) = saved["stages"]
    crest_task = crest_stage["task"]
    params = saved["metadata"]["request"]["parameters"]

    assert result["restarted_count"] == 1
    assert params["charge"] == -1
    assert params["multiplicity"] == 2

    # Existing overrides keep their keys; only the electronic state is added.
    crest_overrides = {"charge": -1, "uhf": 1, "rthr": 0.3, "ewin": 8}
    assert crest_task["payload"]["job_manifest_overrides"] == crest_overrides
    assert crest_task["metadata"]["job_manifest_overrides"] == crest_overrides
    assert crest_stage["metadata"]["job_manifest_overrides"] == crest_overrides

    # The manifest changed, so the CREST stage rebuilds its job directory.
    assert crest_task["payload"]["job_dir"] == ""


def test_restart_electronic_state_when_workflow_json_has_no_request_block(
    tmp_path: Path,
) -> None:
    # An older/hand-edited workflow.json may lack metadata.request.parameters.
    # A charge-only flow.yaml must still create the params and inject the
    # electronic state into rematerialized stages: otherwise
    # the missing params default to charge 0 / uhf 0 and strip it.
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_no_request_block"
    (workspace / "old_crest").mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
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
            "template_name": "conformer_screening",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_conformer_01",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {
                            "job_dir": str(workspace / "old_crest"),
                            "job_manifest_overrides": {"gfn": 1},
                        },
                        "metadata": {"job_manifest_overrides": {"gfn": 1}},
                        "enqueue_payload": {"job_dir": str(workspace / "old_crest")},
                    },
                    "metadata": {"job_manifest_overrides": {"gfn": 1}},
                },
            ],
            "metadata": {},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    crest_task = saved["stages"][0]["task"]
    params = saved["metadata"]["request"]["parameters"]

    assert result["restarted_count"] == 1
    # The params structure is created and carries the electronic state, so
    # later appends see it too.
    assert params["charge"] == -1
    assert params["multiplicity"] == 2
    # And the rematerialized CREST stage keeps its manifest key plus the state.
    assert crest_task["payload"]["job_manifest_overrides"] == {"charge": -1, "uhf": 1, "gfn": 1}
    assert crest_task["payload"]["job_dir"] == ""


def test_restart_rejects_engine_state_conflicting_with_canonical_workflow_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_conflicting_restart_state"
    old_crest = workspace / "old_crest"
    old_crest.mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "charge: -1",
                "orca:",
                "  multiplicity: 2",
                "crest:",
                "  charge: 0",
                "  uhf: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_conflicting_restart_state",
            "template_name": "conformer_screening",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [
                {
                    "stage_id": "crest_conformer_01",
                    "status": "failed",
                    "task": {
                        "engine": "crest",
                        "status": "failed",
                        "payload": {
                            "job_dir": str(old_crest),
                            "job_manifest_overrides": {"gfn": 1},
                        },
                        "enqueue_payload": {"job_dir": str(old_crest)},
                    },
                    "metadata": {},
                }
            ],
            "metadata": {"request": {"parameters": {}}},
        },
    )

    with pytest.raises(
        ValueError,
        match="engine manifest charge=0 conflicts with workflow charge=-1",
    ):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)
