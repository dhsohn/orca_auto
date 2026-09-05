from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from orca_auto.flow.contracts import OrcaArtifactContract
from orca_auto.flow.orchestration.stage_runtime.orca import sync_orca_stage_impl
from tests.flow.orchestration_services import orchestration_services


@pytest.mark.parametrize(
    ("route_line", "expected_status"),
    [
        ("! OptTS Freq r2scan-3c TightSCF", "completed"),
        ("! Opt r2scan-3c TightSCF", "failed"),
        ("! ScanTS Freq r2scan-3c TightSCF", "failed"),
        ("! NEB-TS Freq r2scan-3c TightSCF", "failed"),
        ("! OptTS NumFreq r2scan-3c TightSCF", "completed"),
    ],
)
def test_sync_orca_stage_binds_completed_contract_to_durable_task_role(
    tmp_path: Path,
    route_line: str,
    expected_status: str,
) -> None:
    selected_inp = tmp_path / "candidate.inp"
    selected_inp.write_text(f"{route_line}\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_optts_freq_01",
        "stage_kind": "orca_stage",
        "status": "submitted",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "optts_freq",
            "status": "submitted",
            "payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    contract = OrcaArtifactContract(
        run_id="run_role_binding",
        status="completed",
        reason="normal_termination",
        state_status="completed",
        reaction_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        selected_inp=str(selected_inp),
        analyzer_status="completed",
    )
    deps = orchestration_services(
        overrides={"load_orca_artifact_contract": Mock(return_value=contract)}
    )

    sync_orca_stage_impl(
        stage,
        orca_config=None,
        orca_repo_root=None,
        submit_ready=False,
        services=deps,
    )

    task = stage["task"]
    assert isinstance(task, dict)
    assert stage["status"] == expected_status
    assert task["status"] == expected_status
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    if expected_status == "failed":
        assert "route-role mismatch" in metadata["reason"]
        artifacts = stage["output_artifacts"]
        assert isinstance(artifacts, list)
        assert not any(
            artifact.get("selected")
            for artifact in artifacts
            if isinstance(artifact, dict)
            and artifact.get("kind") in {"orca_optimized_xyz", "orca_last_out"}
        )
    else:
        assert metadata["reason"] == "normal_termination"


def test_sync_interaction_sp_rejects_completed_non_single_point_input(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "completed_interaction.inp"
    selected_inp.write_text("! HF Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_completed_interaction_sp_mismatch",
        "stage_kind": "orca_stage",
        "status": "submitted",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "sp",
            "status": "submitted",
            "payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    contract = OrcaArtifactContract(
        run_id="run_completed_sp_mismatch",
        status="completed",
        reason="normal_termination",
        state_status="completed",
        reaction_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        selected_inp=str(selected_inp),
        analyzer_status="completed",
    )
    deps = orchestration_services(
        overrides={"load_orca_artifact_contract": Mock(return_value=contract)}
    )

    sync_orca_stage_impl(
        stage,
        orca_config=None,
        orca_repo_root=None,
        submit_ready=False,
        services=deps,
    )

    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "single-point" in metadata["reason"]


def test_sync_orca_rejects_unknown_task_kind_on_completed_contract(tmp_path: Path) -> None:
    selected_inp = tmp_path / "completed_unknown.inp"
    selected_inp.write_text("! HF Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_completed_unknown_kind",
        "stage_kind": "orca_stage",
        "status": "submitted",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "geometry_opt",
            "status": "submitted",
            "payload": {"reaction_dir": str(tmp_path), "selected_inp": str(selected_inp)},
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    contract = OrcaArtifactContract(
        run_id="run_completed_unknown_kind",
        status="completed",
        reason="normal_termination",
        state_status="completed",
        reaction_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        selected_inp=str(selected_inp),
        analyzer_status="completed",
    )
    deps = orchestration_services(
        overrides={"load_orca_artifact_contract": Mock(return_value=contract)}
    )

    sync_orca_stage_impl(
        stage,
        orca_config=None,
        orca_repo_root=None,
        submit_ready=False,
        services=deps,
    )

    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "unsupported workflow ORCA task_kind" in metadata["reason"]


@pytest.mark.parametrize(
    "route_line",
    (
        "! Opt HF",
        "! ScanTS Freq HF",
        "! NEB-TS Freq HF",
        '! "OptTS" Freq HF',
        '! OptTS "Freq" HF',
        "!! OptTS Freq HF",
        "!%pal OptTS Freq HF",
        "! OptTS Freq %pal nprocs 8 end",
        "! OptTS Freq * xyz 0 1",
    ),
)
def test_sync_orca_stage_rejects_role_mismatch_before_submit(
    tmp_path: Path,
    route_line: str,
) -> None:
    selected_inp = tmp_path / "candidate.inp"
    selected_inp.write_text(
        f"{route_line}\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    stage: dict[str, object] = {
        "stage_id": "orca_optts_freq_pre_submit_mismatch",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "optts_freq",
            "status": "planned",
            "payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
                "priority": 10,
            },
        },
    }
    submit = Mock(
        return_value={
            "status": "waiting_for_slot",
            "reason": "submission_conflict",
            "returncode": 0,
            "stderr": "already queued\n",
            "parsed_stdout": {},
        }
    )
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": Mock(return_value=None),
            "now_utc_iso": lambda: "2026-08-23T00:00:00+00:00",
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    submit.assert_not_called()
    task = stage["task"]
    metadata = stage["metadata"]
    assert isinstance(task, dict)
    assert isinstance(metadata, dict)
    assert stage["status"] == "failed"
    assert task["status"] == "failed"
    assert "route-role mismatch" in metadata["reason"]


@pytest.mark.parametrize(
    "route_line",
    (
        "! HF Opt TightSCF",
        "! HF OptTS TightSCF",
        "! HF Freq TightSCF",
        "! HF IRC TightSCF",
        "! HF Engrad TightSCF",
        "! HF NumGrad TightSCF",
        "! HF MD TightSCF",
        "! HF NEB-CI TightSCF",
        "! HF GOAT TightSCF",
    ),
)
def test_sync_interaction_sp_rejects_non_single_point_route_before_submit(
    tmp_path: Path,
    route_line: str,
) -> None:
    selected_inp = tmp_path / "interaction.inp"
    selected_inp.write_text(
        f"{route_line}\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    stage: dict[str, object] = {
        "stage_id": "orca_interaction_sp_mismatch",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {
            "role": "interaction_complex_sp",
            "parent_stage_id": "orca_parent_opt",
        },
        "task": {
            "engine": "orca",
            "task_kind": "sp",
            "status": "planned",
            "payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
                "priority": 10,
            },
        },
    }
    submit = Mock(return_value={"status": "submitted", "queue_id": "q_sp_mismatch"})
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": Mock(return_value=None),
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    submit.assert_not_called()
    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "single-point" in metadata["reason"]


def test_sync_interaction_sp_requires_durable_selected_input_before_submit(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "interaction.inp"
    selected_inp.write_text("! HF TightSCF\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_interaction_sp_missing_durable_selection",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "sp",
            "status": "planned",
            "payload": {"reaction_dir": str(tmp_path)},
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    submit = Mock(return_value={"status": "submitted", "queue_id": "q_sp_missing"})
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": Mock(return_value=None),
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    submit.assert_not_called()
    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "durable selected_inp" in metadata["reason"]


def test_sync_orca_role_stage_requires_durable_selected_input_before_submit(
    tmp_path: Path,
) -> None:
    selected_inp = tmp_path / "actual.inp"
    selected_inp.write_text("! Opt HF\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_opt_missing_durable_selection",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "planned",
            "payload": {"reaction_dir": str(tmp_path)},
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    submit = Mock(return_value={"status": "submitted", "queue_id": "q_missing"})
    load_contract = Mock(return_value=None)
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": load_contract,
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    submit.assert_not_called()
    load_contract.assert_not_called()
    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "durable selected_inp" in metadata["reason"]


@pytest.mark.parametrize("divergence", ("selected_inp", "reaction_dir"))
def test_sync_orca_role_stage_rejects_divergent_durable_submission_paths(
    tmp_path: Path,
    divergence: str,
) -> None:
    task_dir = tmp_path / "task_dir"
    enqueue_dir = tmp_path / "enqueue_dir"
    task_dir.mkdir()
    enqueue_dir.mkdir()
    task_inp = task_dir / "candidate.inp"
    task_inp.write_text("! Opt HF\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    if divergence == "selected_inp":
        enqueue_inp = task_dir / "z_candidate.inp"
        enqueue_reaction_dir = task_dir
    else:
        enqueue_inp = enqueue_dir / "candidate.inp"
        enqueue_reaction_dir = enqueue_dir
    enqueue_inp.write_text("! Opt HF\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": f"orca_opt_divergent_{divergence}",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "planned",
            "payload": {
                "reaction_dir": str(task_dir),
                "selected_inp": str(task_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(enqueue_reaction_dir),
                "selected_inp": str(enqueue_inp),
                "priority": 10,
            },
        },
    }
    submit = Mock(return_value={"status": "submitted", "queue_id": "q_divergent"})
    load_contract = Mock(return_value=None)
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": load_contract,
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    submit.assert_not_called()
    load_contract.assert_not_called()
    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "durable" in metadata["reason"]
    assert "differ" in metadata["reason"]


def test_sync_orca_submission_and_lookup_share_canonical_reaction_dir(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "canonical_dir"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "candidate.inp"
    selected_inp.write_text("! Opt HF\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_opt_canonical_binding",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "planned",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": f"{reaction_dir}/.",
                "selected_inp": f"{reaction_dir}/./candidate.inp",
                "priority": 10,
            },
        },
    }
    submit = Mock(return_value={"status": "submitted", "queue_id": "q_canonical"})
    load_contract = Mock(return_value=None)
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": load_contract,
            "now_utc_iso": lambda: "2026-08-23T00:00:00+00:00",
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    canonical_dir = str(reaction_dir.resolve(strict=True))
    assert submit.call_args.kwargs["reaction_dir"] == canonical_dir
    assert submit.call_args.kwargs["expected_selected_inp"] == str(
        selected_inp.resolve(strict=True)
    )
    assert submit.call_args.kwargs["workflow_task_kind"] == "opt"
    assert load_contract.call_args.kwargs["reaction_dir"] == canonical_dir
    assert load_contract.call_args.kwargs["target"] == canonical_dir


@pytest.mark.parametrize(
    ("task_kind", "selected_input_state", "expected_reason_suffix"),
    [
        ("opt", "missing_selection", "selected input path is missing"),
        ("optts_freq", "missing_contract_selection", "selected input path is missing"),
        ("optts_freq", "missing_file", "candidate.inp'"),
        ("relaxed_scan", "directory", "candidate.inp'"),
        ("opt", "no_route", "candidate.inp'"),
        ("relaxed_scan", "missing_scan_block", "candidate.inp'"),
    ],
)
def test_sync_orca_stage_fails_closed_when_route_role_cannot_be_verified(
    tmp_path: Path,
    task_kind: str,
    selected_input_state: str,
    expected_reason_suffix: str,
) -> None:
    selected_inp = tmp_path / "candidate.inp"
    payload_selected_inp = str(selected_inp)
    contract_selected_inp = str(selected_inp)
    if selected_input_state == "missing_selection":
        payload_selected_inp = ""
        contract_selected_inp = ""
    elif selected_input_state == "missing_contract_selection":
        selected_inp.write_text(
            "! OptTS Freq r2scan-3c TightSCF\n* xyz 0 1\nH 0 0 0\n*\n",
            encoding="utf-8",
        )
        contract_selected_inp = ""
    elif selected_input_state == "directory":
        selected_inp.mkdir()
    elif selected_input_state == "no_route":
        selected_inp.write_text("%pal nprocs 1 end\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    elif selected_input_state == "missing_scan_block":
        selected_inp.write_text(
            "! Opt r2scan-3c TightSCF\n* xyz 0 1\nH 0 0 0\n*\n",
            encoding="utf-8",
        )

    stage: dict[str, object] = {
        "stage_id": f"orca_{task_kind}_unverified",
        "stage_kind": "orca_stage",
        "status": "submitted",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": task_kind,
            "status": "submitted",
            "payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": payload_selected_inp,
            },
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    contract = OrcaArtifactContract(
        run_id="run_unverified_role",
        status="completed",
        reason="normal_termination",
        state_status="completed",
        reaction_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        selected_inp=contract_selected_inp,
        analyzer_status="completed",
    )
    deps = orchestration_services(
        overrides={"load_orca_artifact_contract": Mock(return_value=contract)}
    )

    sync_orca_stage_impl(
        stage,
        orca_config=None,
        orca_repo_root=None,
        submit_ready=False,
        services=deps,
    )

    task = stage["task"]
    assert isinstance(task, dict)
    assert stage["status"] == "failed"
    assert task["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["reason"].startswith("workflow ORCA route-role mismatch: ")
    assert metadata["reason"].endswith(expected_reason_suffix)
    artifacts = stage["output_artifacts"]
    assert isinstance(artifacts, list)
    assert not any(
        artifact.get("selected")
        for artifact in artifacts
        if isinstance(artifact, dict)
        and artifact.get("kind") in {"orca_optimized_xyz", "orca_last_out"}
    )


@pytest.mark.parametrize(
    ("coordinate", "expected_reason"),
    [
        ("B 0 1 = 1, 2, 1", "points"),
        ("B 0 99 = 1, 2, 8", "within input XYZ"),
    ],
)
def test_sync_orca_stage_rejects_invalid_relaxed_scan_coordinate_contract(
    tmp_path: Path,
    coordinate: str,
    expected_reason: str,
) -> None:
    selected_inp = tmp_path / "invalid_scan.inp"
    selected_inp.write_text(
        "\n".join(
            [
                "! Opt HF",
                "%geom",
                "  Scan",
                f"    {coordinate}",
                "  end",
                "end",
                "* xyz 0 1",
                "H 0 0 0",
                "H 0 0 0.7",
                "*",
                "",
            ]
        ),
        encoding="utf-8",
    )
    stage: dict[str, object] = {
        "stage_id": "orca_relaxed_scan_invalid_contract",
        "stage_kind": "orca_stage",
        "status": "submitted",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "relaxed_scan",
            "status": "submitted",
            "payload": {
                "reaction_dir": str(tmp_path),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {"reaction_dir": str(tmp_path), "priority": 10},
        },
    }
    contract = OrcaArtifactContract(
        run_id="run_invalid_scan_contract",
        status="completed",
        reason="normal_termination",
        state_status="completed",
        reaction_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        selected_inp=str(selected_inp),
        analyzer_status="completed",
    )
    deps = orchestration_services(
        overrides={"load_orca_artifact_contract": Mock(return_value=contract)}
    )

    sync_orca_stage_impl(
        stage,
        orca_config=None,
        orca_repo_root=None,
        submit_ready=False,
        services=deps,
    )

    task = stage["task"]
    metadata = stage["metadata"]
    assert isinstance(task, dict)
    assert isinstance(metadata, dict)
    assert stage["status"] == "failed"
    assert task["status"] == "failed"
    assert expected_reason in metadata["reason"]


def test_sync_orca_stage_applies_contract_state_metadata_and_artifacts(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn_done"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "rxn.inp"
    selected_inp.write_text(
        "! HF Opt\n* xyz 0 1\nH 0 0 0\n*\n",
        encoding="utf-8",
    )
    stage: dict[str, object] = {
        "stage_id": "orca_opt_01",
        "stage_kind": "orca_stage",
        "status": "submitted",
        "metadata": {"queue_id": "q_123"},
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "submitted",
            "payload": {"reaction_dir": "/tmp/rxn_pending", "selected_inp": ""},
            "enqueue_payload": {"reaction_dir": "/tmp/rxn_pending", "priority": 10},
        },
    }
    contract = OrcaArtifactContract(
        run_id="run_123",
        status="completed",
        reason="normal_termination",
        state_status="completed",
        reaction_dir="/tmp/rxn_done",
        latest_known_path="/tmp/rxn_done",
        optimized_xyz_path="/tmp/orca_outputs/opt/H2/run_123/rxn.xyz",
        queue_id="q_123",
        queue_status="completed",
        cancel_requested=False,
        selected_inp=str(selected_inp),
        selected_input_xyz="/tmp/rxn_done/rxn.xyz",
        analyzer_status="completed",
        completed_at="2026-04-19T00:00:00+00:00",
        last_out_path="/tmp/rxn_done/rxn.out",
        run_state_path="/tmp/rxn_done/job_state.json",
        report_json_path="/tmp/rxn_done/job_report.json",
        attempt_count=2,
        attempts=(
            {
                "index": 2,
                "attempt_number": 1,
                "inp_path": "/tmp/rxn_done/rxn.retry01.inp",
                "out_path": "/tmp/rxn_done/rxn.retry01.out",
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
                "markers": [],
                "patch_actions": [],
                "started_at": "2026-04-19T00:00:00+00:00",
                "ended_at": "2026-04-19T00:10:00+00:00",
            },
        ),
        final_result={
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "2026-04-19T00:10:00+00:00",
        },
        resource_request={"max_cores": 8, "max_memory_gb": 16},
        resource_actual={"max_cores": 8, "max_memory_gb": 16},
    )

    mock_load = Mock(return_value=contract)
    deps = orchestration_services(overrides={"load_orca_artifact_contract": mock_load})
    sync_orca_stage_impl(
        stage,
        orca_config=None,
        orca_repo_root=None,
        submit_ready=False,
        services=deps,
    )

    assert isinstance(stage["task"], dict)
    assert isinstance(stage["metadata"], dict)
    task = stage["task"]
    metadata = stage["metadata"]
    assert isinstance(task.get("payload"), dict)
    payload = task["payload"]

    assert stage["status"] == "completed"
    assert task["status"] == "completed"
    assert payload["selected_inp"] == contract.selected_inp
    assert payload["selected_input_xyz"] == contract.selected_input_xyz
    assert payload["last_out_path"] == contract.last_out_path
    assert payload["optimized_xyz_path"] == contract.optimized_xyz_path
    assert payload["orca_latest_attempt_inp"] == "/tmp/rxn_done/rxn.retry01.inp"
    assert payload["orca_latest_attempt_out"] == "/tmp/rxn_done/rxn.retry01.out"

    assert metadata["queue_id"] == "q_123"
    assert metadata["run_id"] == "run_123"
    assert metadata["queue_status"] == "completed"
    assert metadata["latest_known_path"] == contract.latest_known_path
    assert metadata["optimized_xyz_path"] == contract.optimized_xyz_path
    assert metadata["attempt_count"] == 2
    assert "max_retries" not in metadata
    assert metadata["orca_latest_attempt_number"] == 1
    assert metadata["orca_latest_attempt_status"] == "completed"
    assert metadata["orca_final_result"]["reason"] == "normal_termination"

    assert isinstance(stage.get("output_artifacts"), list)
    output_artifacts = stage["output_artifacts"]
    assert isinstance(output_artifacts, list)
    artifact_dicts = [artifact for artifact in output_artifacts if isinstance(artifact, dict)]
    artifact_kinds = {artifact["kind"] for artifact in artifact_dicts if "kind" in artifact}
    assert artifact_kinds == {
        "orca_selected_inp",
        "orca_selected_input_xyz",
        "orca_optimized_xyz",
        "orca_last_out",
        "orca_run_state",
        "orca_report_json",
        "orca_output_dir",
    }
    mock_load.assert_called_once()


def test_sync_orca_stage_rejects_unknown_task_kind_before_submission(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn_unknown_kind"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "job.inp"
    selected_inp.write_text("! HF Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_unknown_kind",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "geometry_opt",
            "status": "planned",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "priority": 10,
            },
        },
    }
    submit = Mock(return_value={"status": "submitted", "queue_id": "q_unknown"})
    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": submit,
            "load_orca_artifact_contract": Mock(return_value=None),
        }
    )

    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    submit.assert_not_called()
    assert stage["status"] == "failed"
    metadata = stage["metadata"]
    assert isinstance(metadata, dict)
    assert "unsupported workflow ORCA task_kind" in metadata["reason"]


def test_sync_orca_stage_leaves_submission_conflict_planned(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn_conflict"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "job.inp"
    selected_inp.write_text("! HF Opt TightSCF\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    stage: dict[str, object] = {
        "stage_id": "orca_opt_conflict",
        "stage_kind": "orca_stage",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "orca",
            "task_kind": "opt",
            "status": "planned",
            "payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
            },
            "enqueue_payload": {
                "reaction_dir": str(reaction_dir),
                "selected_inp": str(selected_inp),
                "priority": 10,
            },
        },
    }

    def fake_submit_reaction_dir(**_kwargs: object) -> dict[str, object]:
        return {
            "status": "waiting_for_slot",
            "reason": "submission_conflict",
            "returncode": 0,
            "stderr": "already queued\n",
            "parsed_stdout": {},
        }

    deps = orchestration_services(
        overrides={
            "submit_reaction_dir": fake_submit_reaction_dir,
            "load_orca_artifact_contract": Mock(return_value=None),
            "now_utc_iso": lambda: "2026-04-19T00:00:00+00:00",
        }
    )
    sync_orca_stage_impl(
        stage,
        orca_config="/tmp/orca.yaml",
        orca_repo_root=None,
        submit_ready=True,
        services=deps,
    )

    assert isinstance(stage["task"], dict)
    assert isinstance(stage["metadata"], dict)
    task = stage["task"]
    metadata = stage["metadata"]
    assert stage["status"] == "planned"
    assert task["status"] == "planned"
    assert metadata == {
        "submission_status": "waiting_for_slot",
        "submission_deferred_reason": "submission_conflict",
        "last_submission_attempt_at": "2026-04-19T00:00:00+00:00",
    }
    assert task["submission_result"]["status"] == "waiting_for_slot"
    assert task["submission_result"]["submitted_at"] == "2026-04-19T00:00:00+00:00"
