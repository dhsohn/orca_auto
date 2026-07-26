from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.indexing import roots


def _cfg(allowed_root: Path, *, workflow_root: Path | str = "") -> Any:
    return SimpleNamespace(
        runtime=SimpleNamespace(allowed_root=str(allowed_root)),
        workflow_root=str(workflow_root),
    )


def test_runtime_roots_for_cfg_falls_back_to_runtime_root_without_workflow(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"

    assert roots.runtime_roots_for_cfg(_cfg(allowed_root), engine="xtb") == (
        allowed_root.resolve(),
    )


def test_runtime_roots_for_cfg_deduplicates_workflow_engine_roots(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workflow_root = tmp_path / "workflows"
    first_workspace = workflow_root / "wf-1"
    second_workspace = workflow_root / "wf-2"
    first_engine_root = tmp_path / "wf-1" / "02_xtb"
    second_engine_root = tmp_path / "wf-2" / "02_xtb"
    first_engine_root.mkdir(parents=True)
    second_engine_root.mkdir(parents=True)

    monkeypatch.setattr(
        roots,
        "iter_workflow_runtime_workspaces",
        lambda _root, engine: (first_workspace, first_workspace, second_workspace),
    )
    monkeypatch.setattr(
        roots,
        "workflow_workspace_internal_engine_paths",
        lambda workspace, engine, **kwargs: {
            "allowed_root": first_engine_root
            if workspace == first_workspace
            else second_engine_root,
        },
    )

    assert roots.runtime_roots_for_cfg(
        _cfg(tmp_path / "fallback", workflow_root=workflow_root),
        engine="xtb",
    ) == (
        (tmp_path / "fallback").resolve(),
        first_engine_root.resolve(),
        second_engine_root.resolve(),
    )


def test_runtime_roots_for_cfg_includes_canonical_orca_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workflow_root = tmp_path / "workflows"
    workspace = workflow_root / "wf-orca"
    reaction_orca_root = workspace / "03_orca"
    reaction_orca_root.mkdir(parents=True)

    monkeypatch.setattr(
        roots,
        "iter_workflow_runtime_workspaces",
        lambda _root, engine: (workspace,),
    )

    assert roots.runtime_roots_for_cfg(
        _cfg(tmp_path / "fallback", workflow_root=workflow_root),
        engine="orca",
    ) == (
        (tmp_path / "fallback").resolve(),
        reaction_orca_root.resolve(),
    )


def test_index_root_for_path_prefers_matching_workflow_runtime_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workflow_root = tmp_path / "workflows"
    engine_root = workflow_root / "wf-1" / "02_xtb"

    def fake_paths_from_path(
        path: str, *, workflow_root: str, engine: str
    ) -> dict[str, Path] | None:
        if path.endswith("job"):
            return {"allowed_root": engine_root}
        return None

    monkeypatch.setattr(
        roots,
        "workflow_workspace_internal_engine_paths_from_path",
        fake_paths_from_path,
    )

    cfg = _cfg(tmp_path / "fallback", workflow_root=workflow_root)

    assert roots.index_root_for_path(cfg, "", str(tmp_path / "job"), engine="xtb") == (
        engine_root.resolve()
    )
    assert (
        roots.index_root_for_path(cfg, str(tmp_path / "other"), engine="xtb")
        == (tmp_path / "fallback").resolve()
    )


def test_load_job_artifacts_for_cfg_skips_roots_without_resolved_job_dir(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    root_one = tmp_path / "root-one"
    root_two = tmp_path / "root-two"
    job_dir = tmp_path / "job"
    state = {"status": "completed"}
    report = {"job_id": "job-1"}

    monkeypatch.setattr(
        roots,
        "lookup_roots_for_target",
        lambda _cfg, _target, engine: (root_one, root_two),
    )

    def resolve_latest(root: str | Path, _target: str) -> Path | None:
        return job_dir if Path(root) == root_two else None

    result = roots.load_job_artifacts_for_cfg(
        _cfg(tmp_path / "fallback"),
        "job-1",
        engine="xtb",
        load_state_fn=lambda resolved: state if resolved == job_dir else None,
        load_report_json_fn=lambda resolved: report if resolved == job_dir else None,
        resolve_latest_job_dir_fn=resolve_latest,
        resolve_job_location_fn=lambda _root, _target: None,
    )

    assert result == (job_dir, state, report, None)
