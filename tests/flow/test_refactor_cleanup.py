from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow import orchestration, runtime, runtime_advance
from orca_auto.flow.orchestration import (
    dep_builders,
    reaction_materialization,
    reaction_orca_materialization,
)
from orca_auto.flow.orchestration import materialization as stage_materialization
from orca_auto.flow.orchestration.builders import _copy_input_impl
from orca_auto.flow.orchestration.dep_types import (
    _ORCHESTRATION_STAGE_DEP_REGISTRY,
    OrchestrationStageDeps,
)
from orca_auto.flow.orchestration.deps import orchestration_deps


def test_copy_input_impl_copies_file_and_raises_for_missing_source(tmp_path: Path) -> None:
    source = tmp_path / "source.xyz"
    source.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.7\n", encoding="utf-8")
    target = tmp_path / "nested" / "copied.xyz"

    copied = _copy_input_impl(str(source), target)

    assert copied == str(target.resolve())
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="Input XYZ not found"):
        _copy_input_impl(str(tmp_path / "missing.xyz"), tmp_path / "other.xyz")


def test_orchestration_modules_live_under_orchestration_package() -> None:
    flow_root = Path(__file__).resolve().parents[2] / "src" / "orca_auto" / "flow"

    assert not list(flow_root.glob("_orchestration*.py"))
    assert (flow_root / "orchestration" / "__init__.py").is_file()
    assert (flow_root / "orchestration" / "stage_runtime" / "__init__.py").is_file()


def test_orchestration_deps_use_explicit_overrides_not_public_module_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_now() -> str:
        return "fake"

    monkeypatch.setattr(orchestration, "now_utc_iso", fake_now)

    assert orchestration_deps().persistence.now_utc_iso is not fake_now
    assert (
        orchestration_deps(overrides={"now_utc_iso": fake_now}).persistence.now_utc_iso is fake_now
    )


def test_orchestration_stage_deps_require_explicit_group_access() -> None:
    def fake_normalize(value: object) -> str:
        return f"normalized:{value}"

    deps = orchestration_deps(overrides={"_normalize_text": fake_normalize})

    assert deps.stages.support._normalize_text("x") == "normalized:x"
    assert deps.stages.runtime._append_unique_artifact is not None
    missing_dep = "_not_a_stage_dep"
    with pytest.raises(AttributeError, match="OrchestrationStageDeps"):
        getattr(deps.stages, missing_dep)
    removed_passthrough_dep = "_normalize_text"
    with pytest.raises(AttributeError, match="OrchestrationStageDeps"):
        getattr(deps.stages, removed_passthrough_dep)


def test_stage_dep_registry_matches_group_dataclasses() -> None:
    stage_group_names = {field.name for field in fields(OrchestrationStageDeps)}

    for group in _ORCHESTRATION_STAGE_DEP_REGISTRY:
        assert group.name in stage_group_names
        assert tuple(field.name for field in fields(group.deps_type)) == group.dep_names


def test_stage_dep_fallbacks_cover_registry_names() -> None:
    provider = dep_builders._LazyOrchestrationDeps(None)
    fallback_names = set(dep_builders._stage_dep_fallbacks(None, provider))
    registry_names = {
        dep_name for group in _ORCHESTRATION_STAGE_DEP_REGISTRY for dep_name in group.dep_names
    }

    assert fallback_names == registry_names


def test_stage_dep_fallback_groups_follow_stage_registry() -> None:
    provider = dep_builders._LazyOrchestrationDeps(None)
    fallback_groups = dep_builders._stage_dep_fallback_groups(None, provider)

    assert tuple(group.dep_group for group in fallback_groups) == _ORCHESTRATION_STAGE_DEP_REGISTRY
    for group in fallback_groups:
        assert set(group.fallbacks) == set(group.dep_group.dep_names)


def test_runtime_facade_keeps_advance_helpers_available() -> None:
    assert runtime.WorkflowAdvanceDeps is runtime_advance.WorkflowAdvanceDeps
    assert runtime.WorkflowAdvanceOutcome is runtime_advance.WorkflowAdvanceOutcome
    assert (
        runtime._advance_workflow_record_outcome is runtime_advance.advance_workflow_record_outcome
    )
    assert runtime._advanced_workflow_outcome is runtime_advance.advanced_workflow_outcome


def test_reaction_materialization_facades_keep_orca_entrypoint() -> None:
    assert (
        reaction_materialization.append_reaction_orca_stages_impl
        is reaction_orca_materialization.append_reaction_orca_stages_impl
    )
    assert (
        stage_materialization.append_reaction_orca_stages_impl
        is reaction_orca_materialization.append_reaction_orca_stages_impl
    )


def test_bound_stage_deps_reuse_lazy_orchestration_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    build_contract_deps = dep_builders._build_contract_deps

    def count_builds(overrides: dict[str, Any] | None) -> Any:
        nonlocal calls
        calls += 1
        return build_contract_deps(overrides)

    monkeypatch.setattr(dep_builders, "_build_contract_deps", count_builds)

    deps = orchestration_deps()
    rows: list[dict[str, Any]] = []
    deps.stages.runtime._append_unique_artifact(rows, kind="xyz", path="a.xyz")
    deps.stages.runtime._append_unique_artifact(rows, kind="log", path="b.log", deps=None)

    assert calls == 1
    assert [row["path"] for row in rows] == ["a.xyz", "b.log"]
