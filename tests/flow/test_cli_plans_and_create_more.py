from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_common
from orca_auto.flow.cli import run_dir as cli_run_dir
from orca_auto.flow.run_dir import manifest as run_dir_manifest
from orca_auto.flow.run_dir import options as run_dir_options


def _create_payload(template_name: str) -> dict[str, Any]:
    return {
        "workflow_id": f"wf_create_{template_name}",
        "template_name": template_name,
        "metadata": {"workspace_dir": "/tmp/workflows/wf_create"},
        "stages": [{}, {}],
    }


def test_run_dir_options_preserve_existing_positional_field_order() -> None:
    options = run_dir_options.RunDirWorkflowOptions(
        "/runs",
        "standard",
        10,
        8,
        32,
        20,
        "! r2scan-3c Opt TightSCF",
        0,
        1,
        3,
        4,
    )

    assert options.max_crest_candidates == 3
    assert options.max_xtb_stages == 4
    assert options.boltzmann_temperature_k is None


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
        "scaffold_dir": str(workflow_dir.resolve()),
        "workflow_root": "/tmp/workflow_root",
        "crest_mode": "standard",
        "priority": 10,
        "max_cores": 8,
        "max_memory_gb": 32,
        "max_crest_candidates": 3,
        "max_xtb_stages": 9,
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
                "boltzmann_temperature_k: 310.0",
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
        "scaffold_dir": str(workflow_dir.resolve()),
        "workflow_root": str(workflow_root.resolve()),
        "crest_mode": "nci",
        "priority": 7,
        "max_cores": 12,
        "max_memory_gb": 48,
        "max_orca_stages": 5,
        "orca_route_line": "! test",
        "charge": -1,
        "multiplicity": 2,
        "boltzmann_temperature_k": 310.0,
        "interaction_energy": None,
        "rmsd_dedup": None,
    }


@pytest.mark.parametrize("section", ["crest: fast", "xtb: []", "orca: 3"])
def test_run_dir_rejects_non_mapping_engine_sections(tmp_path: Path, section: str) -> None:
    workflow_dir = tmp_path / "bad_engine_section"
    workflow_dir.mkdir()
    (workflow_dir / "input.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        f"workflow_type: conformer_screening\n{section}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="section must be a mapping"):
        run_dir_manifest._load_run_dir_workflow_config(
            SimpleNamespace(workflow_type=None), workflow_dir
        )


def test_run_dir_rejects_invalid_boltzmann_temperature_at_admission(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "bad_temperature"
    workflow_dir.mkdir()
    (workflow_dir / "input.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "workflow_type: conformer_screening\nboltzmann_temperature_k: fast\n",
        encoding="utf-8",
    )
    config = run_dir_manifest._load_run_dir_workflow_config(
        SimpleNamespace(workflow_type=None), workflow_dir
    )

    with pytest.raises(ValueError, match="boltzmann_temperature_k must be"):
        run_dir_options._resolve_run_dir_workflow_options(
            SimpleNamespace(),
            config.manifest,
            config.sections,
            default_orca_route_line="! r2scan-3c Opt TightSCF",
            default_max_orca_stages=20,
            workflow_root=str(tmp_path / "runs"),
        )


@pytest.mark.parametrize(
    "manifest",
    [
        {"charge": -0.5, "multiplicity": 2.5},
        {"orca": {"charge": -0.5, "multiplicity": 2.5}},
    ],
)
def test_run_dir_rejects_fractional_electronic_state_at_public_ingress(
    manifest: dict[str, object],
) -> None:
    orca_raw = manifest.get("orca")
    orca_section = (
        {str(key): value for key, value in orca_raw.items()} if isinstance(orca_raw, dict) else {}
    )
    sections = run_dir_options.RunDirManifestSections(
        resources={},
        crest={},
        xtb={},
        endpoint_pairing={},
        orca=orca_section,
    )

    with pytest.raises(ValueError, match="charge must be an integer"):
        run_dir_options._resolve_run_dir_workflow_options(
            SimpleNamespace(),
            manifest,
            sections,
            default_orca_route_line="! r2scan-3c OptTS Freq TightSCF",
            default_max_orca_stages=3,
            workflow_root="/tmp/runs",
            workflow_type="reaction_ts_search",
        )


def test_run_dir_rejects_interaction_features_on_unsupported_template(tmp_path: Path) -> None:
    workflow_dir = tmp_path / "reaction"
    workflow_dir.mkdir()
    (workflow_dir / "reactant.xyz").write_text("1\nr\nH 0 0 0\n", encoding="utf-8")
    (workflow_dir / "product.xyz").write_text("1\np\nH 0 0 0\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: reaction_ts_search",
                "interaction_energy:",
                "  enabled: true",
                "  fragments:",
                "    - atom_indices: [0]",
                "    - atom_indices: [1]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = run_dir_manifest._load_run_dir_workflow_config(
        SimpleNamespace(workflow_type=None), workflow_dir
    )
    with pytest.raises(ValueError, match="supported only for conformer_screening"):
        run_dir_options._resolve_run_dir_workflow_options(
            SimpleNamespace(),
            config.manifest,
            config.sections,
            default_orca_route_line="! r2scan-3c OptTS Freq TightSCF",
            default_max_orca_stages=3,
            workflow_root=str(tmp_path / "runs"),
            workflow_type=config.workflow_type,
        )


def _reaction_scaffold(workflow_dir: Path) -> None:
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "reactant.xyz").write_text(
        "2\nreactant\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8"
    )
    (workflow_dir / "product.xyz").write_text("2\nproduct\nH 0 0 0\nH 0 0 0.80\n", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")


def _run_dir_args(workflow_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
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


def test_cmd_run_dir_passes_scaffold_dir_for_generation_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_dir = workflow_root / "rxn_case"
    _reaction_scaffold(workflow_dir)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_common, "_discover_workflow_root", lambda explicit: str(workflow_root.resolve())
    )
    monkeypatch.setattr(
        cli_run_dir,
        "create_reaction_ts_search_workflow",
        lambda **kwargs: captured.update(kwargs) or _create_payload("reaction_ts_search"),
    )

    assert cli_run_dir.cmd_run_dir(_run_dir_args(workflow_dir)) == 0
    assert "workflow_id: wf_create_reaction_ts_search" in capsys.readouterr().out
    # The scaffold hosts the generation workspace; the id is factory-minted.
    assert captured["scaffold_dir"] == str(workflow_dir.resolve())
    assert "workflow_id" not in captured


def test_cmd_run_dir_materializes_generation_workspace_inside_scaffold(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression: the real creation path must mint a generation workspace
    inside the submitted scaffold (no mocked workflow factory), mirroring
    standalone ORCA executions."""

    from orca_auto.core.queue.generation import is_visible_generation_name

    workflow_root = tmp_path / "workflow_root"
    workflow_dir = workflow_root / "rxn_case"
    _reaction_scaffold(workflow_dir)

    monkeypatch.setattr(
        cli_common, "_discover_workflow_root", lambda explicit: str(workflow_root.resolve())
    )

    assert cli_run_dir.cmd_run_dir(_run_dir_args(workflow_dir)) == 0
    stdout = capsys.readouterr().out

    generations = [
        item
        for item in workflow_dir.iterdir()
        if item.is_dir() and is_visible_generation_name(item.name)
    ]
    assert len(generations) == 1
    workspace_dir = generations[0]
    assert (workspace_dir / "workflow.json").is_file()
    assert f"workflow_id: {workspace_dir.name}" in stdout
    # The scaffold inputs stay untouched next to the generation workspace.
    assert sorted(item.name for item in workflow_dir.iterdir() if item.is_file()) == [
        "flow.yaml",
        "product.xyz",
        "reactant.xyz",
    ]

    # A second run mints a sibling generation instead of failing.
    assert cli_run_dir.cmd_run_dir(_run_dir_args(workflow_dir)) == 0
    generations_after = [
        item
        for item in workflow_dir.iterdir()
        if item.is_dir() and is_visible_generation_name(item.name)
    ]
    assert len(generations_after) == 2


def test_cmd_run_dir_rejects_parenthesized_workflow_name_before_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "TS8(wf)"
    workflow_dir.mkdir()
    (workflow_dir / "flow.yaml").write_text(
        "workflow_type: reaction_ts_search\n",
        encoding="utf-8",
    )
    (workflow_dir / "reactant.xyz").write_text("1\nreactant\nH 0 0 0\n", encoding="utf-8")
    (workflow_dir / "product.xyz").write_text("1\nproduct\nH 0 0 0\n", encoding="utf-8")

    def unexpected_create(**_kwargs: Any) -> dict[str, Any]:
        pytest.fail("parenthesized workflow names must be rejected before creation")

    monkeypatch.setattr(cli_run_dir, "create_reaction_ts_search_workflow", unexpected_create)

    rc = cli_run_dir.cmd_run_dir(
        SimpleNamespace(
            workflow_dir=str(workflow_dir),
            json=False,
        )
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "workflow_id cannot contain parentheses" in captured.err
    assert "TS8_wf" in captured.err
    assert not (workflow_dir / "workflow.json").exists()
    assert not (workflow_dir / "01_crest").exists()


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
        "xcontrol_file": str((workflow_dir / "path.inp").resolve()),
    }
    assert captured["endpoint_pairing"] == {
        "enabled": True,
        "comparison_atoms": [1, 2],
        "max_distance_rmsd": 0.25,
    }
