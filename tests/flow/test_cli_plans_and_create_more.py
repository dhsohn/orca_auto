from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_common
from orca_auto.flow.cli import run_dir as cli_run_dir
from orca_auto.flow.run_dir import options as run_dir_options


def _create_payload(template_name: str) -> dict[str, Any]:
    return {
        "workflow_id": f"wf_create_{template_name}",
        "template_name": template_name,
        "metadata": {"workspace_dir": "/tmp/workflows/wf_create"},
        "stages": [{}, {}],
    }


def test_cmd_run_dir_reads_manifest_for_reaction_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "reaction_job"
    workflow_dir.mkdir()
    (workflow_dir / "reactant.xyz").write_text(
        "2\nreactant\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8"
    )
    (workflow_dir / "product.xyz").write_text("2\nproduct\nH 0 0 0\nH 0 0 0.80\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_common, "_discover_workflow_root", lambda explicit: "/tmp/workflow_root"
    )

    def fake_create_reaction_ts_search_workflow(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _create_payload("reaction_ts_search")

    monkeypatch.setattr(
        cli_run_dir, "create_reaction_ts_search_workflow", fake_create_reaction_ts_search_workflow
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 0
    stdout = capsys.readouterr().out
    assert "workflow_id: wf_create_reaction_ts_search" in stdout
    assert captured == {
        "reactant_xyz": str((workflow_dir / "reactant.xyz").resolve()),
        "product_xyz": str((workflow_dir / "product.xyz").resolve()),
        "workflow_id": "wf_reaction_ts_reaction_job",
        "workflow_root": "/tmp/workflow_root",
        "crest_mode": "standard",
        "priority": 10,
        "max_cores": 8,
        "max_memory_gb": 32,
        "max_crest_candidates": 3,
        "max_xtb_stages": 3,
        "max_orca_stages": 3,
        "orca_route_line": "! r2scan-3c OptTS Freq TightSCF",
        "charge": 0,
        "multiplicity": 1,
    }


def test_cmd_run_dir_reads_manifest_for_conformer_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "conformer_job"
    workflow_dir.mkdir()
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {workflow_root}\n", encoding="utf-8")
    (workflow_dir / "input.xyz").write_text("2\nmol\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "crest_mode: nci",
                "priority: 7",
                "resources:",
                "  max_cores: 12",
                "  max_memory_gb: 48",
                "max_orca_stages: 5",
                'orca_route_line: "! test"',
                "charge: -1",
                "multiplicity: 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def fake_create_conformer_screening_workflow(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _create_payload("conformer_screening")

    monkeypatch.setattr(
        cli_run_dir, "create_conformer_screening_workflow", fake_create_conformer_screening_workflow
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        orca_auto_config=str(config_path),
        json=True,
    )

    assert cli_run_dir.cmd_run_dir(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow_id"] == "wf_create_conformer_screening"
    assert captured == {
        "input_xyz": str((workflow_dir / "input.xyz").resolve()),
        "workflow_id": "wf_conformer_screening_conformer_job",
        "workflow_root": str(workflow_root.resolve()),
        "crest_mode": "nci",
        "priority": 7,
        "max_cores": 12,
        "max_memory_gb": 48,
        "max_orca_stages": 5,
        "orca_route_line": "! test",
        "charge": -1,
        "multiplicity": 2,
    }


def test_cmd_run_dir_reuses_direct_child_workflow_directory_when_already_under_workflow_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_dir = workflow_root / "rxn_case"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "reactant.xyz").write_text(
        "2\nreactant\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8"
    )
    (workflow_dir / "product.xyz").write_text("2\nproduct\nH 0 0 0\nH 0 0 0.80\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_common, "_discover_workflow_root", lambda explicit: str(workflow_root.resolve())
    )
    monkeypatch.setattr(
        cli_run_dir,
        "create_reaction_ts_search_workflow",
        lambda **kwargs: captured.update(kwargs) or _create_payload("reaction_ts_search"),
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 0
    assert "workflow_id: wf_create_reaction_ts_search" in capsys.readouterr().out
    assert captured["workflow_id"] == "rxn_case"


def test_cmd_run_dir_reports_ambiguous_layout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "ambiguous_job"
    workflow_dir.mkdir()
    (workflow_dir / "flow.yaml").write_text("priority: 10\n", encoding="utf-8")
    (workflow_dir / "reactant.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "product.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "input.xyz").write_text("x", encoding="utf-8")

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 1
    assert "Ambiguous workflow_dir" in capsys.readouterr().err


def test_cmd_run_dir_workflow_type_override_resolves_ambiguous_layout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "ambiguous_job"
    workflow_root = tmp_path / "manifest_workflows"
    workflow_root.mkdir()
    workflow_dir.mkdir()
    (workflow_dir / "flow.yaml").write_text(
        f"workflow_root: {workflow_root}\n",
        encoding="utf-8",
    )
    (workflow_dir / "reactant.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "product.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "input.xyz").write_text("x", encoding="utf-8")
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_run_dir,
        "create_conformer_screening_workflow",
        lambda **kwargs: captured.update(kwargs) or _create_payload("conformer_screening"),
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type="conformer_screening",
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        orca_auto_config=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 0
    assert "workflow_id: wf_create_conformer_screening" in capsys.readouterr().out
    assert captured["input_xyz"] == str((workflow_dir / "input.xyz").resolve())
    assert captured["workflow_root"] == str(workflow_root.resolve())


def test_cmd_run_dir_requires_manifest_before_materializing_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "missing_manifest"
    workflow_dir.mkdir()
    (workflow_dir / "input.xyz").write_text("x", encoding="utf-8")

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 1
    assert "workflow run-dir requires flow.yaml" in capsys.readouterr().err


def test_cmd_run_dir_requires_standard_input_xyz_name_for_conformer_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "conformer_nonstandard"
    workflow_dir.mkdir()
    (workflow_dir / "molecule.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\n", encoding="utf-8"
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 1
    assert "conformer_screening requires input.xyz" in capsys.readouterr().err


def test_cmd_run_dir_requires_standard_reaction_xyz_names_for_reaction_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "reaction_nonstandard"
    workflow_dir.mkdir()
    (workflow_dir / "reactants.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "products.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 1
    assert (
        "reaction_ts_search requires both reactant.xyz and product.xyz" in capsys.readouterr().err
    )


def test_cmd_run_dir_requires_workflow_root_for_reaction_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "reaction_missing_root"
    workflow_dir.mkdir()
    (workflow_dir / "reactant.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "product.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")
    create_called = False

    monkeypatch.setattr(cli_common, "_discover_workflow_root", lambda explicit: None)
    monkeypatch.setattr(
        run_dir_options, "_cli_workflow_root_for_args", lambda args, *, config_path=None: None
    )

    def fake_create_reaction_ts_search_workflow(**kwargs: Any) -> dict[str, Any]:
        nonlocal create_called
        create_called = True
        return _create_payload("reaction_ts_search")

    monkeypatch.setattr(
        cli_run_dir, "create_reaction_ts_search_workflow", fake_create_reaction_ts_search_workflow
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        orca_auto_config=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 1
    assert "workflow_root is not configured" in capsys.readouterr().err
    assert create_called is False


def test_cmd_run_dir_requires_workflow_root_for_conformer_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "conformer_missing_root"
    workflow_dir.mkdir()
    (workflow_dir / "input.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\n", encoding="utf-8"
    )
    create_called = False

    monkeypatch.setattr(cli_common, "_discover_workflow_root", lambda explicit: None)
    monkeypatch.setattr(
        run_dir_options, "_cli_workflow_root_for_args", lambda args, *, config_path=None: None
    )

    def fake_create_conformer_screening_workflow(**kwargs: Any) -> dict[str, Any]:
        nonlocal create_called
        create_called = True
        return _create_payload("conformer_screening")

    monkeypatch.setattr(
        cli_run_dir, "create_conformer_screening_workflow", fake_create_conformer_screening_workflow
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        orca_auto_config=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 1
    assert "workflow_root is not configured" in capsys.readouterr().err
    assert create_called is False


def test_cmd_run_dir_for_reaction_uses_nested_engine_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "reaction_job_nested"
    workflow_dir.mkdir()
    (workflow_dir / "reactant.xyz").write_text(
        "2\nreactant\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8"
    )
    (workflow_dir / "product.xyz").write_text("2\nproduct\nH 0 0 0\nH 0 0 0.80\n", encoding="utf-8")
    (workflow_dir / "path.inp").write_text("$path\nnrun=3\n$end\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "resources:",
                "  max_cores: 20",
                "  max_memory_gb: 64",
                "crest:",
                "  mode: nci",
                "  speed: squick",
                "  gfn: ff",
                "  no_preopt: true",
                "  noreftopo: true",
                "  notopo: true",
                "  nocbonds: true",
                "xtb:",
                "  gfn: 1",
                "  namespace: rxn_case",
                "  xcontrol_file: path.inp",
                "  endpoint_pairing:",
                "    enabled: true",
                "    comparison_atoms: [1, 2]",
                "    max_distance_rmsd: 0.25",
                "orca:",
                '  route_line: "! custom ts"',
                "  charge: -2",
                "  multiplicity: 3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_common, "_discover_workflow_root", lambda explicit: "/tmp/workflow_root"
    )

    def fake_create_reaction_ts_search_workflow(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _create_payload("reaction_ts_search")

    monkeypatch.setattr(
        cli_run_dir, "create_reaction_ts_search_workflow", fake_create_reaction_ts_search_workflow
    )

    args = SimpleNamespace(
        workflow_dir=str(workflow_dir),
        workflow_type=None,
        workflow_root=None,
        reactant_xyz=None,
        product_xyz=None,
        input_xyz=None,
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        json=False,
    )

    assert cli_run_dir.cmd_run_dir(args) == 0
    assert "workflow_id: wf_create_reaction_ts_search" in capsys.readouterr().out
    assert captured["crest_mode"] == "nci"
    assert captured["orca_route_line"] == "! custom ts"
    assert captured["charge"] == -2
    assert captured["multiplicity"] == 3
    assert captured["max_cores"] == 20
    assert captured["max_memory_gb"] == 64
    assert captured["crest_job_manifest"] == {
        "mode": "nci",
        "speed": "squick",
        "gfn": "ff",
        "no_preopt": True,
        "noreftopo": True,
        "notopo": True,
        "nocbonds": True,
    }
    assert captured["xtb_job_manifest"] == {
        "gfn": 1,
        "namespace": "rxn_case",
        "xcontrol_file": str((workflow_dir / "path.inp").resolve()),
    }
    assert captured["endpoint_pairing"] == {
        "enabled": True,
        "comparison_atoms": [1, 2],
        "max_distance_rmsd": 0.25,
    }
