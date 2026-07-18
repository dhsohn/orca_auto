from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.contracts.xtb import XtbArtifactContract, XtbCandidateArtifact
from orca_auto.flow.orchestration.lifecycle import (
    downstream_terminal_result_impl,
    effective_stage_status_impl,
    latest_child_stage_summary_impl,
    recompute_workflow_status_impl,
    stage_failure_is_recoverable_impl,
    workflow_has_active_children_impl,
    workflow_sync_only_impl,
)
from orca_auto.flow.orchestration.stage_runtime.crest import (
    completed_crest_roles_impl as _completed_crest_roles,
)
from orca_auto.flow.orchestration.stage_runtime.crest import (
    completed_crest_stage_impl as _completed_crest_stage,
)
from orca_auto.flow.orchestration.stage_runtime.orca import (
    completed_orca_stage_impl as _completed_orca_stage,
)
from orca_auto.flow.orchestration.stage_runtime.shared import (
    _load_contract_or_none,
)
from orca_auto.flow.orchestration.stage_runtime.shared import (
    append_unique_artifact_impl as _append_unique_artifact,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_handoff import (
    stage_has_xtb_candidates_impl as _stage_has_xtb_candidates,
)
from orca_auto.flow.orchestration.stage_runtime.xtb_handoff import (
    xtb_handoff_status_impl as _xtb_handoff_status,
)
from orca_auto.flow.orchestration.support import (
    clear_reaction_xtb_handoff_error_if_recovering_impl as _clear_reaction_xtb_handoff_error_if_recovering,
)
from orca_auto.flow.orchestration.support import (
    load_config_root_impl as _load_config_root,
)
from orca_auto.flow.orchestration.support import (
    reaction_orca_allows_next_candidate_impl as _reaction_orca_allows_next_candidate,
)
from orca_auto.flow.orchestration.support import (
    reaction_ts_guess_error_impl as _reaction_ts_guess_error,
)
from orca_auto.flow.orchestration.support import (
    stage_metadata_impl as _stage_metadata,
)
from orca_auto.flow.orchestration.support import (
    submission_target_impl as _submission_target,
)
from tests.flow.orchestration_services import orchestration_services


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _workflow_sync_only(payload: dict[str, Any]) -> bool:
    return workflow_sync_only_impl(payload, normalize_text_fn=_normalize_text)


def _workflow_has_active_children(
    payload: dict[str, Any],
    *,
    active_downstream: bool = False,
) -> bool:
    return workflow_has_active_children_impl(
        payload,
        normalize_text_fn=_normalize_text,
        workflow_has_active_downstream_fn=lambda current_payload: active_downstream,
    )


def test_orchestration_services_reject_unknown_override() -> None:
    with pytest.raises(TypeError, match="unknown orchestration service override"):
        orchestration_services(overrides={"_sync_crest_stage": lambda stage, **kwargs: None})


def test_load_contract_or_none_returns_none_for_missing_contract() -> None:
    def missing_loader(**kwargs: Any) -> object:
        raise FileNotFoundError("contract is not materialized yet")

    assert (
        _load_contract_or_none(
            missing_loader,
            engine="xtb",
            target="xtb_job_missing",
        )
        is None
    )


def test_load_contract_or_none_propagates_corrupt_contract_errors() -> None:
    def corrupt_loader(**kwargs: Any) -> object:
        raise ValueError("corrupt artifact payload")

    with pytest.raises(ValueError, match="corrupt artifact payload"):
        _load_contract_or_none(
            corrupt_loader,
            engine="xtb",
            target="xtb_job_corrupt",
        )


def _stage_failure_is_recoverable(stage: dict[str, Any]) -> bool:
    return stage_failure_is_recoverable_impl(
        stage,
        normalize_text_fn=_normalize_text,
        stage_metadata_fn=_stage_metadata,
    )


def _recompute_workflow_status(payload: dict[str, Any]) -> str:
    return recompute_workflow_status_impl(
        payload,
        normalize_text_fn=_normalize_text,
        effective_stage_status_fn=lambda stage: effective_stage_status_impl(
            stage,
            normalize_text_fn=_normalize_text,
            stage_failure_is_recoverable_fn=_stage_failure_is_recoverable,
        ),
    )


def test_workflow_sync_only_and_active_children_cover_stage_task_and_downstream() -> None:
    assert _workflow_sync_only({"status": "completed"}) is True
    assert _workflow_sync_only({"status": "failed"}) is True
    assert _workflow_sync_only({"status": "submission_failed"}) is True
    assert _workflow_sync_only({"status": "planned"}) is False
    assert (
        _workflow_sync_only(
            {
                "template_name": "conformer_screening",
                "status": "completed",
                "stages": [
                    {
                        "status": "completed",
                        "task": {"engine": "crest", "status": "completed"},
                    }
                ],
            }
        )
        is False
    )

    assert (
        _workflow_has_active_children(
            {"stages": [{"status": "completed", "task": {"status": "completed"}}]},
            active_downstream=True,
        )
        is True
    )
    assert _workflow_has_active_children({"stages": [{"status": "running"}]}) is True
    assert (
        _workflow_has_active_children(
            {"stages": [{"status": "completed", "task": {"status": "submitted"}}]}
        )
        is True
    )
    assert (
        _workflow_has_active_children(
            {"stages": [{"status": "completed", "task": {"status": "completed"}}]}
        )
        is False
    )


def test_latest_child_stage_summary_and_terminal_result_extract_relevant_fields() -> None:
    stage_summaries = [
        {"stage_id": "s1", "status": "planned", "task_status": "planned", "completed_at": ""},
        {
            "stage_id": "s2",
            "stage_kind": "orca_stage",
            "engine": "orca",
            "task_kind": "opt",
            "status": "running",
            "task_status": "completed",
            "analyzer_status": "running",
            "reason": "working",
            "queue_id": "q_1",
            "run_id": "run_1",
            "latest_known_path": "/tmp/rxn",
            "completed_at": "2026-04-19T00:10:00+00:00",
        },
        {
            "stage_id": "s3",
            "status": "queued",
            "task_status": "queued",
            "completed_at": "2026-04-19T00:05:00+00:00",
        },
    ]

    summary = latest_child_stage_summary_impl(stage_summaries, normalize_text_fn=_normalize_text)

    assert summary == {
        "stage_id": "s2",
        "stage_kind": "orca_stage",
        "engine": "orca",
        "task_kind": "opt",
        "status": "running",
        "task_status": "completed",
        "analyzer_status": "running",
        "reason": "working",
        "queue_id": "q_1",
        "run_id": "run_1",
        "latest_known_path": "/tmp/rxn",
        "completed_at": "2026-04-19T00:10:00+00:00",
    }

    terminal = downstream_terminal_result_impl(
        {"metadata": {"workflow_error": {"reason": "boom", "scope": "orca"}}},
        {"status": "failed", "stage_summaries": stage_summaries},
        normalize_text_fn=_normalize_text,
    )
    assert terminal == {
        "status": "failed",
        "completed_at": "2026-04-19T00:05:00+00:00",
        "failure_reason": "boom",
        "failure_scope": "orca",
    }
    assert (
        downstream_terminal_result_impl(
            {},
            {"status": "running", "stage_summaries": []},
            normalize_text_fn=_normalize_text,
        )
        == {}
    )


def test_submission_target_and_config_roots_follow_precedence() -> None:
    stage = {
        "metadata": {"queue_id": "q_meta"},
        "task": {
            "submission_result": {"parsed_stdout": {"job_id": "job_stdout", "queue_id": "q_stdout"}}
        },
    }
    assert _submission_target(stage) == "q_meta"
    assert (
        _submission_target(
            {"task": {"submission_result": {"parsed_stdout": {"job_id": "job_stdout"}}}}
        )
        == "job_stdout"
    )
    assert _submission_target({}) == ""

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": Path("/tmp/allowed"),
            }
        }
    )
    assert _load_config_root("/tmp/config.yaml", services=deps) == Path("/tmp/allowed")

    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: (_ for _ in ()).throw(ValueError("bad"))
        }
    )
    assert _load_config_root("/tmp/config.yaml", services=deps) is None
    assert _load_config_root(None) is None


def _empty_xtb_contract() -> XtbArtifactContract:
    return XtbArtifactContract(
        job_id="",
        job_type="",
        status="",
        reason="",
        job_dir="",
        latest_known_path="",
    )


def test_xtb_handoff_status_and_ts_guess_error_cover_ready_and_failure() -> None:
    ready_input = WorkflowStageInput(
        source_job_id="xtb_job",
        source_job_type="path_search",
        reaction_key="rxn",
        selected_input_xyz="/tmp/ts.xyz",
        rank=1,
        kind="ts_guess",
        artifact_path="/tmp/ts.xyz",
        selected=True,
    )
    contract = _empty_xtb_contract()

    deps = orchestration_services(
        overrides={
            "select_xtb_downstream_inputs": lambda contract, policy, require_geometry: (
                ready_input,
            )
        }
    )
    ready = _xtb_handoff_status(contract, services=deps)
    assert ready == {
        "status": "ready",
        "reason": "",
        "message": "",
        "artifact_path": "/tmp/ts.xyz",
    }

    missing_contract = _empty_xtb_contract()
    deps = orchestration_services(
        overrides={"select_xtb_downstream_inputs": lambda contract, policy, require_geometry: ()}
    )
    assert _xtb_handoff_status(missing_contract, services=deps) == {
        "status": "failed",
        "reason": "xtb_ts_guess_missing",
        "message": "xTB path_search did not produce a ts_guess candidate (xtbpath_ts.xyz); refusing ORCA handoff.",
        "artifact_path": "",
    }

    invalid_contract = XtbArtifactContract(
        job_id="",
        job_type="",
        status="",
        reason="",
        job_dir="",
        latest_known_path="",
        candidate_details=(
            XtbCandidateArtifact(rank=1, kind="ts_guess", path="/tmp/xtbpath_ts.xyz"),
        ),
    )
    deps = orchestration_services(
        overrides={
            "choose_orca_geometry_frame": lambda path, candidate_kind: (
                "",
                {"selection_reason": "ts_guess_requires_single_frame"},
            )
        }
    )
    assert _reaction_ts_guess_error(invalid_contract, services=deps) == {
        "reason": "xtb_ts_guess_not_single_geometry",
        "message": "xTB produced xtbpath_ts.xyz but it is not a single-geometry TS guess; refusing ORCA handoff.",
    }


def test_stage_candidate_and_failure_helpers_cover_recoverable_paths() -> None:
    assert (
        _stage_has_xtb_candidates(
            {"output_artifacts": [{"kind": "xtb_candidate", "path": "/tmp/candidate.xyz"}]}
        )
        is True
    )
    assert (
        _stage_has_xtb_candidates({"output_artifacts": [{"kind": "xtb_candidate", "path": ""}]})
        is False
    )

    xtb_stage = {
        "status": "failed",
        "task": {"engine": "xtb"},
        "metadata": {"reaction_handoff_status": "ready"},
    }
    orca_stage = {
        "status": "cancel_failed",
        "task": {"engine": "orca"},
        "metadata": {"reaction_candidate_status": "superseded"},
    }
    plain_stage = {"status": "failed", "task": {"engine": "crest"}, "metadata": {}}
    assert _stage_failure_is_recoverable(xtb_stage) is True
    assert _stage_failure_is_recoverable(orca_stage) is True
    assert _stage_failure_is_recoverable(plain_stage) is False
    assert (
        effective_stage_status_impl(
            xtb_stage,
            normalize_text_fn=_normalize_text,
            stage_failure_is_recoverable_fn=_stage_failure_is_recoverable,
        )
        == "completed"
    )
    assert (
        effective_stage_status_impl(
            {"status": "running"},
            normalize_text_fn=_normalize_text,
            stage_failure_is_recoverable_fn=_stage_failure_is_recoverable,
        )
        == "running"
    )

    failing_orca: dict[str, Any] = {
        "status": "failed",
        "metadata": {"analyzer_status": "ts_not_found"},
    }
    assert _reaction_orca_allows_next_candidate(failing_orca) is True
    failing_orca["metadata"]["reaction_candidate_status"] = "superseded"
    assert _reaction_orca_allows_next_candidate(failing_orca) is False


def test_clear_reaction_xtb_handoff_error_and_unique_artifact_helpers() -> None:
    payload = {
        "metadata": {
            "workflow_error": {
                "status": "failed",
                "scope": "reaction_ts_search_xtb_handoff",
            }
        },
        "stages": [
            {
                "status": "planned",
                "task": {"engine": "xtb"},
                "metadata": {"reaction_handoff_status": "retrying"},
            }
        ],
    }

    _clear_reaction_xtb_handoff_error_if_recovering(payload)
    assert "workflow_error" not in payload["metadata"]

    rows = [{"kind": "artifact", "path": "/tmp/a.xyz"}]
    _append_unique_artifact(rows, kind="artifact", path="/tmp/a.xyz")
    _append_unique_artifact(
        rows, kind="artifact", path="/tmp/b.xyz", selected=True, metadata={"rank": 2}
    )
    assert rows == [
        {"kind": "artifact", "path": "/tmp/a.xyz"},
        {"kind": "artifact", "path": "/tmp/b.xyz", "selected": True, "metadata": {"rank": 2}},
    ]


def test_completed_role_and_contract_helpers_use_expected_targets() -> None:
    payload = {
        "stages": [
            {
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "status": "running",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
            {
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ]
    }
    assert set(_completed_crest_roles(payload).keys()) == {"reactant", "product"}

    crest_calls: list[dict[str, Any]] = []

    def fake_load_crest_artifact_contract(*, crest_index_root: Path, target: str) -> str:
        crest_calls.append({"crest_index_root": crest_index_root, "target": target})
        return "crest_contract"

    crest_stage = {
        "task": {"payload": {"job_dir": "/tmp/crest_job"}},
        "metadata": {"queue_id": "q_ignore"},
    }
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": Path("/tmp/crest_allowed")
            },
            "load_crest_artifact_contract": fake_load_crest_artifact_contract,
        }
    )
    assert (
        _completed_crest_stage(crest_stage, crest_config="/tmp/crest.yaml", services=deps)
        == "crest_contract"
    )
    assert crest_calls == [
        {"crest_index_root": Path("/tmp/crest_allowed"), "target": "/tmp/crest_job"}
    ]

    orca_calls: list[dict[str, Any]] = []

    def fake_load_orca_artifact_contract(**kwargs: Any) -> str:
        orca_calls.append(kwargs)
        return "orca_contract"

    orca_stage = {
        "metadata": {"run_id": "run_1", "queue_id": "q_1"},
        "task": {
            "payload": {"reaction_dir": "/tmp/reaction_dir"},
            "enqueue_payload": {"reaction_dir": "/tmp/enqueue_dir"},
        },
    }
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": Path("/tmp/orca_allowed")
            },
            "load_orca_artifact_contract": fake_load_orca_artifact_contract,
        }
    )
    assert (
        _completed_orca_stage(orca_stage, orca_config="/tmp/orca.yaml", services=deps)
        == "orca_contract"
    )
    assert orca_calls == [
        {
            "target": "run_1",
            "orca_allowed_root": Path("/tmp/orca_allowed"),
            "queue_id": "q_1",
            "run_id": "run_1",
            "reaction_dir": "/tmp/reaction_dir",
        }
    ]


def test_completed_crest_roles_ignore_stale_completed_stage_when_newer_stage_is_active() -> None:
    payload = {
        "stages": [
            {
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "status": "running",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest", "status": "running"},
            },
        ]
    }

    assert set(_completed_crest_roles(payload).keys()) == {"reactant"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"metadata": {"workflow_error": {"status": "failed"}}, "stages": []}, "failed"),
        ({"status": "failed", "stages": [{"status": "completed"}]}, "failed"),
        (
            {"status": "submission_failed", "stages": [{"status": "planned"}]},
            "submission_failed",
        ),
        ({"stages": [{"status": "submission_failed"}]}, "failed"),
        ({"status": "cancel_requested", "stages": [{"status": "running"}]}, "cancel_requested"),
        ({"status": "cancel_requested", "stages": [{"status": "completed"}]}, "cancelled"),
        ({"stages": [{"status": "queued"}]}, "running"),
        ({"stages": [{"status": "completed"}, {"status": "completed"}]}, "completed"),
        ({"stages": [{"status": "completed"}, {"status": "planned"}]}, "running"),
        # A stage-level cancel is not success: all-terminal with any
        # cancelled stage resolves to cancelled, not completed.
        ({"stages": [{"status": "cancelled"}]}, "cancelled"),
        ({"stages": [{"status": "cancelled"}, {"status": "completed"}]}, "cancelled"),
        # ... including alongside failed ORCA candidates (whose failure alone
        # would not fail the workflow).
        (
            {
                "stages": [
                    {"status": "cancelled"},
                    {"status": "failed", "task": {"engine": "orca"}},
                ]
            },
            "cancelled",
        ),
        ({"stages": []}, "planned"),
    ],
)
def test_recompute_workflow_status_covers_major_branches(
    payload: dict[str, Any], expected: str
) -> None:
    assert _recompute_workflow_status(payload) == expected


def test_recompute_workflow_status_treats_child_failures_by_engine_role() -> None:
    assert (
        _recompute_workflow_status(
            {
                "template_name": "reaction_ts_search",
                "stages": [
                    {"status": "failed", "task": {"engine": "crest"}},
                    {"status": "running", "task": {"engine": "xtb"}},
                ],
            }
        )
        == "failed"
    )

    assert (
        _recompute_workflow_status(
            {
                "template_name": "reaction_ts_search",
                "stages": [
                    {"status": "failed", "task": {"engine": "xtb"}},
                    {"status": "planned", "task": {"engine": "orca"}},
                ],
            }
        )
        == "running"
    )

    assert (
        _recompute_workflow_status(
            {
                "template_name": "reaction_ts_search",
                "stages": [
                    {"status": "failed", "task": {"engine": "xtb"}},
                    {"status": "failed", "task": {"engine": "orca"}},
                ],
            }
        )
        == "completed"
    )

    assert (
        _recompute_workflow_status(
            {
                "template_name": "conformer_screening",
                "stages": [
                    {"status": "cancel_requested", "task": {"engine": "orca"}},
                    {"status": "completed", "task": {"engine": "orca"}},
                ],
            }
        )
        == "running"
    )

    # Cancelling part of the plan means the workflow did not run to
    # completion: the honest terminal status is cancelled, never a silent
    # "completed" that reads as success.
    assert (
        _recompute_workflow_status(
            {
                "template_name": "conformer_screening",
                "stages": [
                    {"status": "cancelled", "task": {"engine": "orca"}},
                    {"status": "completed", "task": {"engine": "orca"}},
                ],
            }
        )
        == "cancelled"
    )

    assert (
        _recompute_workflow_status(
            {
                "template_name": "conformer_screening",
                "stages": [
                    {"status": "completed", "task": {"engine": "orca"}},
                    {"status": "running", "task": {"engine": "orca"}},
                ],
            }
        )
        == "running"
    )

    assert (
        _recompute_workflow_status(
            {
                "template_name": "conformer_screening",
                "stages": [
                    {"status": "failed", "task": {"engine": "orca"}},
                    {"status": "completed", "task": {"engine": "orca"}},
                ],
            }
        )
        == "completed"
    )
