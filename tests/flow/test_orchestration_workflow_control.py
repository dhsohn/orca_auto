from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import orca_auto.flow.orchestration.advance as advance_module
from orca_auto.flow import orchestration
from orca_auto.flow.orchestration.deps import orchestration_deps
from orca_auto.flow.orchestration.stage_runtime.crest import ensure_crest_job_dir_impl
from orca_auto.flow.orchestration.stage_runtime.xtb_inputs import (
    _materialize_xtb_override_xcontrol,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_path_jobs import write_xtb_path_job_impl
from orca_auto.flow.orchestration.stage_runtime.xtb_retry import (
    xtb_current_attempt_number_impl,
    xtb_path_retry_limit_impl,
    xtb_retry_recipe_impl,
)


def _write_xyz_ensemble(path: Path, comments: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for comment in comments:
        lines.extend(
            [
                "2",
                comment,
                "H 0 0 0",
                "H 0 0 0.74",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _si_publication_test_deps(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    sync_workflow_registry: Any | None = None,
) -> Any:
    return orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-07-12T12:00:00+00:00",
            "_append_conformer_orca_stages": lambda current_payload, **kwargs: False,
            "_append_interaction_energy_stages": lambda current_payload, **kwargs: False,
            "_maybe_notify_workflow_phase_summary": lambda *args, **kwargs: None,
            "_recompute_workflow_status": lambda current_payload: str(
                current_payload.get("status", "running")
            ),
            "_workflow_has_active_children": lambda current_payload: False,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": sync_workflow_registry
            or (lambda workflow_root, workspace_dir, current_payload: None),
        }
    )


def test_nonterminal_si_publication_honors_backoff_without_resetting_attempts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_si_backoff",
        "template_name": "conformer_screening",
        "status": "running",
        "stages": [],
        "metadata": {},
    }
    writer_calls = 0

    def fail_writer(*args: Any, **kwargs: Any) -> None:
        nonlocal writer_calls
        writer_calls += 1
        raise PermissionError("transient denial")

    monkeypatch.setattr(advance_module, "write_workflow_si", fail_writer)
    monkeypatch.setattr(advance_module, "write_workflow_html_report", lambda *args: None)
    deps = _si_publication_test_deps(tmp_path, payload)

    orchestration.advance_workflow(target="wf_si_backoff", workflow_root=tmp_path, deps=deps)
    assert payload["metadata"]["si_publish_attempts"] == 1
    assert payload["metadata"]["si_publish_pending"] is True
    retry_at = payload["metadata"]["si_publish_next_retry_at"]

    orchestration.advance_workflow(target="wf_si_backoff", workflow_root=tmp_path, deps=deps)
    assert writer_calls == 1
    assert payload["metadata"]["si_publish_attempts"] == 1
    assert payload["metadata"]["si_publish_next_retry_at"] == retry_at


def test_permanent_si_publication_error_blocks_without_automatic_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_si_block",
        "template_name": "conformer_screening",
        "status": "running",
        "stages": [],
        "metadata": {},
    }
    writer_calls = 0

    def blocked_writer(*args: Any, **kwargs: Any) -> None:
        nonlocal writer_calls
        writer_calls += 1
        raise FileExistsError("unowned interaction_energy.csv")

    monkeypatch.setattr(advance_module, "write_workflow_si", blocked_writer)
    monkeypatch.setattr(advance_module, "write_workflow_html_report", lambda *args: None)
    deps = _si_publication_test_deps(tmp_path, payload)
    orchestration.advance_workflow(target="wf_si_block", workflow_root=tmp_path, deps=deps)
    assert payload["metadata"]["si_publish_blocked"] is True
    assert payload["metadata"]["si_publish_pending"] is False

    orchestration.advance_workflow(target="wf_si_block", workflow_root=tmp_path, deps=deps)
    assert writer_calls == 1
    assert payload["metadata"]["si_publish_blocked"] is True


def test_registry_checkpoint_failure_does_not_consume_si_writer_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_si_registry_failure",
        "template_name": "conformer_screening",
        "status": "running",
        "stages": [],
        "metadata": {},
    }
    writer_calls = 0

    def writer(*args: Any, **kwargs: Any) -> None:
        nonlocal writer_calls
        writer_calls += 1

    def fail_registry(*args: Any, **kwargs: Any) -> None:
        raise OSError("registry unavailable")

    monkeypatch.setattr(advance_module, "write_workflow_si", writer)
    monkeypatch.setattr(advance_module, "write_workflow_html_report", lambda *args: None)
    deps = _si_publication_test_deps(
        tmp_path,
        payload,
        sync_workflow_registry=fail_registry,
    )
    with pytest.raises(OSError, match="registry unavailable"):
        orchestration.advance_workflow(
            target="wf_si_registry_failure",
            workflow_root=tmp_path,
            deps=deps,
        )
    assert writer_calls == 0
    assert "si_publish_attempts" not in payload["metadata"]


def test_xtb_retry_helpers_and_job_writer_materialize_attempt_files(tmp_path: Path) -> None:
    reactant_xyz = tmp_path / "inputs" / "reactant.xyz"
    product_xyz = tmp_path / "inputs" / "product.xyz"
    reactant_xyz.parent.mkdir(parents=True)
    reactant_xyz.write_text("2\nreactant\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    product_xyz.write_text("2\nproduct\nH 0 0 0\nH 0 0 0.80\n", encoding="utf-8")

    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_01",
        "metadata": {},
        "task": {
            "resource_request": {"max_cores": 12, "max_memory_gb": 36},
            "payload": {
                "reaction_key": "rxn_01",
                "reactant_source": {"artifact_path": str(reactant_xyz)},
                "product_source": {"artifact_path": str(product_xyz)},
                "max_handoff_retries": "3",
            },
            "metadata": {"max_handoff_retries": "5"},
            "enqueue_payload": {},
        },
    }

    assert xtb_path_retry_limit_impl(stage) == 3
    assert xtb_current_attempt_number_impl(stage) == 0
    assert xtb_retry_recipe_impl(1)["recipe_id"] == "path_input_recommended"
    assert xtb_retry_recipe_impl(2)["xcontrol_name"] == "path_retry_02.inp"

    job_dir = write_xtb_path_job_impl(
        stage,
        xtb_allowed_root=tmp_path / "xtb_allowed",
        workflow_id="wf_01",
        attempt_number=2,
    )

    job_path = Path(job_dir)
    payload = cast(dict[str, Any], stage["task"])["payload"]
    metadata = cast(dict[str, Any], stage["metadata"])
    attempt = cast(list[dict[str, Any]], metadata["xtb_attempts"])[0]

    assert job_path == tmp_path / "xtb_allowed" / "xtb_path_search_01" / "retry_attempt_02"
    assert (job_path / "reactants" / "r1.xyz").exists()
    assert (job_path / "products" / "p1.xyz").exists()
    assert (job_path / "path_retry_02.inp").read_text(encoding="utf-8").startswith("$path")
    assert "namespace:" not in (job_path / "xtb_job.yaml").read_text(encoding="utf-8")
    assert payload["job_dir"] == str(job_path)
    assert payload["selected_input_xyz"] == str(job_path / "reactants" / "r1.xyz")
    assert payload["secondary_input_xyz"] == str(job_path / "products" / "p1.xyz")
    assert payload["xtb_active_attempt_number"] == 2
    assert payload["xtb_retry_recipe_id"] == "path_input_refined"
    assert metadata["xtb_active_attempt_number"] == 2
    assert metadata["xtb_retry_recipe_label"] == "refined_path_input"
    assert attempt["attempt_number"] == 2
    assert attempt["recipe_id"] == "path_input_refined"
    assert attempt["job_dir"] == str(job_path)
    assert attempt["namespace"] == ""

    metadata["xtb_active_attempt_number"] = 4
    assert xtb_current_attempt_number_impl(stage) == 4


@pytest.mark.parametrize(
    "target_name",
    (
        "../escape.inp",
        "/tmp/escape.inp",
        "nested/escape.inp",
        "nested\\escape.inp",
        "C:\\temp\\escape.inp",
        "C:escape.inp",
        "..",
        ".",
    ),
)
def test_xtb_xcontrol_target_name_rejects_paths(
    tmp_path: Path,
    target_name: str,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    with pytest.raises(ValueError, match="xcontrol target"):
        _materialize_xtb_override_xcontrol(
            job_dir,
            overrides={"xcontrol": target_name, "xcontrol_text": "$path"},
        )

    assert not (tmp_path / "escape.inp").exists()


def test_xtb_xcontrol_target_name_allows_plain_filename(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    target_name = _materialize_xtb_override_xcontrol(
        job_dir,
        overrides={"xcontrol": "custom_xcontrol.inp", "xcontrol_text": "$path"},
    )

    assert target_name == "custom_xcontrol.inp"
    assert (job_dir / "custom_xcontrol.inp").read_text(encoding="utf-8") == "$path\n"


def test_xtb_job_writer_materializes_ranked_multiframe_inputs(tmp_path: Path) -> None:
    reactant_xyz = tmp_path / "inputs" / "crest_reactant_conformers.xyz"
    product_xyz = tmp_path / "inputs" / "crest_product_conformers.xyz"
    _write_xyz_ensemble(reactant_xyz, ("energy: -3.0", "energy: -2.5", "energy: -2.2"))
    _write_xyz_ensemble(product_xyz, ("energy: -1.0", "energy: -0.8", "energy: -0.6"))

    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_02",
        "metadata": {},
        "task": {
            "resource_request": {"max_cores": 8, "max_memory_gb": 24},
            "payload": {
                "reaction_key": "rxn_ranked",
                "reactant_source": {
                    "artifact_path": str(reactant_xyz),
                    "rank": 2,
                    "metadata": {"source_frame_index": 2},
                },
                "product_source": {
                    "artifact_path": str(product_xyz),
                    "rank": 3,
                    "metadata": {"source_frame_index": 3},
                },
            },
            "enqueue_payload": {},
        },
    }

    job_dir = write_xtb_path_job_impl(
        stage,
        xtb_allowed_root=tmp_path / "xtb_ranked",
        workflow_id="wf_ranked",
        attempt_number=0,
    )

    job_path = Path(job_dir)
    reactant_target = job_path / "reactants" / "r2.xyz"
    product_target = job_path / "products" / "p3.xyz"
    manifest = yaml.safe_load((job_path / "xtb_job.yaml").read_text(encoding="utf-8"))

    assert reactant_target.exists()
    assert product_target.exists()
    assert reactant_target.read_text(encoding="utf-8").splitlines()[1] == "energy: -2.5"
    assert product_target.read_text(encoding="utf-8").splitlines()[1] == "energy: -0.6"
    assert manifest["reactant_xyz"] == "r2.xyz"
    assert manifest["product_xyz"] == "p3.xyz"


def test_xtb_job_writer_rejects_endpoint_element_order_mismatch(tmp_path: Path) -> None:
    reactant_xyz = tmp_path / "inputs" / "reactant.xyz"
    product_xyz = tmp_path / "inputs" / "product.xyz"
    reactant_xyz.parent.mkdir(parents=True)
    reactant_xyz.write_text("2\nr\nH 0 0 0\nO 0 0 1\n", encoding="utf-8")
    product_xyz.write_text("2\np\nO 0 0 0\nH 0 0 1\n", encoding="utf-8")
    stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_01",
        "metadata": {},
        "task": {
            "resource_request": {"max_cores": 2, "max_memory_gb": 4},
            "payload": {
                "reaction_key": "rxn_mismatch",
                "reactant_source": {"artifact_path": str(reactant_xyz), "rank": 1},
                "product_source": {"artifact_path": str(product_xyz), "rank": 1},
            },
            "enqueue_payload": {},
        },
    }

    with pytest.raises(ValueError, match="identical atom counts and element order"):
        write_xtb_path_job_impl(
            stage,
            xtb_allowed_root=tmp_path / "xtb_mismatch",
            workflow_id="wf_mismatch",
            attempt_number=0,
        )


def test_job_dir_writers_apply_manifest_overrides(tmp_path: Path) -> None:
    input_xyz = tmp_path / "crest_input.xyz"
    input_xyz.write_text("2\ncrest\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    crest_stage: dict[str, Any] = {
        "stage_id": "crest_conformer_01",
        "task": {
            "resource_request": {"max_cores": 7, "max_memory_gb": 28},
            "payload": {
                "source_input_xyz": str(input_xyz),
                "mode": "standard",
                "job_manifest_overrides": {
                    "speed": "mquick",
                    "solvent_model": "alpb",
                    "solvent": "water",
                },
            },
            "enqueue_payload": {},
        },
    }
    crest_job_dir = ensure_crest_job_dir_impl(
        crest_stage,
        crest_allowed_root=tmp_path / "crest_allowed",
        workflow_id="wf_crest",
    )
    crest_manifest = yaml.safe_load(
        (Path(crest_job_dir) / "crest_job.yaml").read_text(encoding="utf-8")
    )
    assert crest_manifest == {
        "mode": "standard",
        "speed": "mquick",
        "gfn": 2,
        "solvent_model": "alpb",
        "solvent": "water",
        "resources": {"max_cores": 7, "max_memory_gb": 28},
        "input_xyz": "input.xyz",
    }

    reactant_xyz = tmp_path / "reactant_override.xyz"
    product_xyz = tmp_path / "product_override.xyz"
    xcontrol_file = tmp_path / "path_override.inp"
    reactant_xyz.write_text("2\nreactant\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    product_xyz.write_text("2\nproduct\nH 0 0 0\nH 0 0 0.80\n", encoding="utf-8")
    xcontrol_file.write_text("$path\nnrun=4\n$end\n", encoding="utf-8")
    xtb_stage: dict[str, Any] = {
        "stage_id": "xtb_path_search_01",
        "metadata": {},
        "task": {
            "resource_request": {"max_cores": 9, "max_memory_gb": 30},
            "payload": {
                "reaction_key": "rxn_override",
                "reactant_source": {"artifact_path": str(reactant_xyz)},
                "product_source": {"artifact_path": str(product_xyz)},
                "job_manifest_overrides": {
                    "gfn": 1,
                    "charge": -1,
                    "uhf": 1,
                    "solvent_model": "alpb",
                    "solvent": "water",
                    "xcontrol_file": str(xcontrol_file),
                },
            },
            "enqueue_payload": {},
        },
    }
    xtb_job_dir = write_xtb_path_job_impl(
        xtb_stage,
        xtb_allowed_root=tmp_path / "xtb_allowed_override",
        workflow_id="wf_xtb",
        attempt_number=0,
    )
    xtb_job_path = Path(xtb_job_dir)
    xtb_manifest = yaml.safe_load((xtb_job_path / "xtb_job.yaml").read_text(encoding="utf-8"))
    assert xtb_manifest == {
        "job_type": "path_search",
        "gfn": 1,
        "charge": -1,
        "uhf": 1,
        "solvent_model": "alpb",
        "solvent": "water",
        "resources": {"max_cores": 9, "max_memory_gb": 30},
        "reaction_key": "rxn_override",
        "reactant_xyz": "r1.xyz",
        "product_xyz": "p1.xyz",
        "xcontrol": "workflow_xcontrol.inp",
    }
    assert (xtb_job_path / "workflow_xcontrol.inp").read_text(
        encoding="utf-8"
    ) == "$path\nnrun=4\n$end\n"


def test_advance_workflow_reaction_ts_search_runs_append_sequence_and_sets_child_sync_metadata(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_01",
        "template_name": "reaction_ts_search",
        "status": "planned",
        "stages": [
            {
                "stage_id": "crest_stage_01",
                "status": "completed",
                "task": {"engine": "crest", "status": "completed"},
                "metadata": {},
            }
        ],
        "metadata": {},
    }
    calls: list[tuple[str, str, bool]] = []
    written: list[dict[str, Any]] = []
    synced: list[dict[str, Any]] = []

    def fake_sync_crest_stage(stage: dict[str, Any], **kwargs: object) -> None:
        calls.append(("crest", str(stage.get("stage_id", "")), bool(kwargs["submit_ready"])))

    def fake_append_reaction_xtb_stages(current_payload: dict[str, Any], **kwargs: object) -> bool:
        calls.append(("append_xtb", str(kwargs["workspace_dir"]), False))
        cast(list[dict[str, Any]], current_payload.setdefault("stages", [])).append(
            {
                "stage_id": "xtb_stage_01",
                "status": "planned",
                "task": {"engine": "xtb", "status": "planned"},
                "metadata": {},
            }
        )
        return True

    def fake_sync_xtb_stage(stage: dict[str, Any], **kwargs: object) -> None:
        calls.append(("xtb", str(stage.get("stage_id", "")), bool(kwargs["submit_ready"])))
        task = stage.get("task")
        if isinstance(task, dict) and str(task.get("engine", "")) == "xtb":
            stage["status"] = "completed"
            task["status"] = "completed"

    def fake_clear(current_payload: dict[str, Any]) -> None:
        calls.append(("clear_xtb_error", str(current_payload.get("workflow_id", "")), False))

    def fake_append_reaction_orca_stages(current_payload: dict[str, Any], **kwargs: object) -> bool:
        calls.append(("append_orca", str(kwargs["workspace_dir"]), False))
        cast(list[dict[str, Any]], current_payload.setdefault("stages", [])).append(
            {
                "stage_id": "orca_stage_01",
                "status": "planned",
                "task": {"engine": "orca", "status": "planned"},
                "metadata": {},
            }
        )
        return True

    def fake_sync_orca_stage(stage: dict[str, Any], **kwargs: object) -> None:
        calls.append(("orca", str(stage.get("stage_id", "")), bool(kwargs["submit_ready"])))

    def fake_write_workflow_payload(workspace_dir: Path, current_payload: dict[str, Any]) -> None:
        written.append(deepcopy(current_payload))

    def fake_sync_workflow_registry(
        workflow_root: Path, workspace_dir: Path, current_payload: dict[str, Any]
    ) -> None:
        synced.append(deepcopy(current_payload))

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-19T12:00:00+00:00",
            "_sync_crest_stage": fake_sync_crest_stage,
            "_append_reaction_xtb_stages": fake_append_reaction_xtb_stages,
            "_sync_xtb_stage": fake_sync_xtb_stage,
            "_clear_reaction_xtb_handoff_error_if_recovering": fake_clear,
            "_append_reaction_orca_stages": fake_append_reaction_orca_stages,
            "_sync_orca_stage": fake_sync_orca_stage,
            "_recompute_workflow_status": lambda current_payload: "failed",
            "_workflow_has_active_children": lambda current_payload: True,
            "write_workflow_payload": fake_write_workflow_payload,
            "sync_workflow_registry": fake_sync_workflow_registry,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_reaction_01",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert result["status"] == "failed"
    assert result["metadata"]["last_advanced_at"] == "2026-04-19T12:00:00+00:00"
    assert result["metadata"]["sync_only"] is False
    assert result["metadata"]["final_child_sync_pending"] is True
    assert result["metadata"]["final_child_sync_completed_at"] == ""
    assert [entry[:2] for entry in calls] == [
        ("crest", "crest_stage_01"),
        ("append_xtb", str(tmp_path / "wf_reaction_01")),
        ("xtb", "crest_stage_01"),
        ("xtb", "xtb_stage_01"),
        ("clear_xtb_error", "wf_reaction_01"),
        ("append_orca", str(tmp_path / "wf_reaction_01")),
        ("orca", "crest_stage_01"),
        ("orca", "xtb_stage_01"),
        ("orca", "orca_stage_01"),
    ]
    assert {entry[2] for entry in calls if entry[0] in {"crest", "xtb", "orca"}} == {True}
    assert written and written[-1]["metadata"]["final_child_sync_pending"] is True
    assert synced and synced[-1]["metadata"]["sync_only"] is False


def test_advance_workflow_quarantines_renamed_legacy_workflow_before_submission(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "TS8_wf"
    payload: dict[str, Any] = {
        "workflow_id": "TS8(wf)",
        "template_name": "reaction_ts_search",
        "status": "planned",
        "stages": [
            {
                "stage_id": "crest_reactant_01",
                "status": "planned",
                "task": {"engine": "crest", "status": "planned"},
                "metadata": {},
            }
        ],
        "metadata": {},
    }
    written: list[dict[str, Any]] = []
    synced: list[dict[str, Any]] = []

    sync_calls: list[dict[str, object]] = []

    def sync_without_submission(_stage: dict[str, Any], **kwargs: object) -> None:
        sync_calls.append(kwargs)
        assert kwargs["submit_ready"] is False

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: workspace,
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-07-10T06:00:00+00:00",
            "_sync_crest_stage": sync_without_submission,
            "write_workflow_payload": lambda workspace_dir, current: written.append(
                deepcopy(current)
            ),
            "sync_workflow_registry": lambda root, workspace_dir, current: synced.append(
                deepcopy(current)
            ),
        }
    )

    result = orchestration.advance_workflow(
        target=str(workspace),
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert result is payload
    assert payload["status"] == "failed"
    assert payload["stages"][0]["status"] == "cancelled"
    assert payload["stages"][0]["task"]["status"] == "cancelled"
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "workflow_identity_validation",
        "reason": (
            "workflow directory name 'TS8_wf' does not match persisted workflow_id "
            "'TS8(wf)'. Renaming an existing workflow directory is not supported; "
            "restore its original name or create a new workflow."
        ),
        "message": (
            "workflow directory name 'TS8_wf' does not match persisted workflow_id "
            "'TS8(wf)'. Renaming an existing workflow directory is not supported; "
            "restore its original name or create a new workflow."
        ),
        "detected_at": "2026-07-10T06:00:00+00:00",
    }
    assert payload["metadata"]["sync_only"] is True
    assert payload["metadata"]["final_child_sync_pending"] is False
    assert len(sync_calls) == 1
    assert written[-1] == payload
    assert synced[-1] == payload


def test_identity_quarantine_allows_active_child_to_drain_across_sync_cycles(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "TS8_wf"
    stage: dict[str, Any] = {
        "stage_id": "crest_reactant_01",
        "stage_kind": "crest_stage",
        "status": "queued",
        "task": {"engine": "crest", "status": "submitted", "payload": {}},
        "metadata": {"child_job_id": "crest-active"},
    }
    payload: dict[str, Any] = {
        "workflow_id": "TS8(wf)",
        "template_name": "reaction_ts_search",
        "status": "running",
        "stages": [stage],
        "metadata": {},
    }
    sync_count = 0
    cancel_count = 0

    def sync_crest_stage(_stage: dict[str, Any], **kwargs: object) -> None:
        nonlocal sync_count
        sync_count += 1
        assert kwargs["submit_ready"] is False
        if sync_count == 2:
            _stage["status"] = "cancelled"
            cast(dict[str, Any], _stage["task"])["status"] = "cancelled"

    def cancel_active_stages(
        current_payload: dict[str, Any], **_kwargs: object
    ) -> dict[str, list[dict[str, Any]]]:
        nonlocal cancel_count
        cancel_count += 1
        current_stage = cast(list[dict[str, Any]], current_payload["stages"])[0]
        if current_stage["status"] in {"queued", "running", "submitted"}:
            current_stage["status"] = "cancel_requested"
            cast(dict[str, Any], current_stage["task"])["status"] = "cancel_requested"
            return {
                "cancelled": [{"stage_id": "crest_reactant_01", "status": "cancel_requested"}],
                "failed": [],
            }
        return {"cancelled": [], "failed": []}

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: workspace,
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-07-10T06:00:00+00:00",
            "_sync_crest_stage": sync_crest_stage,
            "_cancel_active_workflow_stages": cancel_active_stages,
            "write_workflow_payload": lambda workspace_dir, current: None,
            "sync_workflow_registry": lambda root, workspace_dir, current: None,
        }
    )

    first = orchestration.advance_workflow(
        target=str(workspace),
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )
    first_snapshot = deepcopy(first)
    second = orchestration.advance_workflow(
        target=str(workspace),
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert first_snapshot["status"] == "failed"
    assert first_snapshot["stages"][0]["status"] == "cancel_requested"
    assert first_snapshot["metadata"]["final_child_sync_pending"] is True
    assert second["status"] == "failed"
    assert second["stages"][0]["status"] == "cancelled"
    assert second["metadata"]["final_child_sync_pending"] is False
    assert second["metadata"]["workflow_error"]["detected_at"] == ("2026-07-10T06:00:00+00:00")
    assert sync_count == 2
    assert cancel_count == 2


def test_advance_workflow_checkpoints_completed_crest_before_xtb_materialization(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_checkpoint",
        "template_name": "reaction_ts_search",
        "status": "planned",
        "stages": [
            {
                "stage_id": "crest_reactant_01",
                "status": "running",
                "task": {"engine": "crest", "status": "running"},
                "metadata": {"input_role": "reactant"},
            },
            {
                "stage_id": "crest_product_01",
                "status": "running",
                "task": {"engine": "crest", "status": "running"},
                "metadata": {"input_role": "product"},
            },
        ],
        "metadata": {},
    }
    writes: list[dict[str, Any]] = []

    def fake_sync_crest_stage(stage: dict[str, Any], **kwargs: object) -> None:
        stage["status"] = "completed"
        cast(dict[str, Any], stage["task"])["status"] = "completed"

    def fake_append_reaction_xtb_stages(current_payload: dict[str, Any], **kwargs: object) -> bool:
        cast(list[dict[str, Any]], current_payload.setdefault("stages", [])).append(
            {
                "stage_id": "xtb_path_search_01",
                "status": "planned",
                "task": {"engine": "xtb", "status": "planned"},
                "metadata": {},
            }
        )
        return True

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-24T06:00:00+00:00",
            "_sync_crest_stage": fake_sync_crest_stage,
            "_append_reaction_xtb_stages": fake_append_reaction_xtb_stages,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_reaction_orca_stages": lambda current_payload, **kwargs: False,
            "_sync_orca_stage": lambda stage, **kwargs: None,
            "_recompute_workflow_status": lambda current_payload: "running",
            "_workflow_has_active_children": lambda current_payload: False,
            "write_workflow_payload": lambda workspace_dir, current_payload: writes.append(
                deepcopy(current_payload)
            ),
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    orchestration.advance_workflow(
        target="wf_reaction_checkpoint",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert len(writes) >= 3
    first_stage_ids = [stage["stage_id"] for stage in writes[0]["stages"]]
    second_stage_ids = [stage["stage_id"] for stage in writes[1]["stages"]]
    assert first_stage_ids == ["crest_reactant_01", "crest_product_01"]
    assert all(stage["status"] == "completed" for stage in writes[0]["stages"])
    assert second_stage_ids == ["crest_reactant_01", "crest_product_01", "xtb_path_search_01"]
    assert writes[-1]["metadata"]["last_advanced_at"] == "2026-04-24T06:00:00+00:00"


def test_advance_workflow_reaction_ts_search_waits_for_all_xtb_children_before_queueing_orca(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_incremental",
        "template_name": "reaction_ts_search",
        "status": "running",
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "task": {"engine": "xtb", "status": "completed"},
                "metadata": {},
            },
            {
                "stage_id": "xtb_path_search_02",
                "status": "queued",
                "task": {"engine": "xtb", "status": "queued"},
                "metadata": {},
            },
        ],
        "metadata": {},
    }
    calls: list[tuple[str, str]] = []

    def fake_sync_xtb_stage(stage: dict[str, Any], **kwargs: object) -> None:
        calls.append(("sync_xtb", str(stage.get("stage_id", ""))))

    def fake_append_reaction_orca_stages(current_payload: dict[str, Any], **kwargs: object) -> bool:
        calls.append(("append_orca", "unexpected"))
        return True

    def fake_sync_orca_stage(stage: dict[str, Any], **kwargs: object) -> None:
        task = stage.get("task")
        if isinstance(task, dict) and str(task.get("engine", "")) == "orca":
            calls.append(("sync_orca", str(stage.get("stage_id", ""))))

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-22T09:00:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_append_reaction_xtb_stages": lambda current_payload, **kwargs: False,
            "_sync_xtb_stage": fake_sync_xtb_stage,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_reaction_orca_stages": fake_append_reaction_orca_stages,
            "_sync_orca_stage": fake_sync_orca_stage,
            "_recompute_workflow_status": lambda current_payload: "running",
            "_workflow_has_active_children": lambda current_payload: True,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_reaction_incremental",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert result["status"] == "running"
    assert all(entry[0] != "append_orca" for entry in calls)
    assert all(entry[0] != "sync_orca" for entry in calls)
    assert [stage["stage_id"] for stage in result["stages"]] == [
        "xtb_path_search_01",
        "xtb_path_search_02",
    ]


def test_advance_workflow_records_reaction_orca_exhaustion_after_sync_failure(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_orca_exhausts_during_sync",
        "template_name": "reaction_ts_search",
        "status": "running",
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "task": {"engine": "xtb", "status": "completed"},
                "metadata": {},
            },
            {
                "stage_id": "orca_candidate_01",
                "status": "running",
                "task": {"engine": "orca", "status": "running"},
                "metadata": {},
            },
        ],
        "metadata": {},
    }
    append_calls = 0

    def fake_append_reaction_orca_stages(
        current_payload: dict[str, Any], **_kwargs: object
    ) -> bool:
        nonlocal append_calls
        append_calls += 1
        orca_stages = [
            stage
            for stage in current_payload.get("stages", [])
            if isinstance(stage, dict)
            and isinstance(stage.get("task"), dict)
            and cast(dict[str, Any], stage["task"]).get("engine") == "orca"
        ]
        if orca_stages and all(str(stage.get("status")) == "failed" for stage in orca_stages):
            current_payload.setdefault("metadata", {})["workflow_error"] = {
                "status": "failed",
                "scope": "reaction_ts_search_orca_candidate_exhausted",
                "reason": "ts_candidates_exhausted",
            }
        return False

    def fake_sync_orca_stage(stage: dict[str, Any], **_kwargs: object) -> None:
        task = stage.get("task")
        if isinstance(task, dict) and task.get("engine") == "orca":
            stage["status"] = "failed"
            task["status"] = "failed"

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-22T10:00:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_append_reaction_xtb_stages": lambda current_payload, **kwargs: False,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_reaction_orca_stages": fake_append_reaction_orca_stages,
            "_sync_orca_stage": fake_sync_orca_stage,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_reaction_orca_exhausts_during_sync",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert append_calls == 2
    assert result["status"] == "failed"
    assert result["metadata"]["workflow_error"]["scope"] == (
        "reaction_ts_search_orca_candidate_exhausted"
    )


def test_advance_workflow_conformer_screening_queues_twenty_orca_children_after_crest_completion(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_conformer_incremental",
        "template_name": "conformer_screening",
        "status": "running",
        "stages": [
            {
                "stage_id": "crest_conformer_01",
                "status": "completed",
                "task": {"engine": "crest", "status": "completed"},
                "metadata": {},
            }
        ],
        "metadata": {},
    }
    synced_orca_stage_ids: list[str] = []

    def fake_append_crest_orca_stages(current_payload: dict[str, Any], **kwargs: object) -> bool:
        for index in range(1, 21):
            cast(list[dict[str, Any]], current_payload.setdefault("stages", [])).append(
                {
                    "stage_id": f"orca_conformer_{index:02d}",
                    "status": "planned",
                    "task": {"engine": "orca", "status": "planned"},
                    "metadata": {"source_crest_stage_id": "crest_conformer_01"},
                }
            )
        return True

    def fake_sync_orca_stage(stage: dict[str, Any], **kwargs: object) -> None:
        if str((stage.get("task") or {}).get("engine", "")) == "orca":
            synced_orca_stage_ids.append(str(stage.get("stage_id", "")))

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-22T11:00:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_crest_orca_stages": fake_append_crest_orca_stages,
            "_sync_orca_stage": fake_sync_orca_stage,
            "_recompute_workflow_status": lambda current_payload: "running",
            "_workflow_has_active_children": lambda current_payload: True,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_conformer_incremental",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert result["status"] == "running"
    assert len(synced_orca_stage_ids) == 20
    assert synced_orca_stage_ids[0] == "orca_conformer_01"
    assert synced_orca_stage_ids[-1] == "orca_conformer_20"


def test_advance_workflow_records_conformer_orca_exhaustion_after_sync_failure(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_conformer_orca_exhausts_during_sync",
        "template_name": "conformer_screening",
        "status": "running",
        "stages": [
            {
                "stage_id": "crest_conformer_01",
                "status": "completed",
                "task": {"engine": "crest", "status": "completed"},
                "metadata": {},
            },
            {
                "stage_id": "orca_conformer_01",
                "status": "running",
                "task": {"engine": "orca", "status": "running"},
                "metadata": {},
            },
        ],
        "metadata": {},
    }
    append_calls = 0

    def fake_append_crest_orca_stages(current_payload: dict[str, Any], **_kwargs: object) -> bool:
        nonlocal append_calls
        append_calls += 1
        orca_stages = [
            stage
            for stage in current_payload.get("stages", [])
            if isinstance(stage, dict)
            and isinstance(stage.get("task"), dict)
            and cast(dict[str, Any], stage["task"]).get("engine") == "orca"
        ]
        if orca_stages and all(str(stage.get("status")) == "failed" for stage in orca_stages):
            current_payload.setdefault("metadata", {})["workflow_error"] = {
                "status": "failed",
                "scope": "conformer_screening_orca_conformers_exhausted",
                "reason": "conformers_failed",
            }
        return False

    def fake_sync_orca_stage(stage: dict[str, Any], **_kwargs: object) -> None:
        task = stage.get("task")
        if isinstance(task, dict) and task.get("engine") == "orca":
            stage["status"] = "failed"
            task["status"] = "failed"

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-22T10:30:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_crest_orca_stages": fake_append_crest_orca_stages,
            "_sync_orca_stage": fake_sync_orca_stage,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_conformer_orca_exhausts_during_sync",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert append_calls == 2
    assert result["status"] == "failed"
    assert result["metadata"]["workflow_error"]["scope"] == (
        "conformer_screening_orca_conformers_exhausted"
    )


def test_advance_workflow_reopens_completed_conformer_pending_orca_handoff(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_conformer_reopen",
        "template_name": "conformer_screening",
        "status": "completed",
        "stages": [
            {
                "stage_id": "crest_conformer_01",
                "status": "completed",
                "task": {"engine": "crest", "status": "completed"},
                "metadata": {},
            }
        ],
        "metadata": {},
    }
    append_calls = 0

    def fake_append_crest_orca_stages(current_payload: dict[str, Any], **kwargs: object) -> bool:
        nonlocal append_calls
        append_calls += 1
        cast(list[dict[str, Any]], current_payload.setdefault("stages", [])).append(
            {
                "stage_id": "orca_conformer_01",
                "status": "planned",
                "task": {"engine": "orca", "status": "planned"},
                "metadata": {},
            }
        )
        return True

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-22T12:00:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_crest_orca_stages": fake_append_crest_orca_stages,
            "_sync_orca_stage": lambda stage, **kwargs: None,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_conformer_reopen",
        workflow_root=tmp_path,
        submit_ready=True,
        deps=deps,
    )

    assert append_calls == 1
    assert result["status"] == "running"
    assert result["metadata"]["sync_only"] is False
    assert [stage["stage_id"] for stage in result["stages"]] == [
        "crest_conformer_01",
        "orca_conformer_01",
    ]


def test_advance_workflow_auto_cancels_active_siblings_after_failure(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_failed_cancel",
        "template_name": "reaction_ts_search",
        "status": "running",
        "stages": [
            {
                "stage_id": "crest_product",
                "status": "failed",
                "task": {"engine": "crest", "status": "failed"},
                "metadata": {},
            },
            {
                "stage_id": "crest_reactant",
                "status": "running",
                "task": {"engine": "crest", "status": "running"},
                "metadata": {"queue_id": "q_reactant"},
            },
            {
                "stage_id": "xtb_pending",
                "status": "planned",
                "task": {"engine": "xtb", "status": "planned"},
                "metadata": {},
            },
        ],
        "metadata": {},
    }
    crest_cancel_calls: list[dict[str, Any]] = []

    def fake_crest_cancel_target(**kwargs: Any) -> dict[str, Any]:
        crest_cancel_calls.append(dict(kwargs))
        return {"status": "cancel_requested", "queue_id": kwargs["target"]}

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-24T01:00:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_append_reaction_xtb_stages": lambda current_payload, **kwargs: False,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_append_reaction_orca_stages": lambda current_payload, **kwargs: False,
            "_sync_orca_stage": lambda stage, **kwargs: None,
            "crest_cancel_target": fake_crest_cancel_target,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_failed_cancel",
        workflow_root=tmp_path,
        crest_config="crest.yaml",
        submit_ready=True,
        deps=deps,
    )

    assert result["status"] == "failed"
    assert crest_cancel_calls == [
        {
            "target": "q_reactant",
            "config_path": "crest.yaml",
        }
    ]
    assert result["stages"][1]["status"] == "cancel_requested"
    assert result["stages"][1]["task"]["status"] == "cancel_requested"
    assert result["stages"][1]["task"]["cancel_result"]["status"] == "cancel_requested"
    assert result["stages"][2]["status"] == "cancelled"
    assert result["stages"][2]["task"]["status"] == "cancelled"
    assert result["metadata"]["final_child_sync_pending"] is True
    assert result["metadata"]["final_child_sync_completed_at"] == ""


def test_advance_workflow_auto_cancels_active_children_for_submission_failed_status(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_submission_failed_cancel",
        "template_name": "reaction_ts_search",
        "status": "submission_failed",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "running",
                "task": {"engine": "crest", "status": "running"},
                "metadata": {"queue_id": "q_reactant"},
            },
            {
                "stage_id": "xtb_pending",
                "status": "planned",
                "task": {"engine": "xtb", "status": "planned"},
                "metadata": {},
            },
        ],
        "metadata": {},
    }
    crest_cancel_calls: list[dict[str, Any]] = []

    def fake_crest_cancel_target(**kwargs: Any) -> dict[str, Any]:
        crest_cancel_calls.append(dict(kwargs))
        return {"status": "cancel_requested", "queue_id": kwargs["target"]}

    deps = orchestration_deps(
        overrides={
            "resolve_workflow_workspace": lambda target, workflow_root: (
                tmp_path / str(payload["workflow_id"])
            ),
            "acquire_workflow_lock": lambda workspace_dir, timeout_seconds=5.0: nullcontext(),
            "load_workflow_payload": lambda workspace_dir: payload,
            "now_utc_iso": lambda: "2026-04-24T01:00:00+00:00",
            "_sync_crest_stage": lambda stage, **kwargs: None,
            "_sync_xtb_stage": lambda stage, **kwargs: None,
            "_clear_reaction_xtb_handoff_error_if_recovering": lambda current_payload: None,
            "_sync_orca_stage": lambda stage, **kwargs: None,
            "crest_cancel_target": fake_crest_cancel_target,
            "write_workflow_payload": lambda workspace_dir, current_payload: None,
            "sync_workflow_registry": lambda workflow_root, workspace_dir, current_payload: None,
        }
    )

    result = orchestration.advance_workflow(
        target="wf_submission_failed_cancel",
        workflow_root=tmp_path,
        crest_config="crest.yaml",
        submit_ready=True,
        deps=deps,
    )

    assert result["status"] == "submission_failed"
    assert crest_cancel_calls == [
        {
            "target": "q_reactant",
            "config_path": "crest.yaml",
        }
    ]
    assert result["stages"][0]["status"] == "cancel_requested"
    assert result["stages"][0]["task"]["status"] == "cancel_requested"
    assert result["stages"][1]["status"] == "cancelled"
    assert result["stages"][1]["task"]["status"] == "cancelled"
    assert result["metadata"]["sync_only"] is True
    assert result["metadata"]["final_child_sync_pending"] is True
