from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow import orchestration
from orca_auto.flow.orchestration.builders import new_crest_stage_impl


def _write_xyz(path: Path, atoms: list[tuple[str, float, float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [str(len(atoms)), "comment"]
    for symbol, x, y, z in atoms:
        lines.append(f"{symbol} {x:.6f} {y:.6f} {z:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_new_crest_stage_builds_expected_payload_and_metadata() -> None:
    stage = new_crest_stage_impl(
        workflow_id="wf_crest_01",
        template_name="reaction_ts_search",
        stage_id="crest_reactant_01",
        source_path="/tmp/reactant.xyz",
        input_role="reactant",
        mode="nci",
        priority=7,
        max_cores=4,
        max_memory_gb=12,
    )

    task = stage["task"]
    assert stage["stage_kind"] == "crest_stage"
    assert stage["status"] == "planned"
    assert stage["input_artifacts"] == [
        {
            "kind": "input_xyz",
            "path": "/tmp/reactant.xyz",
            "selected": True,
            "metadata": {"input_role": "reactant"},
        }
    ]
    assert task["engine"] == "crest"
    assert task["task_kind"] == "conformer_search"
    assert task["resource_request"] == {"max_cores": 4, "max_memory_gb": 12}
    assert task["payload"]["template_name"] == "reaction_ts_search"
    assert task["payload"]["input_role"] == "reactant"
    assert task["payload"]["mode"] == "nci"
    assert task["enqueue_payload"]["priority"] == 7
    assert task["enqueue_payload"]["submitter"] == "orca_auto_crest"
    assert task["enqueue_payload"]["command_argv"] == [
        "orca_auto.flow.engines.crest.submission.direct_enqueue",
        "config=<crest_config>",
        "job_dir=<job_dir>",
        "priority=7",
    ]
    assert task["metadata"] == {"input_role": "reactant", "mode": "nci"}
    assert stage["metadata"] == {"input_role": "reactant", "mode": "nci"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_cores": 0, "max_memory_gb": 12}, "max_cores must be >= 1"),
        ({"max_cores": 4, "max_memory_gb": 0}, "max_memory_gb must be >= 1"),
    ],
)
def test_new_crest_stage_rejects_non_positive_resources(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        new_crest_stage_impl(
            workflow_id="wf_crest_01",
            template_name="reaction_ts_search",
            stage_id="crest_reactant_01",
            source_path="/tmp/reactant.xyz",
            input_role="reactant",
            mode="nci",
            priority=7,
            **kwargs,
        )


def test_create_reaction_ts_search_workflow_rejects_mismatched_atom_order(tmp_path: Path) -> None:
    reactant_xyz = tmp_path / "reactant_bad.xyz"
    product_xyz = tmp_path / "product_bad.xyz"
    _write_xyz(reactant_xyz, [("H", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    _write_xyz(product_xyz, [("O", 0.0, 0.0, 0.96), ("H", 0.0, 0.0, 0.0)])

    with pytest.raises(ValueError, match="identical reactant/product atom order"):
        orchestration.create_reaction_ts_search_workflow(
            reactant_xyz=str(reactant_xyz),
            product_xyz=str(product_xyz),
            workflow_root=tmp_path,
        )


def test_create_reaction_ts_search_workflow_rejects_invalid_crest_mode(tmp_path: Path) -> None:
    reactant_xyz = tmp_path / "reactant.xyz"
    product_xyz = tmp_path / "product.xyz"
    _write_xyz(reactant_xyz, [("H", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    _write_xyz(product_xyz, [("H", 0.1, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])

    with pytest.raises(ValueError, match="crest_mode 'standard' or 'nci'"):
        orchestration.create_reaction_ts_search_workflow(
            reactant_xyz=str(reactant_xyz),
            product_xyz=str(product_xyz),
            workflow_root=tmp_path,
            crest_mode="weird",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_cores": 0}, "max_cores must be >= 1"),
        ({"max_memory_gb": 0}, "max_memory_gb must be >= 1"),
        ({"max_crest_candidates": 0}, "max_crest_candidates must be >= 1"),
        ({"max_xtb_stages": 0}, "max_xtb_stages must be >= 1"),
        ({"max_orca_stages": 0}, "max_orca_stages must be >= 1"),
        ({"multiplicity": 0}, "multiplicity must be >= 1"),
        ({"max_cores": "many"}, "max_cores must be an integer >= 1"),
    ],
)
def test_create_reaction_ts_search_workflow_rejects_non_positive_limits(
    tmp_path: Path,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    reactant_xyz = tmp_path / "reactant.xyz"
    product_xyz = tmp_path / "product.xyz"
    _write_xyz(reactant_xyz, [("H", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    _write_xyz(product_xyz, [("H", 0.1, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])

    with pytest.raises(ValueError, match=message):
        orchestration.create_reaction_ts_search_workflow(
            reactant_xyz=str(reactant_xyz),
            product_xyz=str(product_xyz),
            workflow_root=tmp_path,
            **kwargs,
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_cores": 0}, "max_cores must be >= 1"),
        ({"multiplicity": 0}, "multiplicity must be >= 1"),
        ({"max_memory_gb": "lots"}, "max_memory_gb must be an integer >= 1"),
        (
            {"boltzmann_temperature_k": float("nan")},
            "boltzmann_temperature_k must be a positive finite number",
        ),
        (
            {"boltzmann_temperature_k": 0},
            "boltzmann_temperature_k must be a positive finite number",
        ),
        (
            {"boltzmann_temperature_k": True},
            "boltzmann_temperature_k must be a positive finite number",
        ),
    ],
)
def test_create_conformer_screening_workflow_rejects_invalid_positive_fields(
    tmp_path: Path,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    input_xyz = tmp_path / "conformer.xyz"
    _write_xyz(input_xyz, [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)])

    with pytest.raises(ValueError, match=message):
        orchestration.create_conformer_screening_workflow(
            input_xyz=str(input_xyz),
            workflow_root=tmp_path,
            **kwargs,
        )


def test_conformer_request_preserves_existing_positional_manifest_slot() -> None:
    manifest = {"ewin": 8}

    request = orchestration.ConformerScreeningWorkflowRequest(
        "input.xyz",
        "/runs",
        None,
        "standard",
        10,
        8,
        32,
        20,
        "! r2scan-3c Opt TightSCF",
        0,
        1,
        manifest,
    )

    assert request.crest_job_manifest == manifest
    assert request.boltzmann_temperature_k is None


@pytest.mark.parametrize("crest_mode", ["standard", "nci"])
def test_create_reaction_ts_search_workflow_materializes_two_crest_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crest_mode: str,
) -> None:
    reactant_xyz = tmp_path / "reactant.xyz"
    product_xyz = tmp_path / "product.xyz"
    _write_xyz(reactant_xyz, [("H", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    _write_xyz(product_xyz, [("H", 0.1, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    sync_calls: list[str] = []

    monkeypatch.setattr(orchestration, "timestamped_token", lambda prefix: "wf_reaction_extra")
    monkeypatch.setattr(orchestration, "now_utc_iso", lambda: "2026-04-19T16:10:00+00:00")
    monkeypatch.setattr(
        orchestration,
        "sync_workflow_registry",
        lambda workflow_root, workspace_dir, payload: sync_calls.append(payload["workflow_id"]),
    )

    payload = orchestration.create_reaction_ts_search_workflow(
        reactant_xyz=str(reactant_xyz),
        product_xyz=str(product_xyz),
        workflow_root=tmp_path,
        crest_mode=crest_mode,
        max_crest_candidates=2,
        max_xtb_stages=4,
        max_xtb_handoff_retries=3,
        max_orca_stages=2,
        orca_route_line="! custom ts route",
    )

    workspace_dir = tmp_path / "wf_reaction_extra"
    request = payload["metadata"]["request"]
    assert payload["template_name"] == "reaction_ts_search"
    assert [stage["stage_id"] for stage in payload["stages"]] == [
        "crest_reactant_01",
        "crest_product_01",
    ]
    assert [stage["metadata"]["mode"] for stage in payload["stages"]] == [crest_mode, crest_mode]
    assert request["parameters"]["crest_mode"] == crest_mode
    assert request["parameters"]["crest_job_manifest"] == {"rthr": 0.3}
    assert request["parameters"]["max_xtb_stages"] == 4
    assert request["parameters"]["max_xtb_handoff_retries"] == 3
    assert request["parameters"]["max_orca_stages"] == 2
    assert request["parameters"]["orca_route_line"] == "! custom ts route"
    assert payload["stages"][0]["task"]["payload"]["job_manifest_overrides"] == {"rthr": 0.3}
    assert payload["stages"][1]["task"]["payload"]["job_manifest_overrides"] == {"rthr": 0.3}
    assert (workspace_dir / "workflow.json").exists()
    assert sync_calls == ["wf_reaction_extra"]


@pytest.mark.parametrize(
    ("workflow_id", "crest_mode"),
    [
        ("wf_conformer_standard_extra", "standard"),
        ("wf_conformer_nci_extra", "nci"),
    ],
)
def test_single_input_crest_workflow_factories_materialize_expected_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_id: str,
    crest_mode: str,
) -> None:
    factory = "create_conformer_screening_workflow"
    stage_id = "crest_conformer_01"
    template_name = "conformer_screening"
    input_role = "molecule"
    artifact_kind = "input_xyz"
    input_xyz = tmp_path / f"{template_name}.xyz"
    _write_xyz(input_xyz, [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)])
    sync_calls: list[str] = []

    monkeypatch.setattr(orchestration, "timestamped_token", lambda prefix: workflow_id)
    monkeypatch.setattr(orchestration, "now_utc_iso", lambda: "2026-04-19T16:20:00+00:00")
    monkeypatch.setattr(
        orchestration,
        "sync_workflow_registry",
        lambda workflow_root, workspace_dir, payload: sync_calls.append(payload["workflow_id"]),
    )

    payload = getattr(orchestration, factory)(
        input_xyz=str(input_xyz),
        workflow_root=tmp_path,
        crest_mode=crest_mode,
        max_orca_stages=2,
        orca_route_line="! custom route",
        charge=1,
        multiplicity=2,
    )

    workspace_dir = tmp_path / workflow_id
    request = payload["metadata"]["request"]
    stage = payload["stages"][0]
    assert payload["workflow_id"] == workflow_id
    assert payload["template_name"] == template_name
    assert request["template_name"] == template_name
    assert request["parameters"]["max_orca_stages"] == 2
    assert request["parameters"]["orca_route_line"] == "! custom route"
    assert request["source_artifacts"][0]["kind"] == artifact_kind
    assert stage["stage_id"] == stage_id
    assert stage["metadata"]["input_role"] == input_role
    assert stage["task"]["payload"]["mode"] == crest_mode
    assert (workspace_dir / "workflow.json").exists()
    assert sync_calls == [workflow_id]


def test_create_conformer_screening_nci_workflow_writes_expected_request_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_xyz = tmp_path / "complex.xyz"
    _write_xyz(input_xyz, [("H", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    sync_calls: list[str] = []

    monkeypatch.setattr(orchestration, "timestamped_token", lambda prefix: "wf_conf_nci_extra")
    monkeypatch.setattr(orchestration, "now_utc_iso", lambda: "2026-04-19T16:30:00+00:00")
    monkeypatch.setattr(
        orchestration,
        "sync_workflow_registry",
        lambda workflow_root, workspace_dir, payload: sync_calls.append(payload["workflow_id"]),
    )

    payload = orchestration.create_conformer_screening_workflow(
        input_xyz=str(input_xyz),
        workflow_root=tmp_path,
        crest_mode="nci",
        priority=13,
        max_cores=10,
        max_memory_gb=40,
        max_orca_stages=4,
        orca_route_line="! nci route",
        charge=-1,
        multiplicity=3,
        boltzmann_temperature_k=310.0,
    )

    assert payload["workflow_id"] == "wf_conf_nci_extra"
    assert [stage["stage_id"] for stage in payload["stages"]] == ["crest_conformer_01"]
    assert payload["stages"][0]["metadata"]["mode"] == "nci"
    assert payload["metadata"]["request"]["template_name"] == "conformer_screening"
    assert payload["metadata"]["request"]["source_artifacts"] == [
        {
            "kind": "input_xyz",
            "path": str((tmp_path / "wf_conf_nci_extra" / "inputs" / input_xyz.name).resolve()),
            "selected": True,
            "metadata": {},
        }
    ]
    assert payload["metadata"]["request"]["parameters"] == {
        "priority": 13,
        "max_cores": 10,
        "max_memory_gb": 40,
        "max_orca_stages": 4,
        "orca_route_line": "! nci route",
        "charge": -1,
        "multiplicity": 3,
        "boltzmann_temperature_k": 310.0,
        "crest_mode": "nci",
    }
    assert sync_calls == ["wf_conf_nci_extra"]


def test_create_conformer_screening_workflow_defaults_to_twenty_orca_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_xyz = tmp_path / "default_conf.xyz"
    _write_xyz(input_xyz, [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)])

    monkeypatch.setattr(orchestration, "timestamped_token", lambda prefix: "wf_conf_default_20")
    monkeypatch.setattr(orchestration, "now_utc_iso", lambda: "2026-04-22T10:00:00+00:00")
    monkeypatch.setattr(
        orchestration, "sync_workflow_registry", lambda workflow_root, workspace_dir, payload: None
    )

    payload = orchestration.create_conformer_screening_workflow(
        input_xyz=str(input_xyz),
        workflow_root=tmp_path,
    )

    assert payload["metadata"]["request"]["parameters"]["max_orca_stages"] == 20


def test_create_reaction_ts_search_workflow_uses_explicit_workflow_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reactant_xyz = tmp_path / "reactant.xyz"
    product_xyz = tmp_path / "product.xyz"
    _write_xyz(reactant_xyz, [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)])
    _write_xyz(product_xyz, [("H", 0.1, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)])

    monkeypatch.setattr(orchestration, "timestamped_token", lambda prefix: "wf_should_not_be_used")
    monkeypatch.setattr(orchestration, "now_utc_iso", lambda: "2026-04-24T00:00:00+00:00")
    monkeypatch.setattr(
        orchestration, "sync_workflow_registry", lambda workflow_root, workspace_dir, payload: None
    )

    payload = orchestration.create_reaction_ts_search_workflow(
        reactant_xyz=str(reactant_xyz),
        product_xyz=str(product_xyz),
        workflow_root=tmp_path,
        workflow_id="manual_rxn_case",
    )

    assert payload["workflow_id"] == "manual_rxn_case"
    assert payload["metadata"]["workspace_dir"] == str((tmp_path / "manual_rxn_case").resolve())


def test_workflow_factories_preserve_engine_manifest_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reactant_xyz = tmp_path / "reactant.yaml.xyz"
    product_xyz = tmp_path / "product.yaml.xyz"
    input_xyz = tmp_path / "single.yaml.xyz"
    _write_xyz(reactant_xyz, [("H", 0.0, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    _write_xyz(product_xyz, [("H", 0.1, 0.0, 0.0), ("O", 0.0, 0.0, 0.96)])
    _write_xyz(input_xyz, [("H", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.74)])

    monkeypatch.setattr(
        orchestration, "timestamped_token", lambda prefix: f"{prefix}_with_manifest"
    )
    monkeypatch.setattr(orchestration, "now_utc_iso", lambda: "2026-04-19T17:00:00+00:00")
    monkeypatch.setattr(
        orchestration, "sync_workflow_registry", lambda workflow_root, workspace_dir, payload: None
    )

    reaction_payload = orchestration.create_reaction_ts_search_workflow(
        reactant_xyz=str(reactant_xyz),
        product_xyz=str(product_xyz),
        workflow_root=tmp_path,
        crest_job_manifest={"speed": "squick", "solvent": "water", "gfn": "ff", "no_preopt": True},
        xtb_job_manifest={"gfn": 1, "namespace": "rxn_a"},
    )
    request_params = reaction_payload["metadata"]["request"]["parameters"]
    assert request_params["crest_job_manifest"] == {
        "rthr": 0.3,
        "speed": "squick",
        "solvent": "water",
        "gfn": "ff",
        "no_preopt": True,
    }
    assert request_params["xtb_job_manifest"] == {"gfn": 1, "namespace": "rxn_a"}
    assert reaction_payload["stages"][0]["task"]["payload"]["job_manifest_overrides"] == {
        "rthr": 0.3,
        "speed": "squick",
        "solvent": "water",
        "gfn": "ff",
        "no_preopt": True,
    }

    conformer_payload = orchestration.create_conformer_screening_workflow(
        input_xyz=str(input_xyz),
        workflow_root=tmp_path,
        crest_job_manifest={"speed": "mquick", "gfn": "ff"},
    )
    conformer_params = conformer_payload["metadata"]["request"]["parameters"]
    assert conformer_params["crest_job_manifest"] == {"speed": "mquick", "gfn": "ff"}
    assert conformer_payload["stages"][0]["task"]["payload"]["job_manifest_overrides"] == {
        "speed": "mquick",
        "gfn": "ff",
    }
