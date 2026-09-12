from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow._orca_stage_materialization import render_orca_input
from orca_auto.flow.contracts import (
    WorkflowStageInput,
)
from orca_auto.flow.orchestration import (
    crest_orca_materialization,
)
from orca_auto.flow.orchestration.crest_orca_materialization import append_crest_orca_stages_impl
from tests.flow.orchestration_services import orchestration_services


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
        metadata=dict(metadata or {}),
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


@pytest.mark.parametrize(
    ("charge", "multiplicity"),
    [(1.9, 2), (1, 2.9), (True, 1), (0, True)],
)
def test_orca_input_renderer_rejects_lossy_electronic_state(
    charge: object,
    multiplicity: object,
) -> None:
    with pytest.raises(ValueError, match=r"(?:charge|multiplicity) must be an integer"):
        render_orca_input(
            route_line="! r2scan-3c",
            charge=charge,  # type: ignore[arg-type]
            multiplicity=multiplicity,  # type: ignore[arg-type]
            max_cores=1,
            max_memory_gb=1,
            xyz_filename="input.xyz",
        )


def test_append_crest_orca_stages_materializes_orca_stages_from_completed_crest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "request": {
                "parameters": {
                    "max_orca_stages": 1,
                    "orca_route_line": "! r2scan-3c Opt TightSCF",
                }
            },
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

    monkeypatch.setattr(
        crest_orca_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: "crest_contract",
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / "orca_allowed"
            },
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
        services=deps,
    )

    assert created is True
    assert build_calls[0]["workspace_dir"] == (tmp_path / "wf_conf_01" / "03_orca").resolve()
    assert payload["stages"][-1]["stage_id"] == "orca_conformer_01"
    assert payload["stages"][-1]["task"]["engine"] == "orca"


def test_append_crest_orca_stages_fails_workflow_when_all_conformers_fail(
    tmp_path: Path,
) -> None:
    # conformer_screening: the CREST stage completed and ORCA conformer opt stages
    # were materialized, but every one failed to optimize. No optimized conformer
    # was produced, so the workflow must be recorded as FAILED (a failed ORCA
    # conformer stage is engine-role non-fatal, so recompute would otherwise report
    # COMPLETED).
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_all_failed",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 2}},
            "workspace_dir": str((tmp_path / "wf_conf_all_failed").resolve()),
        },
        "stages": [
            {"stage_id": "crest_stage_01", "status": "completed", "task": {"engine": "crest"}},
            {
                "stage_id": "orca_conformer_01",
                "status": "failed",
                "metadata": {"analyzer_status": "geom_not_converged"},
                "task": {"engine": "orca"},
            },
            {
                "stage_id": "orca_conformer_02",
                "status": "failed",
                "metadata": {"analyzer_status": "scf_not_converged"},
                "task": {"engine": "orca"},
            },
        ],
    }

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer_guess.xyz",
        inp_filename="conformer_opt.inp",
        services=orchestration_services(),
    )

    assert created is False
    assert payload["metadata"]["workflow_error"] == {
        "status": "failed",
        "scope": "conformer_screening_orca_conformers_exhausted",
        "stage_id": "orca_conformer_01",
        "reason": "conformers_failed",
        "message": "All conformer optimization stages failed; no optimized conformer was produced.",
    }


def test_append_crest_orca_stages_fails_when_completed_crest_has_no_geometry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "workflow_id": "wf_conf_empty_crest",
        "metadata": {
            "request": {"parameters": {"max_orca_stages": 2}},
            "workspace_dir": str(tmp_path),
        },
        "stages": [
            {"stage_id": "crest_stage_01", "status": "completed", "task": {"engine": "crest"}}
        ],
    }
    monkeypatch.setattr(
        crest_orca_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: "crest_contract",
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {"allowed_root": tmp_path},
            "select_crest_downstream_inputs": lambda contract, policy: (),
        }
    )

    created = append_crest_orca_stages_impl(
        payload,
        template_name="conformer_screening",
        crest_config="/tmp/crest.yaml",
        orca_config="/tmp/orca.yaml",
        stage_id_prefix="orca_conformer",
        xyz_filename="conformer.xyz",
        inp_filename="conformer.inp",
        services=deps,
    )

    assert created is False
    assert payload["metadata"]["workflow_error"]["scope"] == ("conformer_screening_crest_handoff")


def test_append_crest_orca_stages_materializes_twenty_orca_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        "metadata": {
            "request": {
                "parameters": {
                    "max_orca_stages": 20,
                    "orca_route_line": "! r2scan-3c Opt TightSCF",
                }
            }
        },
        "stages": [
            {
                "stage_id": "crest_stage_01",
                "status": "completed",
                "task": {"engine": "crest"},
            }
        ],
    }

    monkeypatch.setattr(
        crest_orca_materialization,
        "completed_crest_stage_impl",
        lambda stage, **kwargs: "crest_contract",
    )
    deps = orchestration_services(
        overrides={
            "engine_runtime_paths": lambda path, **kwargs: {
                "allowed_root": tmp_path / "orca_allowed"
            },
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
        services=deps,
    )

    orca_stages = [
        stage for stage in payload["stages"] if stage.get("task", {}).get("engine") == "orca"
    ]
    assert created is True
    assert len(orca_stages) == 20
    assert orca_stages[0]["stage_id"] == "orca_conformer_01"
    assert orca_stages[-1]["stage_id"] == "orca_conformer_20"
