"""Hard removal is a rejection boundary, never an alias or silent migration."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from orca_auto.cli import build_parser
from orca_auto.flow.manifest import load_flow_manifest
from orca_auto.flow.orchestration import (
    advance_workflow,
    cancel_materialized_workflow,
    create_conformer_screening_workflow,
)
from orca_auto.flow.restart import restart_failed_workflow
from orca_auto.flow.restart.settings import _flow_restart_settings
from orca_auto.flow.templates import (
    WORKFLOW_SCAFFOLD_SHORTCUTS,
    WORKFLOW_TEMPLATE_IDS,
    normalize_workflow_template_id,
)


def test_only_conformer_template_is_registered() -> None:
    assert set(WORKFLOW_TEMPLATE_IDS) == {"conformer_screening"}
    assert {row[0] for row in WORKFLOW_SCAFFOLD_SHORTCUTS} == {"conformer_search"}


@pytest.mark.parametrize("shortcut", ["ts_search", "scan_ts"])
def test_removed_scaffold_is_rejected_without_creating_target(
    tmp_path: Path, shortcut: str
) -> None:
    target = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["scaffold", shortcut, str(target)])
    assert exc.value.code == 2
    assert not target.exists()


@pytest.mark.parametrize("template", ["reaction_ts_search", "scan_ts_search"])
def test_removed_template_is_not_normalized_or_aliased(template: str) -> None:
    with pytest.raises(ValueError, match="conformer_screening"):
        normalize_workflow_template_id(template)


@pytest.mark.parametrize("template", ["reaction_ts_search", "scan_ts_search"])
def test_restart_rejects_removed_manifest_type_even_with_conformer_state(
    tmp_path: Path, template: str
) -> None:
    (tmp_path / "flow.yaml").write_text(f"workflow_type: {template}\n", encoding="utf-8")
    payload = {"template_name": "conformer_screening"}
    before = json.dumps(payload)
    with pytest.raises(ValueError, match="conformer_screening"):
        _flow_restart_settings(tmp_path, payload)
    assert json.dumps(payload) == before


@pytest.mark.parametrize(
    "field",
    [
        "xtb",
        "endpoint_pairing",
        "reactant_xyz",
        "product_xyz",
        "max_crest_candidates",
        "max_xtb_stages",
        "max_xtb_handoff_retries",
        "scan_coordinate",
        "scan_peak_prominence_kcal",
        "orca_optts_route_line",
    ],
)
def test_removed_flow_options_are_rejected_at_load_and_restart(tmp_path: Path, field: str) -> None:
    (tmp_path / "flow.yaml").write_text(
        f"workflow_type: conformer_screening\n{field}: {{}}\n", encoding="utf-8"
    )
    payload = {"template_name": "conformer_screening"}
    before = json.dumps(payload)
    with pytest.raises(ValueError, match="unknown key"):
        load_flow_manifest(tmp_path)
    with pytest.raises(ValueError, match="unknown key"):
        _flow_restart_settings(tmp_path, payload)
    assert json.dumps(payload) == before


@pytest.mark.parametrize(
    "module",
    [
        "endpoint_pairing",
        "endpoint_pairing_selection",
        "geometry_validation",
        "hessian_utils",
        "orchestration.reaction_materialization",
        "orchestration.reaction_orca_materialization",
        "orchestration.scan_orca_materialization",
        "orchestration.stage_runtime.xtb_path_jobs",
        "orchestration.stage_runtime.xtb_retry",
        "orchestration.stage_runtime.xtb_handoff",
        "orchestration.stage_runtime.xtb_sync",
    ],
)
def test_removed_modules_have_no_compatibility_facade(module: str) -> None:
    assert importlib.util.find_spec(f"orca_auto.flow.{module}") is None


@pytest.mark.parametrize("template", ["reaction_ts_search", "scan_ts_search"])
@pytest.mark.parametrize("operation", ["advance", "restart", "cancel"])
def test_old_workflow_mutations_fail_without_changing_state_or_results(
    tmp_path: Path, template: str, operation: str
) -> None:
    source = tmp_path / "input.xyz"
    source.write_text("2\ninput\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    root = tmp_path / "workflows"
    payload = create_conformer_screening_workflow(input_xyz=str(source), workflow_root=root)
    workspace = Path(payload["metadata"]["workspace_dir"])
    payload["template_name"] = template
    payload["metadata"]["request"]["template_name"] = template
    payload["status"] = "failed"
    for stage in payload["stages"]:
        stage["status"] = stage["task"]["status"] = "failed"
    state = workspace / "workflow.json"
    state.write_text(json.dumps(payload), encoding="utf-8")
    (workspace / "historical-result.out").write_bytes(b"existing calculation evidence\n")

    def snapshot() -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and not path.name.endswith(".lock")
        }

    before = snapshot()
    with pytest.raises(ValueError, match="conformer_screening"):
        if operation == "restart":
            restart_failed_workflow(workspace_dir=workspace, workflow_root=root, force=True)
        elif operation == "cancel":
            cancel_materialized_workflow(target=str(workspace), workflow_root=root)
        else:
            advance_workflow(target=str(workspace), workflow_root=root)
    assert snapshot() == before
