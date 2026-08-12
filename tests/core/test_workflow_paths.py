from __future__ import annotations

from pathlib import Path

from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    WORKFLOW_SCAFFOLD_MANIFEST_NAME,
    workflow_root_for_workspace,
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths_from_path,
)


def test_orca_workflow_stage_dirname_is_canonical() -> None:
    assert workflow_stage_dirnames_for_engine("orca") == ("03_orca",)


def test_workflow_paths_from_path_accepts_orca_root(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    reaction_dir = workflow_root / "wf_conformer" / "03_orca" / "01_conformer"
    reaction_dir.mkdir(parents=True)

    paths = workflow_workspace_internal_engine_paths_from_path(
        reaction_dir,
        workflow_root=workflow_root,
        engine="orca",
    )

    assert paths == {
        "allowed_root": (workflow_root / "wf_conformer" / "03_orca").resolve(),
    }


def test_workflow_paths_from_path_accepts_scaffold_nested_generation(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    reaction_dir = (
        workflow_root / "rxn_case" / "20260717-090000-0a1b2c3d" / "03_orca" / "01_ts_guess"
    )
    reaction_dir.mkdir(parents=True)
    # A generation-shaped name alone does not authorize workflow paths; the
    # scaffold manifest (or the workspace's committed workflow.json) does.
    (workflow_root / "rxn_case" / "flow.yaml").write_text("template: x\n", encoding="utf-8")

    paths = workflow_workspace_internal_engine_paths_from_path(
        reaction_dir,
        workflow_root=workflow_root,
        engine="orca",
    )

    assert paths == {
        "allowed_root": (
            workflow_root / "rxn_case" / "20260717-090000-0a1b2c3d" / "03_orca"
        ).resolve(),
    }


def test_workflow_root_for_workspace_distinguishes_scaffold_and_root_direct(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflows"
    scaffold = workflow_root / "rxn_case"
    nested_workspace = scaffold / "20260717-090000-0a1b2c3d"
    nested_workspace.mkdir(parents=True)
    (scaffold / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")

    # Direct API submissions mint generation-named workspaces at root itself.
    root_direct_workspace = workflow_root / "20260717-090001-0a1b2c3e"
    root_direct_workspace.mkdir(parents=True)

    assert workflow_root_for_workspace(nested_workspace) == workflow_root.resolve()
    assert workflow_root_for_workspace(root_direct_workspace) == workflow_root.resolve()


def test_planted_workflow_json_in_orca_generation_is_not_a_workspace(tmp_path: Path) -> None:
    """A workflow.json dropped inside a standalone ORCA execution generation
    (an attacker-influenced output surface) must never be trusted: not
    discovered as a workspace, not accepted as a run-dir target."""

    from orca_auto.core.commands.run_dir import validate_production_run_dir_target
    from orca_auto.core.paths.workflow import is_workflow_workspace_location
    from orca_auto.flow.workflow.store import iter_workflow_workspaces

    workflow_root = tmp_path / "runs"
    orca_job = workflow_root / "TS1"
    orca_generation = orca_job / "20260717-091500-0a1b2c3d"
    orca_generation.mkdir(parents=True)
    (orca_generation / "workflow.json").write_text(
        '{"workflow_id": "20260717-091500-0a1b2c3d"}', encoding="utf-8"
    )

    assert iter_workflow_workspaces(workflow_root) == []
    assert not is_workflow_workspace_location(orca_generation, workflow_root.resolve())
    try:
        validate_production_run_dir_target(orca_generation, workflow_root)
    except ValueError as error:
        assert "execution generation" in str(error)
    else:
        raise AssertionError("planted generation target must be rejected")


def test_scaffold_nested_and_root_direct_workspaces_are_valid_run_dir_targets(
    tmp_path: Path,
) -> None:
    from orca_auto.core.commands.run_dir import validate_production_run_dir_target
    from orca_auto.flow.workflow.store import iter_workflow_workspaces

    workflow_root = tmp_path / "runs"
    scaffold = workflow_root / "rxn_case"
    nested = scaffold / "20260717-091501-0a1b2c3e"
    nested.mkdir(parents=True)
    (scaffold / "flow.yaml").write_text("workflow_type: reaction_ts_search\n", encoding="utf-8")
    (nested / "workflow.json").write_text(
        '{"workflow_id": "20260717-091501-0a1b2c3e"}', encoding="utf-8"
    )
    root_direct = workflow_root / "20260717-091502-0a1b2c3f"
    root_direct.mkdir(parents=True)
    (root_direct / "workflow.json").write_text(
        '{"workflow_id": "20260717-091502-0a1b2c3f"}', encoding="utf-8"
    )

    assert sorted(item.name for item in iter_workflow_workspaces(workflow_root)) == [
        "20260717-091501-0a1b2c3e",
        "20260717-091502-0a1b2c3f",
    ]
    validate_production_run_dir_target(nested, workflow_root)
    validate_production_run_dir_target(root_direct, workflow_root)


def _workflow_paths(root: Path, target: Path):
    return workflow_workspace_internal_engine_paths_from_path(
        target,
        workflow_root=root,
        engine="orca",
    )


def test_generation_shape_alone_does_not_grant_workflow_paths(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    job = root / "plain_orca_job"
    generation = job / "20260717-120000-aabbccdd"
    stage = generation / "03_orca"
    stage.mkdir(parents=True)

    assert _workflow_paths(root, stage) is None


def test_scaffold_parent_grants_workflow_paths(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    scaffold = root / "reaction_scaffold"
    generation = scaffold / "20260717-120000-aabbccdd"
    stage = generation / "03_orca"
    stage.mkdir(parents=True)
    (scaffold / WORKFLOW_SCAFFOLD_MANIFEST_NAME).write_text("template: x\n", encoding="utf-8")

    assert _workflow_paths(root, stage) is not None


def test_committed_manifest_grants_workflow_paths_without_scaffold(tmp_path: Path) -> None:
    # The scaffold's mutable flow.yaml was removed after submission; the
    # committed workspace manifest still authorizes the workspace.
    root = tmp_path / "runs"
    scaffold = root / "reaction_scaffold"
    generation = scaffold / "20260717-120000-aabbccdd"
    stage = generation / "03_orca"
    stage.mkdir(parents=True)
    (generation / WORKFLOW_FILE_NAME).write_text("{}", encoding="utf-8")

    assert _workflow_paths(root, stage) is not None
