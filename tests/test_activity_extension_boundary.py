from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto import activity
from orca_auto.activity import _clear, _collectors, _sources
from orca_auto.core.queue.persistence import entry_to_dict
from orca_auto.core.queue.types import QueueEntry, QueueStatus


def _standalone_queue(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runs"
    root.mkdir()
    config = tmp_path / "orca_auto.yaml"
    config.write_text(f"runs_root: {root}\n", encoding="utf-8")
    entry = QueueEntry(
        queue_id="standalone-job",
        task_id="standalone-task",
        app_name="orca_auto_orca",
        engine="orca",
        task_kind="orca_run_inp",
        status=QueueStatus.PENDING,
        priority=0,
        enqueued_at="2026-01-01T00:00:00Z",
        metadata={"reaction_dir": str(root / "standalone")},
    )
    (root / "queue.json").write_text(json.dumps([entry_to_dict(entry)]), encoding="utf-8")
    return config, root


@pytest.fixture
def without_workflows(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (_sources, _collectors, _clear):
        monkeypatch.setattr(module, "workflows_available", lambda: False)


@pytest.mark.usefixtures("without_workflows")
def test_core_only_activity_retains_the_standalone_queue(tmp_path: Path) -> None:
    config, root = _standalone_queue(tmp_path)
    original = (root / "queue.json").read_bytes()

    payload = activity.list_activities(shared_config=str(config))
    assert payload["count"] == 1
    assert payload["activities"][0]["activity_id"] == "standalone-job"
    assert payload["activities"][0]["status"] == "pending"
    assert activity.clear_activities(shared_config=str(config))["total_cleared"] == 0
    assert (root / "queue.json").read_bytes() == original


@pytest.mark.usefixtures("without_workflows")
@pytest.mark.parametrize(
    "evidence",
    [
        "registry",
        "registry_symlink",
        "scaffold_symlink",
        "workspace",
        "scaffold_generation",
        "stage",
        "queue_owner",
    ],
)
@pytest.mark.parametrize("operation", ["list", "cancel", "clear"])
def test_core_only_activity_refuses_workflow_state_before_mutating(
    tmp_path: Path, evidence: str, operation: str
) -> None:
    config, root = _standalone_queue(tmp_path)
    if evidence == "registry":
        (root / "workflow_registry.json").write_text("not valid JSON", encoding="utf-8")
    elif evidence == "registry_symlink":
        (root / "workflow_registry.json").symlink_to(root / "absent-registry")
    elif evidence == "scaffold_symlink":
        scaffold = root / "workflow"
        scaffold.mkdir()
        (scaffold / "flow.yaml").symlink_to(scaffold / "missing.yaml")
    elif evidence in {"workspace", "scaffold_generation", "stage"}:
        workspace = root / "workflow"
        workspace.mkdir()
        if evidence == "scaffold_generation":
            (workspace / "flow.yaml").write_text("template: conformer_search\n", encoding="utf-8")
            workspace = workspace / "20260912-120000-1234abcd"
            workspace.mkdir()
        if evidence == "stage":
            (workspace / "03_orca").mkdir()
        else:
            (workspace / "workflow.json").write_text("{}", encoding="utf-8")
    elif evidence == "queue_owner":
        queue = json.loads((root / "queue.json").read_text(encoding="utf-8"))
        queue[0]["metadata"]["workflow_id"] = "missing-workflow"
        (root / "queue.json").write_text(json.dumps(queue), encoding="utf-8")
    original = (root / "queue.json").read_bytes()

    with pytest.raises(ValueError, match="Workflow state exists.*restore.*workflows extension"):
        if operation == "list":
            activity.list_activities(shared_config=str(config), refresh=True)
        elif operation == "cancel":
            activity.cancel_activity(target="standalone-job", shared_config=str(config))
        else:
            activity.clear_activities(shared_config=str(config))

    assert (root / "queue.json").read_bytes() == original


@pytest.mark.usefixtures("without_workflows")
@pytest.mark.parametrize("engine", ["xtb", "crest"])
def test_core_only_activity_does_not_hide_foreign_engine_rows(tmp_path: Path, engine: str) -> None:
    config, root = _standalone_queue(tmp_path)
    queue = json.loads((root / "queue.json").read_text(encoding="utf-8"))
    queue[0].update(app_name=f"orca_auto_{engine}", engine=engine)
    (root / "queue.json").write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(ValueError, match="Workflow state exists"):
        activity.list_activities(shared_config=str(config))


@pytest.mark.usefixtures("without_workflows")
def test_core_only_clear_propagates_unreadable_workflow_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, root = _standalone_queue(tmp_path)
    original = (root / "queue.json").read_bytes()

    def unreadable(_root: Path) -> list[Path]:
        raise PermissionError("workflow root cannot be inspected")

    monkeypatch.setattr(_sources, "iter_workflow_runtime_workspaces", unreadable)
    with pytest.raises(PermissionError, match="workflow root cannot be inspected"):
        activity.clear_activities(shared_config=str(config))
    assert (root / "queue.json").read_bytes() == original


@pytest.mark.usefixtures("without_workflows")
def test_core_only_activity_refuses_a_scaffold_with_unreadable_generations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, root = _standalone_queue(tmp_path)
    scaffold = root / "workflow"
    scaffold.mkdir()
    (scaffold / "flow.yaml").write_text("template: conformer_search\n", encoding="utf-8")
    original = (root / "queue.json").read_bytes()
    original_iterdir = Path.iterdir

    def unreadable_generations(path: Path):
        if path == scaffold:
            raise PermissionError("scaffold generations cannot be inspected")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", unreadable_generations)
    with pytest.raises(ValueError, match="Workflow state exists"):
        activity.list_activities(shared_config=str(config))
    assert (root / "queue.json").read_bytes() == original


@pytest.mark.usefixtures("without_workflows")
@pytest.mark.parametrize("engine", ["crest", "xtb"])
def test_core_only_clear_inspects_each_explicit_engine_config_root(
    tmp_path: Path, engine: str
) -> None:
    config, root = _standalone_queue(tmp_path)
    alternate_root = tmp_path / "alternate-runs"
    alternate_root.mkdir()
    (alternate_root / "workflow_registry.json").write_text("[]", encoding="utf-8")
    alternate_config = tmp_path / "alternate.yaml"
    alternate_config.write_text(f"runs_root: {alternate_root}\n", encoding="utf-8")
    original = (root / "queue.json").read_bytes()

    with pytest.raises(ValueError, match="Workflow state exists.*alternate-runs"):
        activity.clear_activities(
            shared_config=str(config), **{f"{engine}_config": str(alternate_config)}
        )
    assert (root / "queue.json").read_bytes() == original
