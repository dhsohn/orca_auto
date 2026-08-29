from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow.manifest import interaction_energy_config_fingerprint
from orca_auto.flow.restart import mutation as restart_mutation
from orca_auto.flow.restart import restart_failed_workflow
from tests.flow.restart_helpers import _failed_orca_restart_stage, _write_workflow


def test_restart_preserves_primary_stage_with_spoofed_interaction_role(
    tmp_path: Path,
) -> None:
    stage: dict[str, Any] = {
        "stage_id": "orca_primary_spoofed_role_failed",
        "stage_kind": "orca_stage",
        "status": "failed",
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "failed",
            "payload": {},
            "enqueue_payload": {"priority": 10},
        },
        "metadata": {"role": "interaction_fragment"},
    }
    payload: dict[str, Any] = {"stages": [stage], "metadata": {}}

    restarted = restart_mutation._reset_restartable_stages(
        payload,
        flow_settings={
            "applied": True,
            "priority": 4,
            "resources": {},
            "interaction_energy_disabled": False,
            "electronic_state_present": False,
            "orca_route_line_present": False,
            "orca_optts_route_line_present": False,
        },
        restart_allowed_root=tmp_path,
    )

    assert payload["stages"] == [stage]
    assert stage["status"] == "planned"
    assert stage["task"]["status"] == "planned"
    assert stage["task"]["enqueue_payload"]["priority"] == 4
    assert restarted[0]["stage_id"] == "orca_primary_spoofed_role_failed"


def test_disabling_interaction_energy_does_not_retire_spoofed_primary_stage(
    tmp_path: Path,
) -> None:
    parent: dict[str, Any] = {
        "stage_id": "orca_parent",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {"engine": "orca", "task_kind": "opt", "status": "completed"},
        "metadata": {},
    }
    stage: dict[str, Any] = {
        "stage_id": "orca_primary_spoofed_role_disable",
        "stage_kind": "orca_stage",
        "status": "failed",
        "task": {
            "engine": "orca",
            "task_kind": "sp",
            "status": "failed",
            "payload": {},
        },
        "metadata": {
            "role": "interaction_complex_sp",
            "parent_stage_id": "orca_parent",
            "interaction_config_fingerprint": "b" * 64,
        },
    }
    payload: dict[str, Any] = {"stages": [parent, stage], "metadata": {}}

    restarted = restart_mutation._reset_restartable_stages(
        payload,
        flow_settings={
            "applied": False,
            "interaction_energy_disabled": True,
            "persisted_interaction_energy_fingerprint": "a" * 64,
        },
        restart_allowed_root=tmp_path,
    )

    assert payload["stages"] == [parent, stage]
    assert stage["status"] == "planned"
    assert all(item.get("action") != "retired_disabled_interaction_energy" for item in restarted)


def test_disabling_interaction_energy_still_retires_valid_interaction_child(
    tmp_path: Path,
) -> None:
    parent: dict[str, Any] = {
        "stage_id": "orca_parent",
        "stage_kind": "orca_stage",
        "status": "completed",
        "task": {"engine": "orca", "task_kind": "opt", "status": "completed"},
        "metadata": {},
    }
    child: dict[str, Any] = {
        "stage_id": "orca_fragment_child",
        "stage_kind": "orca_stage",
        "status": "failed",
        "task": {"engine": "orca", "task_kind": "sp", "status": "failed", "payload": {}},
        "metadata": {
            "role": "interaction_fragment",
            "parent_stage_id": "orca_parent",
            "fragment_index": 0,
            "interaction_config_fingerprint": "a" * 64,
        },
    }
    payload: dict[str, Any] = {"stages": [parent, child], "metadata": {}}

    restarted = restart_mutation._reset_restartable_stages(
        payload,
        flow_settings={
            "applied": False,
            "interaction_energy_disabled": True,
            "persisted_interaction_energy_fingerprint": "a" * 64,
        },
        restart_allowed_root=tmp_path,
    )

    assert payload["stages"] == [parent]
    assert restarted == [
        {
            "stage_id": "orca_fragment_child",
            "previous_status": "failed",
            "previous_task_status": "failed",
            "engine": "orca",
            "action": "retired_disabled_interaction_energy",
        }
    ]


def test_restart_refuses_corrupt_journal_before_any_mutation(tmp_path: Path) -> None:
    # The restart journal append runs only after the mutation is durably
    # committed, so a journal the append would refuse previously made restart
    # report failure for a restart that had taken effect — and the retry then
    # failed with "no failed or cancelled stages to restart".
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_journal_guard"
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_journal_guard",
            "template_name": "reaction_ts_search",
            "status": "failed",
            "requested_at": "2026-04-27T00:00:00+00:00",
            "stages": [_failed_orca_restart_stage("orca_failed", reaction_dir)],
            "metadata": {"workflow_error": {"status": "failed", "reason": "boom"}},
        },
    )
    original = (workspace / "workflow.json").read_text(encoding="utf-8")
    journal_path = root / "workflow_registry.journal.jsonl"
    real_journal = tmp_path / "elsewhere.jsonl"
    real_journal.write_bytes(b"")
    journal_path.symlink_to(real_journal)

    with pytest.raises(ValueError, match="must not be a symlink"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    # Nothing changed: the durable payload is untouched, so the retry is not
    # poisoned into the no-restartable-stages refusal.
    assert (workspace / "workflow.json").read_text(encoding="utf-8") == original

    journal_path.unlink()
    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)
    assert result["status"] == "restarted"


def test_restart_disabling_interaction_energy_retires_existing_stages(tmp_path: Path) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_ie_disable"
    workspace.mkdir(parents=True)
    (workspace / "flow.yaml").write_text(
        "workflow_type: conformer_screening\ninteraction_energy:\n  enabled: false\n",
        encoding="utf-8",
    )
    interaction = {
        "enabled": True,
        "sp_route_line": "! HF TightSCF",
        "max_fragments": 2,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "fragment_a"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "fragment_b"},
        ],
    }
    fingerprint = interaction_energy_config_fingerprint(
        interaction,
        complex_charge=0,
        complex_multiplicity=1,
    )
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_ie_disable",
            "template_name": "conformer_screening",
            "status": "failed",
            "stages": [
                {
                    "stage_id": "orca_parent",
                    "stage_kind": "orca_stage",
                    "status": "completed",
                    "task": {
                        "engine": "orca",
                        "task_kind": "opt",
                        "status": "completed",
                    },
                    "metadata": {},
                },
                {
                    "stage_id": "ie_failed",
                    "stage_kind": "orca_stage",
                    "status": "failed",
                    "task": {
                        "engine": "orca",
                        "task_kind": "sp",
                        "status": "failed",
                        "payload": {},
                    },
                    "metadata": {
                        "role": "interaction_fragment",
                        "parent_stage_id": "orca_parent",
                        "fragment_index": 0,
                        "interaction_config_fingerprint": fingerprint,
                    },
                },
            ],
            "metadata": {"request": {"parameters": {"interaction_energy": interaction}}},
        },
    )

    result = restart_failed_workflow(workspace_dir=workspace, workflow_root=root)

    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert result["restarted_count"] == 1
    assert result["restarted_stages"][0]["action"] == "retired_disabled_interaction_energy"
    assert [stage["stage_id"] for stage in saved["stages"]] == ["orca_parent"]
    assert "interaction_energy" not in saved["metadata"]["request"]["parameters"]


def test_restart_rejects_primary_orca_reopen_after_interaction_fanout(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_ie_primary_restart"
    interaction = {
        "enabled": True,
        "sp_route_line": "! HF TightSCF",
        "max_fragments": 2,
        "fragments": [
            {"atom_indices": [0], "charge": 0, "multiplicity": 1, "label": "a"},
            {"atom_indices": [1], "charge": 0, "multiplicity": 1, "label": "b"},
        ],
    }
    fingerprint = interaction_energy_config_fingerprint(
        interaction,
        complex_charge=0,
        complex_multiplicity=1,
    )
    payload: dict[str, object] = {
        "workflow_id": "wf_ie_primary_restart",
        "template_name": "conformer_screening",
        "status": "failed",
        "stages": [
            {
                "stage_id": "orca_conf_failed",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {
                    "engine": "orca",
                    "task_kind": "opt",
                    "status": "failed",
                    "payload": {},
                },
                "metadata": {},
            },
            {
                "stage_id": "ie_existing",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "task_kind": "sp", "status": "completed"},
                "metadata": {
                    "role": "interaction_complex_sp",
                    "parent_stage_id": "orca_conf_failed",
                    "interaction_config_fingerprint": fingerprint,
                },
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "charge": 0,
                    "multiplicity": 1,
                    "interaction_energy": interaction,
                }
            }
        },
    }
    _write_workflow(workspace, payload)
    with pytest.raises(ValueError, match="cannot restart primary ORCA stages"):
        restart_failed_workflow(workspace_dir=workspace, workflow_root=root)
    assert json.loads((workspace / "workflow.json").read_text(encoding="utf-8")) == payload


def test_force_restart_rearms_blocked_si_publication_without_failed_stage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workflow_runs"
    workspace = root / "wf_si_blocked"
    _write_workflow(
        workspace,
        {
            "workflow_id": "wf_si_blocked",
            "template_name": "conformer_screening",
            "status": "completed",
            "stages": [],
            "metadata": {
                "si_publish_blocked": True,
                "si_publish_attempts": 5,
                "si_publish_error": "PermissionError: denied",
            },
        },
    )
    result = restart_failed_workflow(
        workspace_dir=workspace,
        workflow_root=root,
        force=True,
    )
    saved = json.loads((workspace / "workflow.json").read_text(encoding="utf-8"))
    assert result["restarted_stages"][0]["action"] == "rearmed_si_publication"
    assert saved["status"] == "planned"
    assert saved["metadata"]["si_publish_pending"] is True
    assert "si_publish_blocked" not in saved["metadata"]
    assert "si_publish_attempts" not in saved["metadata"]


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
        ("TS8_renamed", "TS8_original", "does not match persisted workflow_id"),
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
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW OptTS Freq'\n",
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
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW OptTS Freq'\n",
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
        "workflow_type: reaction_ts_search\norca:\n  route_line: '! NEW OptTS Freq'\n",
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
