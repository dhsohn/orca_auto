from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto import cli_common
from orca_auto.flow import cli_run_dir as run_dir_cli
from orca_auto.flow import run_dir_manifest, run_dir_options


def test_cli_option_and_workflow_root_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_workflow_root = cli_common._discover_workflow_root("~/workflow-root")
    assert explicit_workflow_root is not None
    assert explicit_workflow_root.endswith("workflow-root")

    monkeypatch.setattr(
        cli_common, "_discover_shared_config_path", lambda explicit: "/tmp/orca_auto.yaml"
    )
    monkeypatch.setattr(
        cli_common, "shared_workflow_root_from_config", lambda path: f"resolved:{path}"
    )
    assert (
        cli_common._workflow_root_for_args(SimpleNamespace(workflow_root=None))
        == "resolved:/tmp/orca_auto.yaml"
    )

    with pytest.raises(ValueError, match="workflow_type must be one of"):
        cli_common._normalize_workflow_type("unknown")

    assert (
        run_dir_options._resolve_text_option_with_section(
            "  cli-value  ", {}, "key", {}, "section_key", "fallback"
        )
        == "cli-value"
    )
    assert run_dir_options._resolve_int_option(7, {}, "key", 1) == 7
    assert run_dir_options._resolve_int_option_with_section(9, {}, "key", {}, "section_key", 1) == 9


def test_cli_shared_config_and_worker_root_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflows"
    workflow_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(
        "\n".join(
            [
                "workflow:",
                f"  root: {workflow_root}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli_common, "_discover_shared_config_path", lambda explicit: str(config_path.resolve())
    )
    args = SimpleNamespace(orca_auto_config=None, orca_config=None, workflow_root=None)

    assert cli_common._shared_orca_auto_config(args) == str(config_path.resolve())
    assert cli_common._workflow_root_for_args(args, config_path=str(config_path)) == str(
        workflow_root.resolve()
    )

    explicit_config = tmp_path / "explicit.yaml"
    explicit_args = SimpleNamespace(orca_auto_config=str(explicit_config), orca_config=None)
    assert cli_common._shared_orca_auto_config(explicit_args) == str(explicit_config.resolve())


def test_cli_run_dir_manifest_and_path_resolution_edges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workflow_dir = tmp_path / "workflow_dir"
    workflow_dir.mkdir()

    monkeypatch.setattr(run_dir_manifest, "WORKFLOW_MANIFEST_FILENAMES", ("flow.json",))

    (workflow_dir / "flow.json").write_text(
        '{"workflow_type": "reaction_ts_search"}', encoding="utf-8"
    )
    assert run_dir_manifest._load_run_dir_manifest(workflow_dir) == {
        "workflow_type": "reaction_ts_search"
    }

    (workflow_dir / "flow.json").write_text("null", encoding="utf-8")
    assert run_dir_manifest._load_run_dir_manifest(workflow_dir) == {}

    (workflow_dir / "flow.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="Run directory manifest must contain a mapping"):
        run_dir_manifest._load_run_dir_manifest(workflow_dir)

    assert run_dir_manifest._resolve_run_dir_path(
        workflow_dir,
        explicit=None,
        manifest={"input_xyz": "inputs/input.xyz"},
        key="input_xyz",
        default_names=("unused.xyz",),
    ) == str((workflow_dir / "inputs" / "input.xyz").resolve())


def test_run_dir_workflow_options_apply_cli_manifest_section_default_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_dir_options,
        "_resolve_required_workflow_root",
        lambda args, manifest: getattr(args, "workflow_root", None),
    )
    args = SimpleNamespace(
        workflow_root="/tmp/cli_root",
        crest_mode="cli_nci",
        priority=None,
        max_cores=None,
        max_memory_gb=96,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
    )
    sections = run_dir_options.RunDirManifestSections(
        resources={"max_cores": 12, "max_memory_gb": 48},
        crest={"mode": "section_nci"},
        xtb={},
        endpoint_pairing={},
        orca={"route_line": "! section", "charge": -2, "multiplicity": 3},
    )

    options = run_dir_options._resolve_run_dir_workflow_options(
        args,
        {
            "crest_mode": "manifest_nci",
            "priority": 7,
            "max_cores": 20,
            "orca_route_line": "! manifest",
            "max_crest_candidates": 4,
            "max_xtb_stages": 5,
        },
        sections,
        default_orca_route_line="! default",
        default_max_orca_stages=3,
    )

    assert options.workflow_root == "/tmp/cli_root"
    assert options.crest_mode == "cli_nci"
    assert options.max_memory_gb == 96
    assert options.priority == 7
    assert options.max_cores == 20
    assert options.orca_route_line == "! manifest"
    assert options.max_crest_candidates == 4
    assert options.max_xtb_stages == 5
    assert options.charge == -2
    assert options.multiplicity == 3
    assert options.max_orca_stages == 3


def test_required_workflow_root_uses_manifest_before_shared_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_root = tmp_path / "manifest_workflows"
    monkeypatch.setattr(
        run_dir_options,
        "_cli_workflow_root_for_args",
        lambda args, *, config_path=None: (_ for _ in ()).throw(
            AssertionError("shared config should not be read")
        ),
    )

    resolved = run_dir_options._resolve_required_workflow_root(
        SimpleNamespace(workflow_root=None, config=None, orca_auto_config=None),
        {"workflow": {"root": str(manifest_root)}},
    )

    assert resolved == str(manifest_root.resolve())


@pytest.mark.parametrize(
    ("manifest", "sections", "message"),
    [
        ({"max_cores": 0}, {}, "max_cores must be >= 1"),
        ({}, {"resources": {"max_memory_gb": 0}}, "max_memory_gb must be >= 1"),
        ({"max_orca_stages": 0}, {}, "max_orca_stages must be >= 1"),
        ({"max_crest_candidates": 0}, {}, "max_crest_candidates must be >= 1"),
        ({"max_xtb_stages": 0}, {}, "max_xtb_stages must be >= 1"),
        ({"multiplicity": 0}, {}, "multiplicity must be >= 1"),
        ({"max_cores": "many"}, {}, "max_cores must be an integer >= 1"),
        ({}, {"orca": {"multiplicity": "singlet-ish"}}, "multiplicity must be an integer >= 1"),
        ({"charge": "negative-ish"}, {}, "charge must be an integer"),
    ],
)
def test_run_dir_workflow_options_reject_non_positive_resource_and_stage_limits(
    monkeypatch: pytest.MonkeyPatch,
    manifest: dict[str, Any],
    sections: dict[str, dict[str, Any]],
    message: str,
) -> None:
    monkeypatch.setattr(
        run_dir_options,
        "_resolve_required_workflow_root",
        lambda args, manifest: getattr(args, "workflow_root", None),
    )
    args = SimpleNamespace(
        workflow_root="/tmp/cli_root",
        crest_mode=None,
        priority=None,
        max_cores=None,
        max_memory_gb=None,
        max_orca_stages=None,
        orca_route_line=None,
        charge=None,
        multiplicity=None,
        max_crest_candidates=None,
        max_xtb_stages=None,
    )
    resolved_sections = run_dir_options.RunDirManifestSections(
        resources=sections.get("resources", {}),
        crest={},
        xtb={},
        endpoint_pairing={},
        orca=sections.get("orca", {}),
    )

    with pytest.raises(ValueError, match=message):
        run_dir_options._resolve_run_dir_workflow_options(
            args,
            manifest,
            resolved_sections,
            default_orca_route_line="! default",
            default_max_orca_stages=3,
        )


def test_run_dir_manifest_sections_resolve_paths_and_merge_endpoint_pairing(
    tmp_path: Path,
) -> None:
    workflow_dir = tmp_path / "workflow_dir"
    workflow_dir.mkdir()
    (workflow_dir / "controls").mkdir()
    (workflow_dir / "controls" / "path.inp").write_text("$path\n$end\n", encoding="utf-8")

    sections = run_dir_manifest._resolve_run_dir_manifest_sections(
        workflow_dir,
        {
            "xtb": {
                "gfn": 1,
                "xcontrol_file": "controls/path.inp",
                "endpoint_pairing": {"enabled": False, "max_distance_rmsd": 0.4},
            },
            "endpoint_pairing": {"enabled": True, "comparison_atoms": [1, 2]},
        },
    )

    assert sections.xtb == {
        "gfn": 1,
        "xcontrol_file": str((workflow_dir / "controls" / "path.inp").resolve()),
    }
    assert sections.endpoint_pairing == {
        "enabled": True,
        "max_distance_rmsd": 0.4,
        "comparison_atoms": [1, 2],
    }


def test_unique_run_dir_workflow_id_adds_suffix_for_existing_workspace(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflow_root"
    workflow_root.mkdir()
    workflow_dir = tmp_path / "runs" / "reaction job"
    workflow_dir.mkdir(parents=True)
    (workflow_root / "wf_reaction_ts_reaction_job").mkdir()

    assert (
        run_dir_cli._unique_run_dir_workflow_id(
            workflow_dir,
            workflow_root=workflow_root,
            workflow_type="reaction_ts_search",
        )
        == "wf_reaction_ts_reaction_job_02"
    )


def test_cmd_run_dir_reports_invalid_directory_and_unknown_layout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_dir = tmp_path / "missing"
    invalid_args = SimpleNamespace(
        workflow_dir=str(missing_dir),
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
    assert run_dir_cli.cmd_run_dir(invalid_args) == 1
    assert "workflow_dir does not exist or is not a directory" in capsys.readouterr().err

    empty_workflow_dir = tmp_path / "empty_workflow"
    empty_workflow_dir.mkdir()
    unknown_args = SimpleNamespace(
        **{**invalid_args.__dict__, "workflow_dir": str(empty_workflow_dir)}
    )
    assert run_dir_cli.cmd_run_dir(unknown_args) == 1
    assert "workflow run-dir requires flow.yaml" in capsys.readouterr().err


def test_cmd_run_dir_for_conformer_uses_nested_crest_section(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "conformer_nested"
    workflow_dir.mkdir()
    (workflow_dir / "input.xyz").write_text("x", encoding="utf-8")
    (workflow_dir / "flow.yaml").write_text(
        "\n".join(
            [
                "workflow_type: conformer_screening",
                "resources:",
                "  max_cores: 10",
                "crest:",
                "  mode: nci",
                "  energy_window: 6.0",
                "orca:",
                '  route_line: "! opt"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        cli_common, "_discover_workflow_root", lambda explicit: "/tmp/workflow_root"
    )

    def fake_create_conformer_screening_workflow(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "workflow_id": "wf_conformer_nested",
            "metadata": {"workspace_dir": "/tmp/workflows/wf_conformer_nested"},
            "stages": [{}],
        }

    monkeypatch.setattr(
        run_dir_cli, "create_conformer_screening_workflow", fake_create_conformer_screening_workflow
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

    assert run_dir_cli.cmd_run_dir(args) == 0
    assert "workflow_id: wf_conformer_nested" in capsys.readouterr().out
    assert captured["crest_mode"] == "nci"
    assert captured["crest_job_manifest"] == {"mode": "nci", "energy_window": 6.0}
    assert captured["max_cores"] == 10
    assert captured["orca_route_line"] == "! opt"
