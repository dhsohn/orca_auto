from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.core.engines.artifacts import (
    EngineArtifactInput,
    EngineArtifactJob,
    EngineArtifactProcess,
    EngineArtifactRecovery,
    EngineArtifactResources,
    EngineArtifactStatus,
    EngineArtifactTimestamps,
    build_engine_artifact_payload,
    load_engine_artifact_payload,
)


@pytest.mark.parametrize("engine", ["orca", "xtb", "crest"])
@pytest.mark.parametrize("state", ["running", "completed", "failed", "cancelled"])
def test_engine_artifact_payload_has_common_shape(engine: str, state: str) -> None:
    payload = build_engine_artifact_payload(
        engine=engine,
        job=EngineArtifactJob(
            id="job-1",
            queue_id="queue-1",
            dir="/tmp/job",
            app_name=f"orca_auto_{engine}",
            task_id="task-1",
        ),
        status=EngineArtifactStatus(state=state, reason="reason", exit_code=0),
        input=EngineArtifactInput(
            primary_path="/tmp/job/input.xyz",
            selected_xyz_path="/tmp/job/input.xyz",
        ),
        resources=EngineArtifactResources(
            request={"cores": 2},
            actual={"cores": 1},
        ),
        timestamps=EngineArtifactTimestamps(
            created_at="2026-01-01T00:00:00Z",
            started_at="2026-01-01T00:00:01Z",
            updated_at="2026-01-01T00:00:02Z",
            finished_at="2026-01-01T00:00:03Z",
        ),
        recovery=EngineArtifactRecovery(
            pending=False,
            reason="",
            count=0,
            resumed=False,
        ),
        process=EngineArtifactProcess(worker_pid=1234),
        engine_payload={"engine_specific": engine},
    )

    assert set(payload) == {
        "schema_version",
        "engine",
        "job",
        "status",
        "input",
        "resources",
        "timestamps",
        "recovery",
        "process",
        "artifacts",
        "engine_payload",
    }
    assert set(payload["job"]) == {
        "id",
        "queue_id",
        "dir",
        "app_name",
        "task_id",
        "generation",
    }
    assert set(payload["status"]) == {"state", "reason", "exit_code"}
    assert set(payload["input"]) == {"primary_path", "selected_xyz_path"}
    assert set(payload["resources"]) == {"request", "actual"}
    assert set(payload["timestamps"]) == {
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
    }
    assert set(payload["recovery"]) == {"pending", "reason", "count", "resumed"}
    assert set(payload["process"]) == {"worker_pid"}
    assert set(payload["artifacts"]) >= {
        "manifest_path",
        "stdout_log",
        "stderr_log",
    }
    assert payload["engine_payload"]["engine_specific"] == engine


@pytest.mark.parametrize(
    ("engine", "engine_payload"),
    [
        (
            "orca",
            {
                "attempts": [{"inp_path": "/tmp/job/calc.inp", "return_code": 0}],
                "final_result": {"status": "completed"},
            },
        ),
        (
            "xtb",
            {
                "candidate_count": 2,
                "selected_candidate_paths": ["/tmp/job/best.xyz"],
                "analysis_summary": {"best_total_energy": -1.0},
            },
        ),
        (
            "crest",
            {
                "retained_conformer_count": 2,
                "retained_conformer_paths": ["/tmp/job/conf-a.xyz", "/tmp/job/conf-b.xyz"],
            },
        ),
    ],
)
def test_engine_artifact_payload_preserves_engine_payload(
    engine: str,
    engine_payload: dict[str, object],
) -> None:
    payload = build_engine_artifact_payload(
        engine=engine,
        job=EngineArtifactJob(id="job-1", queue_id="queue-1", dir="/tmp/job"),
        status=EngineArtifactStatus(state="completed"),
        engine_payload=engine_payload,
    )

    assert payload["engine_payload"] == engine_payload


def test_engine_artifact_loader_rejects_invalid_or_unknown_payloads(tmp_path: Path) -> None:
    path = tmp_path / "job_state.json"

    path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    assert load_engine_artifact_payload(path) is None

    path.write_text(
        json.dumps({"schema_version": 0, "engine": "orca"}),
        encoding="utf-8",
    )
    assert load_engine_artifact_payload(path) is None

    path.write_text(
        json.dumps({"schema_version": 1, "engine": "orca"}),
        encoding="utf-8",
    )
    assert load_engine_artifact_payload(path) == {"schema_version": 1, "engine": "orca"}
