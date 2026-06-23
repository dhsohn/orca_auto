from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.flow.orchestration.deps import orchestration_deps
from orca_auto.flow.orchestration.stage_runtime.crest import (
    ensure_crest_job_dir_impl,
    sync_crest_stage_impl,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_path_jobs import ensure_xtb_job_dir_impl


def test_ensure_crest_job_dir_copies_input_and_populates_manifest(tmp_path: Path) -> None:
    source_xyz = tmp_path / "inputs" / "complex.xyz"
    source_xyz.parent.mkdir(parents=True, exist_ok=True)
    source_xyz.write_text("2\ncomplex\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    stage: dict[str, Any] = {
        "stage_id": "crest_nci_01",
        "task": {
            "resource_request": {"max_cores": 10, "max_memory_gb": 48},
            "payload": {
                "source_input_xyz": str(source_xyz),
                "job_dir": "",
                "selected_input_xyz": "",
                "mode": "nci",
            },
            "enqueue_payload": {"job_dir": ""},
        },
    }

    job_dir = ensure_crest_job_dir_impl(
        stage,
        crest_allowed_root=tmp_path / "crest_allowed",
        workflow_id="wf_ensure_crest",
    )

    job_path = Path(job_dir)
    manifest = (job_path / "crest_job.yaml").read_text(encoding="utf-8")
    assert job_path == tmp_path / "crest_allowed" / "crest_nci_01"
    assert (job_path / "input.xyz").exists()
    assert "mode: nci" in manifest
    assert "max_cores: 10" in manifest
    assert "max_memory_gb: 48" in manifest
    assert "input_xyz: input.xyz" in manifest
    assert stage["task"]["payload"]["job_dir"] == str(job_path)
    assert stage["task"]["payload"]["selected_input_xyz"] == str(job_path / "input.xyz")
    assert stage["task"]["enqueue_payload"]["job_dir"] == str(job_path)

    assert ensure_crest_job_dir_impl(
        stage,
        crest_allowed_root=tmp_path / "crest_allowed",
        workflow_id="wf_ensure_crest",
    ) == str(job_path)


def test_ensure_xtb_job_dir_returns_existing_or_generated_job_dir(
    tmp_path: Path,
) -> None:
    existing_stage = {
        "task": {
            "payload": {"job_dir": "/tmp/already_there"},
        }
    }
    assert (
        ensure_xtb_job_dir_impl(
            existing_stage,
            xtb_allowed_root=tmp_path / "xtb_allowed",
            workflow_id="wf_existing",
        )
        == "/tmp/already_there"
    )

    delegated_stage = {
        "task": {
            "payload": {"job_dir": ""},
        }
    }
    calls: list[tuple[str, int]] = []

    def fake_write_xtb_path_job(
        stage: dict[str, Any],
        *,
        xtb_allowed_root: Path,
        workflow_id: str,
        attempt_number: int,
    ) -> str:
        calls.append((workflow_id, attempt_number))
        return "/tmp/generated_xtb_job"

    deps = orchestration_deps(overrides={"_write_xtb_path_job": fake_write_xtb_path_job})

    assert (
        ensure_xtb_job_dir_impl(
            delegated_stage,
            xtb_allowed_root=tmp_path / "xtb_allowed",
            workflow_id="wf_generated",
            deps=deps,
        )
        == "/tmp/generated_xtb_job"
    )
    assert calls == [("wf_generated", 0)]


def test_sync_crest_stage_ignores_non_dict_task_and_non_crest_engine(tmp_path: Path) -> None:
    stage_without_task = {"task": "bad"}
    stage_xtb = {"task": {"engine": "xtb", "status": "planned"}}

    sync_crest_stage_impl(
        stage_without_task,
        crest_config="/tmp/crest.yaml",
        submit_ready=True,
        workflow_id="wf_01",
        workspace_dir=tmp_path / "workspace" / "wf_01",
    )
    sync_crest_stage_impl(
        stage_xtb,
        crest_config="/tmp/crest.yaml",
        submit_ready=True,
        workflow_id="wf_01",
        workspace_dir=tmp_path / "workspace" / "wf_01",
    )

    assert stage_without_task == {"task": "bad"}
    assert stage_xtb == {"task": {"engine": "xtb", "status": "planned"}}


def test_sync_crest_stage_submits_and_materializes_retained_conformers(
    tmp_path: Path,
) -> None:
    contract = SimpleNamespace(
        status="completed",
        job_id="crest_job_01",
        latest_known_path="/tmp/crest_done",
        organized_output_dir="/tmp/crest_outputs/run_01",
        selected_input_xyz="/tmp/crest_done/input.xyz",
        retained_conformer_paths=(
            "/tmp/crest_done/conf_01.xyz",
            "/tmp/crest_done/conf_02.xyz",
        ),
        mode="nci",
    )
    stage: dict[str, Any] = {
        "stage_id": "crest_nci_01",
        "status": "planned",
        "metadata": {},
        "task": {
            "engine": "crest",
            "status": "planned",
            "payload": {"job_dir": "", "selected_input_xyz": ""},
            "enqueue_payload": {"priority": 8},
        },
    }

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: tmp_path / "crest_allowed",
            "_ensure_crest_job_dir": lambda stage, **kwargs: str(
                tmp_path / "crest_allowed" / "wf_01" / "job_01"
            ),
            "submit_crest_job_dir": lambda **kwargs: {
                "status": "submitted",
                "queue_id": "q_crest_01",
                "job_id": "crest_job_01",
            },
            "now_utc_iso": lambda: "2026-04-19T16:20:00+00:00",
            "load_crest_artifact_contract": lambda **kwargs: contract,
        }
    )

    sync_crest_stage_impl(
        stage,
        crest_config="/tmp/crest.yaml",
        submit_ready=True,
        workflow_id="wf_01",
        workspace_dir=tmp_path / "workspace" / "wf_01",
        deps=deps,
    )

    task = cast(dict[str, Any], stage["task"])
    metadata = cast(dict[str, Any], stage["metadata"])
    assert stage["status"] == "completed"
    assert task["status"] == "completed"
    assert task["submission_result"]["queue_id"] == "q_crest_01"
    assert task["submission_result"]["submitted_at"] == "2026-04-19T16:20:00+00:00"
    assert task["payload"]["selected_input_xyz"] == "/tmp/crest_done/input.xyz"
    assert metadata["queue_id"] == "q_crest_01"
    assert metadata["child_job_id"] == "crest_job_01"
    assert metadata["latest_known_path"] == "/tmp/crest_done"
    assert metadata["organized_output_dir"] == "/tmp/crest_outputs/run_01"
    assert stage["output_artifacts"] == [
        {
            "kind": "crest_conformer",
            "path": "/tmp/crest_done/conf_01.xyz",
            "selected": True,
            "metadata": {"rank": 1, "mode": "nci"},
        },
        {
            "kind": "crest_conformer",
            "path": "/tmp/crest_done/conf_02.xyz",
            "selected": False,
            "metadata": {"rank": 2, "mode": "nci"},
        },
    ]


def test_sync_crest_stage_returns_without_target_when_not_submitted_and_no_queue_id(
    tmp_path: Path,
) -> None:
    stage: dict[str, Any] = {
        "stage_id": "crest_nci_02",
        "status": "planned",
        "task": {
            "engine": "crest",
            "status": "planned",
            "payload": {"job_dir": "", "selected_input_xyz": ""},
            "enqueue_payload": {"priority": 5},
        },
    }

    sync_crest_stage_impl(
        stage,
        crest_config=None,
        submit_ready=False,
        workflow_id="wf_02",
        workspace_dir=tmp_path / "workspace" / "wf_02",
    )

    assert stage["status"] == "planned"
    assert stage["task"]["status"] == "planned"
    assert "output_artifacts" not in stage


def test_sync_crest_stage_returns_cleanly_when_contract_lookup_is_missing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stage: dict[str, Any] = {
        "stage_id": "crest_nci_03",
        "status": "submitted",
        "metadata": {"queue_id": "q_existing"},
        "task": {
            "engine": "crest",
            "status": "submitted",
            "payload": {"job_dir": str(tmp_path / "job_dir"), "selected_input_xyz": ""},
            "enqueue_payload": {"priority": 5},
        },
    }

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: tmp_path / "crest_allowed",
            "load_crest_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                FileNotFoundError("not materialized yet")
            ),
        }
    )
    caplog.set_level(logging.DEBUG, logger="orca_auto.flow.orchestration.stage_runtime.shared")

    sync_crest_stage_impl(
        stage,
        crest_config="/tmp/crest.yaml",
        submit_ready=False,
        workflow_id="wf_03",
        workspace_dir=tmp_path / "workspace" / "wf_03",
        deps=deps,
    )

    assert stage["status"] == "submitted"
    assert stage["task"]["status"] == "submitted"
    assert stage["metadata"]["queue_id"] == "q_existing"
    assert "output_artifacts" not in stage
    assert any(
        record.name == "orca_auto.flow.orchestration.stage_runtime.shared"
        and record.levelno == logging.DEBUG
        and "Failed to load crest artifact contract" in record.getMessage()
        and record.exc_info
        for record in caplog.records
    )


def test_sync_crest_stage_propagates_corrupt_contract_lookup_errors(
    tmp_path: Path,
) -> None:
    stage: dict[str, Any] = {
        "stage_id": "crest_nci_04",
        "status": "submitted",
        "metadata": {"queue_id": "q_existing"},
        "task": {
            "engine": "crest",
            "status": "submitted",
            "payload": {"job_dir": str(tmp_path / "job_dir"), "selected_input_xyz": ""},
            "enqueue_payload": {"priority": 5},
        },
    }

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: tmp_path / "crest_allowed",
            "load_crest_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("corrupt crest contract")
            ),
        }
    )

    with pytest.raises(RuntimeError, match="corrupt crest contract"):
        sync_crest_stage_impl(
            stage,
            crest_config="/tmp/crest.yaml",
            submit_ready=False,
            workflow_id="wf_04",
            workspace_dir=tmp_path / "workspace" / "wf_04",
            deps=deps,
        )
