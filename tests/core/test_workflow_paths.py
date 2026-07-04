from __future__ import annotations

from pathlib import Path

from orca_auto.core.paths.workflow import (
    workflow_stage_dirnames_for_engine,
    workflow_workspace_internal_engine_paths_from_path,
)


def test_orca_workflow_stage_dirnames_include_conformer_and_reaction_roots() -> None:
    assert workflow_stage_dirnames_for_engine("orca") == ("03_orca", "02_orca")


def test_workflow_paths_from_path_accepts_conformer_orca_root(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    reaction_dir = workflow_root / "wf_conformer" / "02_orca" / "01_conformer"
    reaction_dir.mkdir(parents=True)

    paths = workflow_workspace_internal_engine_paths_from_path(
        reaction_dir,
        workflow_root=workflow_root,
        engine="orca",
    )

    assert paths == {
        "allowed_root": (workflow_root / "wf_conformer" / "02_orca").resolve(),
    }
