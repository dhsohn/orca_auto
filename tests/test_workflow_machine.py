from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orca_auto.core.machine_observation import machine_code
from orca_auto.flow.workflow.machine import (
    build_workflow_machine_observation,
    write_workflow_machine_observation,
)


def _validate_common_machine(path: Path) -> None:
    validator = os.environ.get("FACTORY_MACHINE_CONTRACT_VALIDATOR")
    if validator:
        subprocess.run([sys.executable, validator, "--machine", str(path)], check=True)


def _payload(workspace: Path, *, stages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "workflow_id": "wf-machine-01",
        "template_name": "reaction_ts_search",
        "status": "completed",
        "reaction_key": "R01-P01",
        "requested_at": "2026-08-09T12:00:00+00:00",
        "metadata": {
            "workspace_dir": str(workspace),
            "last_advanced_at": "2026-08-09T12:30:00+00:00",
        },
        "stages": stages,
    }


def test_machine_code_caps_long_dynamic_reasons_without_losing_identity() -> None:
    first = machine_code("orca_auto", "A" * 500, fallback="workflow_failed")
    second = machine_code("orca_auto", "B" * 500, fallback="workflow_failed")

    assert len(first) == 200
    assert first.startswith("orca_auto/aaaa")
    assert first == machine_code("orca_auto", "A" * 500, fallback="workflow_failed")
    assert first != second


def test_completed_workflow_publishes_one_ready_machine_observation(tmp_path: Path) -> None:
    (tmp_path / "workflow_report.html").write_text("<html>done</html>\n", encoding="utf-8")
    payload = _payload(tmp_path, stages=[])

    path = write_workflow_machine_observation(tmp_path, payload)

    assert path == tmp_path / "machine.json"
    observation = json.loads(path.read_text(encoding="utf-8"))
    _validate_common_machine(path)
    assert set(observation) == {
        "artifacts",
        "contract",
        "delivery",
        "handoff",
        "lifecycle",
        "lineage",
        "operation",
        "payload",
        "producer",
    }
    assert observation["operation"] == {
        "id": "wf-machine-01",
        "kind": "chemistry/workflow",
    }
    assert observation["handoff"] == {"status": "ready", "codes": []}
    assert observation["delivery"] == {"status": "complete", "codes": []}
    assert observation["payload"]["contract"] == {
        "name": "chemistry/results-bundle",
        "version": 1,
    }
    original_identity = (path.stat().st_dev, path.stat().st_ino)
    assert write_workflow_machine_observation(tmp_path, payload) == path
    assert (path.stat().st_dev, path.stat().st_ino) == original_identity
    with pytest.raises(RuntimeError, match="terminal workflow machine observation is immutable"):
        write_workflow_machine_observation(
            tmp_path,
            {**payload, "reaction_key": "changed"},
        )


def test_upstream_lineage_reads_only_inside_the_workspace(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from orca_auto.flow.workflow.machine import _upstream_orca_observations

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workflow_report.html").write_text("<html>done</html>\n", encoding="utf-8")
    assert write_workflow_machine_observation(outside, _payload(outside, stages=[])) is not None

    workspace = tmp_path / "ws"
    inside_job = workspace / "jobs" / "step1"
    inside_job.mkdir(parents=True)
    (inside_job / "workflow_report.html").write_text("<html>done</html>\n", encoding="utf-8")
    assert (
        write_workflow_machine_observation(inside_job, _payload(inside_job, stages=[])) is not None
    )

    report_data = SimpleNamespace(
        orca_results=[
            SimpleNamespace(report_href="jobs/step1/job_report.html"),
            SimpleNamespace(report_href="../outside/job_report.html"),
            SimpleNamespace(report_href=str(outside / "job_report.html")),
        ]
    )

    upstream = _upstream_orca_observations(workspace, report_data)

    assert len(upstream) == 1
    assert upstream[0]["operation_id"] == "wf-machine-01"


def test_upstream_lineage_skips_symlinked_observations(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from orca_auto.flow.workflow.machine import _upstream_orca_observations

    workspace = tmp_path / "ws"
    real_job = workspace / "jobs" / "step1"
    real_job.mkdir(parents=True)
    (real_job / "workflow_report.html").write_text("<html>done</html>\n", encoding="utf-8")
    assert write_workflow_machine_observation(real_job, _payload(real_job, stages=[])) is not None

    linked_job = workspace / "jobs" / "step2"
    linked_job.mkdir()
    (linked_job / "machine.json").symlink_to(real_job / "machine.json")

    report_data = SimpleNamespace(
        orca_results=[
            SimpleNamespace(report_href="jobs/step2/job_report.html"),
        ]
    )

    assert _upstream_orca_observations(workspace, report_data) == []


def test_workflow_with_orca_stage_blocks_when_required_si_is_missing(tmp_path: Path) -> None:
    (tmp_path / "workflow_report.html").write_text("<html>done</html>\n", encoding="utf-8")
    payload = _payload(
        tmp_path,
        stages=[
            {
                "stage_id": "orca-01",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "status": "completed"},
            }
        ],
    )

    observation = build_workflow_machine_observation(tmp_path, payload)

    assert observation is not None
    assert observation["lifecycle"]["outcome"] == "succeeded"
    assert observation["handoff"]["status"] == "blocked"
    assert observation["delivery"]["status"] == "incomplete"
    assert observation["artifacts"]["supporting-information"]["status"] == "missing"
