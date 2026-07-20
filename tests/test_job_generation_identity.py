from __future__ import annotations

import pytest

from orca_auto.flow.adapters._orca_contract_context import (
    LoaderContext,
    load_context_payloads,
)
from orca_auto.orca.job_locations._generation import (
    current_generation_payloads,
    payload_matches_queue_generation,
)


def _provenance(*, suffix: str = "a1b2c3d4") -> dict[str, object]:
    return {
        "execution_dir": f"/runs/job/20260720-120000-{suffix}",
        "execution_dir_identity": {"device": 1, "inode": 2 if suffix == "a1b2c3d4" else 3},
        "generation_owner_token": f"owner-token-{suffix}",
        "bound_selected_identity": {"path": "/runs/job/generation/job.inp"},
    }


def _payload(job_id: str, run_id: str) -> dict[str, object]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "execution_provenance": _provenance(),
    }


def test_queue_absent_state_and_report_require_the_same_generation_identity() -> None:
    state = _payload("job-a", "run-a")
    report = _payload("job-b", "run-b")

    assert current_generation_payloads(None, state, report) == ({}, {})


def test_queue_absent_keeps_matching_or_single_available_payloads() -> None:
    state = _payload("job-a", "run-a")
    report = _payload("job-a", "run-a")

    assert current_generation_payloads(None, state, report) == (state, report)
    assert current_generation_payloads(None, state, {}) == (state, {})


def test_queue_absent_rejects_mixed_or_conflicting_generation_provenance() -> None:
    state = _payload("job-a", "run-a")
    report = _payload("job-a", "run-a")
    report["execution_provenance"] = _provenance(suffix="b1c2d3e4")

    assert current_generation_payloads(None, state, report) == ({}, {})

    state["engine_payload"] = {"execution_provenance": _provenance(suffix="b1c2d3e4")}
    assert not payload_matches_queue_generation(None, state)


def test_queue_present_without_run_id_rejects_state_report_run_mismatch() -> None:
    state = {"job_id": "job-a", "run_id": "run-a"}
    report = {"job_id": "job-a", "run_id": "run-b"}

    assert current_generation_payloads({"task_id": "job-a"}, state, report) == ({}, {})


@pytest.mark.parametrize(
    ("queue_entry", "payload"),
    [
        (
            {"task_id": "job-a"},
            {
                "job_id": "job-a",
                "run_id": "run-a",
                "engine_payload": {"run_id": "run-b"},
            },
        ),
        (
            {"task_id": "job-a", "metadata": {"run_id": "run-a"}},
            {
                "job_id": "job-a",
                "job": {"id": "job-b"},
                "run_id": "run-a",
            },
        ),
        (
            {"metadata": {"run_id": "run-a"}},
            {"job_id": "job-a", "run_id": "run-a"},
        ),
        (
            {"task_id": "job-a"},
            {"job_id": "job-a"},
        ),
    ],
    ids=("run-conflict", "job-conflict", "run-only-queue", "missing-payload-run"),
)
def test_queue_present_rejects_incomplete_or_internally_conflicting_identity(
    queue_entry: dict[str, object],
    payload: dict[str, object],
) -> None:
    assert not payload_matches_queue_generation(queue_entry, payload)


def test_queue_present_accepts_task_identity_before_terminal_run_update() -> None:
    assert payload_matches_queue_generation(
        {"task_id": "job-a"},
        {"job_id": "job-a", "run_id": "run-a"},
    )


def test_adapter_rejects_same_spoofed_inner_pair_before_flattening() -> None:
    def normalized_payload(*, outer_job_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "engine": "orca",
            "job": {"id": outer_job_id, "task_id": outer_job_id},
            "status": {"state": "completed"},
            "input": {},
            "timestamps": {},
            "artifacts": {},
            "engine_payload": {
                "job_id": "job-expected",
                "run_id": "run-expected",
                "attempts": [{"status": "completed"}],
            },
        }

    context = LoaderContext(
        tracked_artifact_dir=None,
        tracked_dir=None,
        tracked_record=None,
        state=normalized_payload(outer_job_id="job-foreign-state"),
        report=normalized_payload(outer_job_id="job-foreign-report"),
        queue_entry={
            "task_id": "job-expected",
            "metadata": {"run_id": "run-expected"},
        },
    )

    load_context_payloads(context, deps=object())

    assert context.state == {}
    assert context.report == {}
