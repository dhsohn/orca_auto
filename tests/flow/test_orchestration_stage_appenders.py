from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow.contracts import WorkflowStageInput
from orca_auto.flow.orchestration.deps import orchestration_deps
from orca_auto.flow.orchestration.materialization import (
    append_crest_orca_stages_impl,
    append_reaction_orca_stages_impl,
    append_reaction_xtb_stages_impl,
)


def _candidate(
    path: str,
    *,
    source_job_id: str,
    source_job_type: str,
    reaction_key: str,
    rank: int,
    kind: str,
    selected_input_xyz: str | None = None,
    selected: bool = True,
    score: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> WorkflowStageInput:
    return WorkflowStageInput(
        source_job_id=source_job_id,
        source_job_type=source_job_type,
        reaction_key=reaction_key,
        selected_input_xyz=selected_input_xyz or path,
        rank=rank,
        kind=kind,
        artifact_path=path,
        selected=selected,
        score=score,
        metadata=metadata or {},
    )


def _orca_stage_result(**kwargs: Any) -> SimpleNamespace:
    candidate = kwargs["candidate"]
    stage = {
        "stage_id": kwargs["stage_id"],
        "status": "planned",
        "metadata": {},
        "input_artifacts": [
            {
                "kind": kwargs["input_artifact_kind"],
                "path": candidate.artifact_path,
                "selected": candidate.selected,
            }
        ],
        "task": {
            "engine": "orca",
            "task_kind": kwargs["task_kind"],
            "status": "planned",
            "payload": {"reaction_dir": ""},
            "metadata": {"source_candidate_path": candidate.artifact_path},
        },
    }
    return SimpleNamespace(to_dict=lambda: stage)


def _write_xyz(path: Path, coords: list[tuple[str, float, float, float]]) -> str:
    path.write_text(
        "\n".join(
            [
                str(len(coords)),
                path.stem,
                *(f"{element} {x:.6f} {y:.6f} {z:.6f}" for element, x, y, z in coords),
                "",
            ]
        ),
        encoding="utf-8",
    )
    return str(path)


def test_append_reaction_xtb_stages_creates_full_cartesian_product(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_01",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 3,
                    "max_xtb_handoff_retries": 4,
                }
            }
        },
    }
    reactant_inputs = [
        _candidate(
            "/tmp/reactant_a.xyz",
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            "/tmp/reactant_b.xyz",
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_b",
            rank=2,
            kind="conformer",
        ),
    ]
    product_inputs = [
        _candidate(
            "/tmp/product_a.xyz",
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            "/tmp/product_b.xyz",
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_b",
            rank=2,
            kind="conformer",
        ),
    ]

    deps = orchestration_deps(
        overrides={
            "_completed_crest_stage": lambda stage, **kwargs: (
                "reactant_contract"
                if stage["metadata"]["input_role"] == "reactant"
                else "product_contract"
            ),
            "select_crest_downstream_inputs": lambda contract, policy: (
                reactant_inputs if contract == "reactant_contract" else product_inputs
            ),
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        deps=deps,
    )

    xtb_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "xtb"
    ]
    assert created is True
    assert [stage["stage_id"] for stage in xtb_stages] == [
        "xtb_path_search_01",
        "xtb_path_search_02",
        "xtb_path_search_03",
        "xtb_path_search_04",
    ]
    assert all(stage["task"]["payload"]["max_handoff_retries"] == 4 for stage in xtb_stages)


def test_append_reaction_xtb_stages_filters_endpoint_pairs(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_pairing",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 1,
                    "endpoint_pairing": {
                        "enabled": True,
                        "comparison_atoms": [1, 2, 3],
                        "max_distance_rmsd": 0.05,
                    },
                }
            }
        },
    }
    r_a = _write_xyz(
        tmp_path / "reactant_a.xyz",
        [("H", 0, 0, 0), ("H", 1, 0, 0), ("H", 0, 1, 0)],
    )
    r_b = _write_xyz(
        tmp_path / "reactant_b.xyz",
        [("H", 0, 0, 0), ("H", 2, 0, 0), ("H", 0, 2, 0)],
    )
    p_a = _write_xyz(
        tmp_path / "product_a.xyz",
        [("H", 5, 5, 0), ("H", 6, 5, 0), ("H", 5, 6, 0)],
    )
    p_b = _write_xyz(
        tmp_path / "product_b.xyz",
        [("H", 0, 0, 0), ("H", 2, 0, 0), ("H", 0, 2, 0)],
    )
    reactant_inputs = [
        _candidate(
            r_a,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            r_b,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_b",
            rank=2,
            kind="conformer",
        ),
    ]
    product_inputs = [
        _candidate(
            p_a,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            p_b,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_b",
            rank=2,
            kind="conformer",
        ),
    ]

    deps = orchestration_deps(
        overrides={
            "_completed_crest_stage": lambda stage, **kwargs: (
                "reactant_contract"
                if stage["metadata"]["input_role"] == "reactant"
                else "product_contract"
            ),
            "select_crest_downstream_inputs": lambda contract, policy: (
                reactant_inputs if contract == "reactant_contract" else product_inputs
            ),
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        deps=deps,
    )

    xtb_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "xtb"
    ]
    assert created is True
    assert len(xtb_stages) == 1
    assert xtb_stages[0]["task"]["payload"]["reactant_source"]["artifact_path"] == r_a
    assert xtb_stages[0]["task"]["payload"]["product_source"]["artifact_path"] == p_a
    pairing = xtb_stages[0]["metadata"]["endpoint_pairing"]
    assert pairing["strategy"] == "distance_fingerprint"
    assert pairing["distance_fingerprint_rmsd"] == 0.0
    assert payload["metadata"]["endpoint_pairing"]["candidate_pair_count"] == 4
    assert payload["metadata"]["endpoint_pairing"]["selected_pair_count"] == 1


def test_append_reaction_xtb_stages_can_exclude_moving_atoms(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_pairing_exclude",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest"},
            },
            {
                "stage_id": "crest_product",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest"},
            },
        ],
        "metadata": {
            "request": {
                "parameters": {
                    "max_crest_candidates": 2,
                    "max_xtb_stages": 1,
                    "endpoint_pairing": {
                        "enabled": True,
                        "moving_atoms": [4],
                        "max_distance_rmsd": 0.05,
                    },
                }
            }
        },
    }
    r_a = _write_xyz(
        tmp_path / "reactant_a.xyz",
        [("C", 0, 0, 0), ("C", 1, 0, 0), ("C", 0, 1, 0), ("H", 0, 0, 2)],
    )
    p_a = _write_xyz(
        tmp_path / "product_a.xyz",
        [("C", 5, 5, 0), ("C", 6, 5, 0), ("C", 5, 6, 0), ("H", 12, 12, 12)],
    )
    r_b = _write_xyz(
        tmp_path / "reactant_b.xyz",
        [("C", 0, 0, 0), ("C", 2, 0, 0), ("C", 0, 2, 0), ("H", 0, 0, 2)],
    )
    p_b = _write_xyz(
        tmp_path / "product_b.xyz",
        [("C", 0, 0, 0), ("C", 3, 0, 0), ("C", 0, 3, 0), ("H", 0, 0, 2)],
    )
    reactant_inputs = [
        _candidate(
            r_a,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            r_b,
            source_job_id="crest_r",
            source_job_type="crest",
            reaction_key="rxn_r_b",
            rank=2,
            kind="conformer",
        ),
    ]
    product_inputs = [
        _candidate(
            p_a,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_a",
            rank=1,
            kind="conformer",
        ),
        _candidate(
            p_b,
            source_job_id="crest_p",
            source_job_type="crest",
            reaction_key="rxn_p_b",
            rank=2,
            kind="conformer",
        ),
    ]

    deps = orchestration_deps(
        overrides={
            "_completed_crest_stage": lambda stage, **kwargs: (
                "reactant_contract"
                if stage["metadata"]["input_role"] == "reactant"
                else "product_contract"
            ),
            "select_crest_downstream_inputs": lambda contract, policy: (
                reactant_inputs if contract == "reactant_contract" else product_inputs
            ),
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        deps=deps,
    )

    xtb_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "xtb"
    ]
    assert created is True
    assert len(xtb_stages) == 1
    assert xtb_stages[0]["task"]["payload"]["reactant_source"]["artifact_path"] == r_a
    assert xtb_stages[0]["task"]["payload"]["product_source"]["artifact_path"] == p_a
    pairing = xtb_stages[0]["metadata"]["endpoint_pairing"]
    assert pairing["comparison_atoms"] == [1, 2, 3]
    assert pairing["excluded_atoms"] == [4]
    assert pairing["distance_fingerprint_rmsd"] == 0.0


def test_append_reaction_xtb_stages_waits_for_latest_product_crest_stage(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_wait",
        "stages": [
            {
                "stage_id": "crest_reactant",
                "status": "completed",
                "metadata": {"input_role": "reactant"},
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "stage_id": "crest_product_old",
                "status": "completed",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest", "status": "completed"},
            },
            {
                "stage_id": "crest_product_new",
                "status": "running",
                "metadata": {"input_role": "product"},
                "task": {"engine": "crest", "status": "running"},
            },
        ],
        "metadata": {"request": {"parameters": {"max_crest_candidates": 2}}},
    }

    deps = orchestration_deps(
        overrides={
            "_completed_crest_stage": lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("xTB stage creation should wait for the newest product CREST stage")
            )
        }
    )

    created = append_reaction_xtb_stages_impl(
        payload,
        workspace_dir=tmp_path,
        crest_config="/tmp/crest.yaml",
        deps=deps,
    )

    assert created is False
    assert all(stage.get("task", {}).get("engine") != "xtb" for stage in payload["stages"])


def test_append_reaction_orca_stages_sets_xtb_handoff_workflow_error_when_no_candidate_survives(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_02",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_01"},
                },
            }
        ],
    }
    contract = SimpleNamespace(job_id="xtb_job_01", job_type="path_search", candidate_details=())

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: (
                tmp_path / ("xtb" if "xtb" in str(path) else "orca")
            ),
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (),
            "_reaction_ts_guess_error": lambda current_contract: {
                "reason": "xtb_ts_guess_missing",
                "message": "missing ts guess",
            },
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        deps=deps,
    )

    xtb_stage = payload["stages"][0]
    assert created is False
    assert xtb_stage["metadata"]["reaction_handoff_status"] == "failed"
    assert xtb_stage["metadata"]["reaction_handoff_reason"] == "xtb_ts_guess_missing"
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "reaction_ts_search_xtb_handoff",
        "stage_id": "xtb_path_search_01",
        "job_id": "xtb_job_01",
        "reason": "xtb_ts_guess_missing",
        "message": "missing ts guess",
    }


def test_append_reaction_orca_stages_waits_when_xtb_contract_is_missing(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_missing_xtb_contract",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_missing"},
                },
            }
        ],
    }

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: (
                tmp_path / ("xtb" if "xtb" in str(path) else "orca")
            ),
            "load_xtb_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                FileNotFoundError("xTB artifact files not found")
            ),
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        deps=deps,
    )

    assert created is False
    assert "workflow_error" not in payload["metadata"]
    assert "reaction_handoff_status" not in payload["stages"][0]["metadata"]


def test_append_reaction_orca_stages_propagates_corrupt_xtb_contract(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_corrupt_xtb_contract",
        "metadata": {"request": {"parameters": {"max_orca_stages": 2}}},
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_corrupt"},
                },
            }
        ],
    }

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: (
                tmp_path / ("xtb" if "xtb" in str(path) else "orca")
            ),
            "load_xtb_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                ValueError("corrupt xTB artifact payload")
            ),
        }
    )

    with pytest.raises(ValueError, match="corrupt xTB artifact payload"):
        append_reaction_orca_stages_impl(
            payload,
            workspace_dir=tmp_path,
            xtb_config="/tmp/xtb.yaml",
            orca_config="/tmp/orca.yaml",
            deps=deps,
        )


def test_append_reaction_orca_stages_waits_for_all_xtb_children_to_finish(
    tmp_path: Path,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_wait_orca",
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "status": "completed",
                    "payload": {"job_dir": "/tmp/xtb_done"},
                },
            },
            {
                "stage_id": "xtb_path_search_02",
                "status": "queued",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "status": "submitted",
                    "payload": {"job_dir": "/tmp/xtb_queued"},
                },
            },
        ],
    }

    deps = orchestration_deps(
        overrides={
            "load_xtb_artifact_contract": lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("ORCA batching should wait for terminal xTB phases")
            )
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        deps=deps,
    )

    assert created is False
    assert [stage["stage_id"] for stage in payload["stages"]] == [
        "xtb_path_search_01",
        "xtb_path_search_02",
    ]


def test_append_reaction_orca_stages_appends_unattempted_candidate_without_mutating_failed_stage(
    tmp_path: Path,
) -> None:
    first_candidate = _candidate(
        "/tmp/candidate_01.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=1,
        kind="ts_guess",
        score=-10.0,
    )
    second_candidate = _candidate(
        "/tmp/candidate_02.xyz",
        source_job_id="xtb_job_02",
        source_job_type="path_search",
        reaction_key="rxn_02",
        rank=2,
        kind="ts_guess",
        score=-9.0,
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_03",
        "metadata": {
            "workflow_error": {"scope": "reaction_ts_search_orca_candidate_exhausted"},
            "request": {
                "parameters": {
                    "max_orca_stages": 3,
                    "orca_route_line": "! custom route",
                }
            },
        },
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_02"},
                },
            },
            {
                "stage_id": "orca_optts_freq_01",
                "status": "failed",
                "metadata": {"analyzer_status": "ts_not_found"},
                "task": {
                    "engine": "orca",
                    "metadata": {"source_candidate_path": first_candidate.artifact_path},
                },
            },
        ],
    }
    contract = SimpleNamespace(job_id="xtb_job_02", job_type="path_search")

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: (
                tmp_path / ("xtb" if "xtb" in str(path) else "orca")
            ),
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (
                first_candidate,
                second_candidate,
            ),
            "build_materialized_orca_stage": _orca_stage_result,
            "now_utc_iso": lambda: "2026-04-19T15:00:00+00:00",
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path,
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        deps=deps,
    )

    latest_existing = payload["stages"][1]
    appended = payload["stages"][2]
    assert created is True
    assert latest_existing["metadata"] == {"analyzer_status": "ts_not_found"}
    assert "workflow_error" not in payload["metadata"]
    assert appended["stage_id"] == "orca_optts_freq_02"
    assert appended["metadata"]["reaction_candidate_attempt_index"] == 2
    assert appended["metadata"]["reaction_candidate_pool_size"] == 2
    assert appended["metadata"]["reaction_remaining_candidates_after_this"] == 0


def test_append_reaction_orca_stages_materializes_under_workflow_orca_stage_root(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        "/tmp/candidate_local.xyz",
        source_job_id="xtb_job_local",
        source_job_type="path_search",
        reaction_key="rxn_local",
        rank=1,
        kind="ts_guess",
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_reaction_local",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 1}},
            "workspace_dir": str((tmp_path / "wf_reaction_local").resolve()),
        },
        "stages": [
            {
                "stage_id": "xtb_path_search_01",
                "status": "completed",
                "metadata": {},
                "task": {
                    "engine": "xtb",
                    "payload": {"job_dir": "/tmp/xtb_job_local"},
                },
            }
        ],
    }
    contract = SimpleNamespace(job_id="xtb_job_local", job_type="path_search", candidate_details=())
    build_calls: list[dict[str, Any]] = []

    def fake_build_materialized_orca_stage(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return _orca_stage_result(**kwargs)

    deps = orchestration_deps(
        overrides={
            "_load_config_root": lambda path, **kwargs: tmp_path / "orca_allowed",
            "load_xtb_artifact_contract": lambda **kwargs: contract,
            "select_xtb_downstream_inputs": lambda *args, **kwargs: (candidate,),
            "build_materialized_orca_stage": fake_build_materialized_orca_stage,
        }
    )

    created = append_reaction_orca_stages_impl(
        payload,
        workspace_dir=tmp_path / "wf_reaction_local",
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
        deps=deps,
    )

    assert created is True
    assert build_calls[0]["workspace_dir"] == (tmp_path / "wf_reaction_local" / "03_orca").resolve()
    assert build_calls[0]["stage_root_name"] == ""


def test_append_crest_orca_stages_materializes_orca_stages_from_completed_crest(
    tmp_path: Path,
) -> None:
    crest_candidate = _candidate(
        "/tmp/crest_conformer.xyz",
        source_job_id="crest_job_01",
        source_job_type="conformer_search",
        reaction_key="rxn_crest",
        rank=1,
        kind="conformer",
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_01",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 1}},
            "workspace_dir": str((tmp_path / "wf_conf_01").resolve()),
        },
        "stages": [
            {
                "stage_id": "crest_stage_01",
                "status": "completed",
                "task": {"engine": "crest"},
            }
        ],
    }
    build_calls: list[dict[str, Any]] = []

    def fake_build_materialized_orca_stage(**kwargs: Any) -> Any:
        build_calls.append(kwargs)
        return _orca_stage_result(**kwargs)

    deps = orchestration_deps(
        overrides={
            "_completed_crest_stage": lambda stage, **kwargs: "crest_contract",
            "_load_config_root": lambda path, **kwargs: tmp_path / "orca_allowed",
            "select_crest_downstream_inputs": lambda contract, policy: (crest_candidate,),
            "build_materialized_orca_stage": fake_build_materialized_orca_stage,
        }
    )

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        deps=deps,
    )

    assert created is True
    assert build_calls[0]["workspace_dir"] == (tmp_path / "wf_conf_01" / "02_orca").resolve()
    assert build_calls[0]["stage_root_name"] == ""
    assert payload["stages"][-1]["stage_id"] == "orca_conformer_01"
    assert payload["stages"][-1]["task"]["engine"] == "orca"


def test_append_crest_orca_stages_materializes_twenty_orca_children(
    tmp_path: Path,
) -> None:
    crest_candidates = tuple(
        _candidate(
            f"/tmp/crest_conformer_{index:02d}.xyz",
            source_job_id="crest_job_20",
            source_job_type="conformer_search",
            reaction_key="mol_20",
            rank=index,
            kind="conformer",
        )
        for index in range(1, 21)
    )
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_20",
        "metadata": {"request": {"parameters": {"max_orca_stages": 20}}},
        "stages": [
            {
                "stage_id": "crest_stage_01",
                "status": "completed",
                "task": {"engine": "crest"},
            }
        ],
    }

    deps = orchestration_deps(
        overrides={
            "_completed_crest_stage": lambda stage, **kwargs: "crest_contract",
            "_load_config_root": lambda path, **kwargs: tmp_path / "orca_allowed",
            "select_crest_downstream_inputs": lambda contract, policy: crest_candidates,
            "build_materialized_orca_stage": _orca_stage_result,
        }
    )

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        deps=deps,
    )

    orca_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "orca"
    ]
    assert created is True
    assert len(orca_stages) == 20
    assert orca_stages[0]["stage_id"] == "orca_conformer_01"
    assert orca_stages[-1]["stage_id"] == "orca_conformer_20"
