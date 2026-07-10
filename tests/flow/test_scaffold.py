from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from orca_auto.flow import scaffold


def test_cmd_scaffold_creates_reaction_workflow_scaffold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_dir = tmp_path / "reaction_workflow"

    rc = scaffold.cmd_scaffold(
        Namespace(
            root=str(workflow_dir),
            workflow_type="reaction_ts_search",
        )
    )

    output = capsys.readouterr().out
    flow_text = (workflow_dir / "flow.yaml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(flow_text)
    readme = (workflow_dir / "README.md").read_text(encoding="utf-8")

    assert rc == 0
    assert (workflow_dir / "reactant.xyz").exists()
    assert (workflow_dir / "product.xyz").exists()
    assert (workflow_dir / "README.md").exists()
    assert manifest["workflow_type"] == "reaction_ts_search"
    assert manifest["crest_mode"] == "standard"
    assert manifest["max_crest_candidates"] == 3
    assert "max_xtb_stages" not in manifest
    assert "max_orca_stages" not in manifest
    assert "workflow_type: reaction_ts_search" in output
    assert "crest_mode: standard" in output
    assert "created_file: reactant.xyz" in output
    assert "created_file: product.xyz" in output
    assert "# crest:" in flow_text
    assert "#   gfn: ff" in flow_text
    assert "#   no_preopt: true" in flow_text
    assert "#   noreftopo: true" in flow_text
    assert "#   notopo: true" in flow_text
    assert "#   nocbonds: true" in flow_text
    assert "orca_auto scaffold ts_search" in readme
    assert "crest_mode: nci" in readme
    assert "`gfn: ff`, `noreftopo: true`, `notopo: true`, or `nocbonds: true`" in readme
    assert "waits for the xTB phase to finish" in readme


def test_cmd_scaffold_is_idempotent_for_conformer_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "conformer_workflow"

    first_rc = scaffold.cmd_scaffold(
        Namespace(
            root=str(workflow_dir),
            workflow_type="conformer_screening",
        )
    )
    assert first_rc == 0
    flow_text = (workflow_dir / "flow.yaml").read_text(encoding="utf-8")
    manifest = yaml.safe_load(flow_text)
    readme = (workflow_dir / "README.md").read_text(encoding="utf-8")
    assert manifest["max_orca_stages"] == 20
    assert "#   gfn: ff" in flow_text
    assert "#   no_preopt: true" in flow_text
    assert "#   noreftopo: true" in flow_text
    assert "#   notopo: true" in flow_text
    assert "#   nocbonds: true" in flow_text
    assert "20 retained CREST conformers" in readme
    assert "`gfn: ff`, `noreftopo: true`, `notopo: true`, or `nocbonds: true`" in readme
    capsys.readouterr()

    custom_input = "1\ncustom\nHe 0.0 0.0 0.0\n"
    (workflow_dir / "input.xyz").write_text(custom_input, encoding="utf-8")

    second_rc = scaffold.cmd_scaffold(
        Namespace(
            root=str(workflow_dir),
            workflow_type="conformer_screening",
        )
    )

    output = capsys.readouterr().out
    assert second_rc == 0
    assert "created: 0" in output
    assert "skipped: 3" in output
    assert "skipped_file: input.xyz" in output
    assert (workflow_dir / "input.xyz").read_text(encoding="utf-8") == custom_input


def test_cmd_scaffold_rejects_invalid_crest_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow_dir = tmp_path / "bad_mode"

    rc = scaffold.cmd_scaffold(
        Namespace(
            root=str(workflow_dir),
            workflow_type="reaction_ts_search",
            crest_mode="fast",
        )
    )

    output = capsys.readouterr().err
    assert rc == 1
    assert "error: unsupported crest_mode: fast" in output


def test_cmd_scaffold_rejects_parenthesized_workflow_name_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow_dir = tmp_path / "TS8(wf)"

    rc = scaffold.cmd_scaffold(
        Namespace(
            root=str(workflow_dir),
            workflow_type="reaction_ts_search",
        )
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "workflow_id cannot contain parentheses" in captured.err
    assert "TS8_wf" in captured.err
    assert not workflow_dir.exists()
