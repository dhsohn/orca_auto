from __future__ import annotations

from pathlib import Path

import pytest

from orca_auto.flow.engine_options import WorkflowEngineOptions
from orca_auto.flow.runtime.admission import workflow_submission_has_capacity
from orca_auto.flow.runtime.cycle import WorkflowCycleDeps, start_workflow_cycle_with_deps
from orca_auto.flow.runtime.models import WorkflowRuntimeContext


def test_workflow_shared_config_applies_to_crest_and_orca() -> None:
    options = WorkflowEngineOptions.from_values(shared_config="shared.yaml")

    assert options.crest_config == options.orca_config == options.shared_config == "shared.yaml"


@pytest.mark.parametrize(
    ("crest_config", "orca_config", "expected_shared"),
    [("crest.yaml", "orca.yaml", "crest.yaml"), (None, "orca.yaml", "orca.yaml")],
)
def test_workflow_shared_config_prefers_crest_then_orca(
    crest_config: str | None,
    orca_config: str,
    expected_shared: str,
) -> None:
    options = WorkflowEngineOptions.from_values(crest_config=crest_config, orca_config=orca_config)

    assert options.shared_config == expected_shared


@pytest.mark.parametrize(
    ("crest_config", "crest_capacity", "orca_capacity", "expected_paths", "blocked"),
    [
        ("crest.yaml", True, False, ["crest.yaml"], False),
        ("crest.yaml", False, True, ["crest.yaml"], True),
        ("crest.yaml", None, True, ["crest.yaml", "orca.yaml"], False),
        (None, None, True, ["orca.yaml"], False),
        (None, None, False, ["orca.yaml"], True),
    ],
)
def test_workflow_cycle_checks_only_retained_engine_configs(
    tmp_path: Path,
    crest_config: str | None,
    crest_capacity: bool | None,
    orca_capacity: bool,
    expected_paths: list[str],
    blocked: bool,
) -> None:
    options = WorkflowEngineOptions.from_values(crest_config=crest_config, orca_config="orca.yaml")
    inspected: list[str] = []

    def capacity(path: str | Path) -> bool | None:
        inspected.append(str(path))
        return crest_capacity if path == "crest.yaml" else orca_capacity

    def workflow_capacity(*paths: str | Path | None) -> bool:
        return workflow_submission_has_capacity(
            *paths, submission_admission_has_capacity_fn=capacity
        )

    cycle = start_workflow_cycle_with_deps(
        context=WorkflowRuntimeContext(root=tmp_path, options=options),
        deps=WorkflowCycleDeps(
            now_utc_iso_fn=lambda: "2026-09-13T00:00:00Z",
            timestamped_token_fn=lambda _prefix: "test-worker",
            workflow_submission_has_capacity_fn=workflow_capacity,
        ),
    )

    assert inspected == expected_paths
    assert cycle.admission_blocked is blocked
    assert cycle.cycle_submit_ready is not blocked
