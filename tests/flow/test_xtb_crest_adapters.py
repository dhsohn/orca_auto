from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.flow.adapters.crest import (
    load_crest_artifact_contract,
    select_crest_downstream_inputs,
)
from orca_auto.flow.adapters.xtb import load_xtb_artifact_contract, select_xtb_downstream_inputs
from orca_auto.flow.contracts.crest import CrestDownstreamPolicy
from orca_auto.flow.contracts.xtb import XtbArtifactContract, XtbDownstreamPolicy
from tests.engine_artifact_helpers import artifact_payload


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _write_xyz(path: Path, *, comment: str = "comment") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "2",
                comment,
                "H 0.0 0.0 0.0",
                "H 0.0 0.0 0.74",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_xyz_ensemble(path: Path, comments: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for comment in comments:
        lines.extend(
            [
                "2",
                comment,
                "H 0.0 0.0 0.0",
                "H 0.0 0.0 0.74",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_xtb_report(
    job_dir: Path,
    *,
    job_id: str,
    status: str = "completed",
    reason: str = "",
    selected_input_xyz: Path | str = "",
    resource_request: dict[str, object] | None = None,
    engine_payload: dict[str, object] | None = None,
) -> None:
    _write_json(
        job_dir / "job_report.json",
        artifact_payload(
            engine="xtb",
            job_id=job_id,
            job_dir=str(job_dir),
            status=status,
            reason=reason,
            primary_path=str(selected_input_xyz),
            selected_xyz_path=str(selected_input_xyz),
            resource_request=resource_request,
            engine_payload=engine_payload,
        ),
    )


def _write_crest_report(
    job_dir: Path,
    *,
    job_id: str,
    status: str = "completed",
    reason: str = "",
    selected_input_xyz: Path | str = "",
    resource_request: dict[str, object] | None = None,
    engine_payload: dict[str, object] | None = None,
) -> None:
    _write_json(
        job_dir / "job_report.json",
        artifact_payload(
            engine="crest",
            job_id=job_id,
            job_dir=str(job_dir),
            status=status,
            reason=reason,
            primary_path=str(selected_input_xyz),
            selected_xyz_path=str(selected_input_xyz),
            resource_request=resource_request,
            engine_payload=engine_payload,
        ),
    )


def _write_crest_state(
    job_dir: Path,
    *,
    job_id: str,
    status: str,
    selected_input_xyz: Path | str = "",
    engine_payload: dict[str, object] | None = None,
) -> None:
    _write_json(
        job_dir / "job_state.json",
        artifact_payload(
            engine="crest",
            job_id=job_id,
            job_dir=str(job_dir),
            status=status,
            primary_path=str(selected_input_xyz),
            selected_xyz_path=str(selected_input_xyz),
            engine_payload=engine_payload,
        ),
    )


def test_load_xtb_artifact_contract_parses_candidate_details_from_direct_path_target(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "xtb_direct"
    selected_input_xyz = job_dir / "input.xyz"
    ts_guess = job_dir / "ts_guess.xyz"
    optimized = job_dir / "optimized.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(ts_guess, comment="energy: -0.5")
    _write_xyz(optimized, comment="energy: -1.2")
    _write_xtb_report(
        job_dir,
        job_id="xtb_direct_1",
        reason="ok",
        selected_input_xyz=selected_input_xyz,
        resource_request={"max_cores": "4"},
        engine_payload={
            "job_type": "path",
            "reaction_key": "rxn-1",
            "analysis_summary": {"best_score": -0.5},
            "candidate_details": [
                {
                    "rank": 2,
                    "kind": "optimized_geometry",
                    "path": str(optimized),
                    "selected": False,
                    "score": "-1.2",
                },
                {
                    "rank": "1",
                    "kind": "ts_guess",
                    "path": str(ts_guess),
                    "selected": "yes",
                    "score": "-0.5",
                    "source": "scan",
                },
                {"rank": 3, "kind": "candidate", "path": "  ", "selected": True},
                "skip-me",
            ],
        },
    )

    contract = load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))

    assert contract.job_id == "xtb_direct_1"
    assert contract.job_dir == str(job_dir.resolve())
    assert contract.latest_known_path == str(job_dir.resolve())
    assert contract.selected_candidate_paths == (str(ts_guess),)
    assert contract.analysis_summary == {"best_score": -0.5}
    assert contract.resource_request == {"max_cores": 4}
    assert contract.resource_actual == {"max_cores": 4}
    assert len(contract.candidate_details) == 2

    details_by_kind = {detail.kind: detail for detail in contract.candidate_details}
    assert details_by_kind["ts_guess"].selected is True
    assert details_by_kind["ts_guess"].score == pytest.approx(-0.5)
    assert details_by_kind["ts_guess"].metadata == {"source": "scan"}
    assert details_by_kind["optimized_geometry"].selected is False

    stage_inputs = select_xtb_downstream_inputs(contract, require_geometry=True)

    assert len(stage_inputs) == 1
    assert stage_inputs[0].artifact_path == str(ts_guess)
    assert stage_inputs[0].kind == "ts_guess"
    assert stage_inputs[0].selected is True
    assert stage_inputs[0].metadata == {"source": "scan"}


def test_load_xtb_artifact_contract_preserves_selected_candidate_paths_without_details(
    tmp_path: Path,
) -> None:
    index_root = tmp_path / "xtb_index"
    job_dir = tmp_path / "xtb_job_fallback"
    selected_input_xyz = job_dir / "input.xyz"
    candidate_one = job_dir / "candidate_1.xyz"
    candidate_two = job_dir / "candidate_2.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(candidate_one)
    _write_xyz(candidate_two)
    _write_json(
        index_root / "job_locations.json",
        [
            {
                "job_id": "xtb_job_fallback",
                "app_name": "orca_auto_xtb",
                "job_type": "xtb_ts",
                "status": "completed",
                "original_run_dir": str(job_dir),
                "molecule_key": "rxn-2",
                "selected_input_xyz": str(selected_input_xyz),
                "latest_known_path": str(job_dir),
                "resource_request": {"max_cores": "8"},
            }
        ],
    )
    _write_xtb_report(
        job_dir,
        job_id="xtb_job_fallback",
        engine_payload={
            "job_type": "",
            "selected_candidate_paths": [" ", str(candidate_one), str(candidate_two)],
        },
    )

    contract = load_xtb_artifact_contract(xtb_index_root=index_root, target="xtb_job_fallback")

    assert contract.job_id == "xtb_job_fallback"
    assert contract.job_type == "ts"
    assert contract.status == "completed"
    assert contract.reaction_key == "rxn-2"
    assert contract.selected_input_xyz == str(selected_input_xyz)
    assert contract.selected_candidate_paths == (str(candidate_one), str(candidate_two))
    assert contract.resource_request == {"max_cores": 8}
    assert contract.resource_actual == {"max_cores": 8}
    assert contract.candidate_details == ()


def test_load_xtb_artifact_contract_ignores_malformed_candidate_details(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "xtb_malformed_fallback"
    candidate_one = job_dir / "candidate_1.xyz"
    candidate_two = job_dir / "candidate_2.xyz"

    _write_xyz(candidate_one)
    _write_xyz(candidate_two)
    _write_xtb_report(
        job_dir,
        job_id="xtb_malformed_fallback",
        engine_payload={
            "selected_candidate_paths": [
                {"path": str(candidate_one)},
                str(candidate_one),
                ["nested"],
                str(candidate_two),
            ],
            "candidate_details": [
                {"rank": 1, "kind": "candidate", "path": " "},
                ["not", "a", "dict"],
            ],
        },
    )

    contract = load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))

    assert contract.selected_candidate_paths == (str(candidate_one), str(candidate_two))
    assert contract.candidate_details == ()


def test_select_xtb_downstream_inputs_ignores_selected_paths_when_details_are_empty(
    tmp_path: Path,
) -> None:
    invalid_candidate = tmp_path / "candidate.txt"
    valid_candidate = tmp_path / "candidate.xyz"

    invalid_candidate.write_text("not xyz", encoding="utf-8")
    _write_xyz(valid_candidate)

    contract = XtbArtifactContract(
        job_id="xtb_no_details",
        job_type="scan",
        status="completed",
        reason="",
        job_dir=str(tmp_path),
        latest_known_path=str(tmp_path),
        reaction_key="rxn-3",
        selected_input_xyz=str(valid_candidate),
        selected_candidate_paths=(str(invalid_candidate), str(valid_candidate)),
        candidate_details=(),
    )

    stage_inputs = select_xtb_downstream_inputs(
        contract,
        policy=XtbDownstreamPolicy.build(max_candidates=2),
        require_geometry=True,
    )

    assert stage_inputs == ()


def test_load_xtb_artifact_contract_rejects_invalid_artifact_json(tmp_path: Path) -> None:
    job_dir = tmp_path / "xtb_corrupt_json"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))


def test_load_xtb_artifact_contract_rejects_non_xtb_index_records(tmp_path: Path) -> None:
    index_root = tmp_path / "xtb_index"
    job_dir = tmp_path / "xtb_wrong_app"

    job_dir.mkdir(parents=True)
    _write_json(
        index_root / "job_locations.json",
        [
            {
                "job_id": "xtb_bad_app",
                "app_name": "orca_auto_crest",
                "job_type": "xtb_path",
                "status": "completed",
                "original_run_dir": str(job_dir),
                "latest_known_path": str(job_dir),
            }
        ],
    )
    _write_json(
        job_dir / "job_state.json",
        artifact_payload(engine="xtb", job_id="xtb_bad_app", job_dir=str(job_dir)),
    )

    with pytest.raises(ValueError, match="Expected orca_auto_xtb index record"):
        load_xtb_artifact_contract(xtb_index_root=index_root, target="xtb_bad_app")


def test_load_crest_artifact_contract_and_select_retained_conformers(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_direct"
    selected_input_xyz = job_dir / "input.xyz"
    conformer_one = job_dir / "conf_1.xyz"
    conformer_two = job_dir / "conf_2.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformer_one, comment="energy: -2.0")
    _write_xyz(conformer_two, comment="energy: -1.5")
    _write_crest_report(
        job_dir,
        job_id="crest_direct_1",
        reason="retained",
        selected_input_xyz=selected_input_xyz,
        resource_request={"max_cores": "2"},
        engine_payload={
            "mode": "nci",
            "molecule_key": "mol-1",
            "retained_conformer_paths": [" ", str(conformer_one), str(conformer_two)],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.job_id == "crest_direct_1"
    assert contract.mode == "nci"
    assert contract.job_dir == str(job_dir.resolve())
    assert contract.latest_known_path == str(job_dir.resolve())
    assert contract.retained_conformer_count == 2
    assert contract.retained_conformer_paths == (str(conformer_one), str(conformer_two))
    assert contract.resource_request == {"max_cores": 2}
    assert contract.resource_actual == {"max_cores": 2}

    stage_inputs = select_crest_downstream_inputs(
        contract, policy=CrestDownstreamPolicy.build(max_candidates=2)
    )

    assert len(stage_inputs) == 2
    assert stage_inputs[0].artifact_path == str(conformer_one)
    assert stage_inputs[0].source_job_type == "crest_nci"
    assert stage_inputs[0].kind == "crest_conformer"
    assert stage_inputs[0].selected is True
    assert stage_inputs[0].metadata == {"mode": "nci"}
    assert stage_inputs[1].artifact_path == str(conformer_two)
    assert stage_inputs[1].selected is False


def test_load_crest_artifact_contract_prefers_active_state_over_stale_report(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "crest_active_state"
    old_conformer = job_dir / "old_conf.xyz"
    active_input = job_dir / "active_input.xyz"
    active_conformer = job_dir / "active_conf.xyz"

    _write_xyz(old_conformer)
    _write_xyz(active_input)
    _write_xyz(active_conformer)
    _write_crest_report(
        job_dir,
        job_id="crest_old",
        engine_payload={
            "mode": "standard",
            "molecule_key": "old-mol",
            "retained_conformer_paths": [str(old_conformer)],
        },
    )
    _write_crest_state(
        job_dir,
        job_id="crest_new",
        status="running",
        selected_input_xyz=active_input,
        engine_payload={
            "mode": "nci",
            "molecule_key": "active-mol",
            "retained_conformer_paths": [str(active_conformer)],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.job_id == "crest_new"
    assert contract.status == "running"
    assert contract.mode == "nci"
    assert contract.molecule_key == "active-mol"
    assert contract.selected_input_xyz == str(active_input.resolve())
    assert contract.retained_conformer_paths == (str(active_conformer.resolve()),)


def test_load_crest_artifact_contract_uses_index_target_without_organized_ref(
    tmp_path: Path,
) -> None:
    index_root = tmp_path / "crest_index"
    job_dir = tmp_path / "crest_index_job"
    selected_input_xyz = job_dir / "input.xyz"
    conformer = job_dir / "conf.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformer)
    _write_json(
        index_root / "job_locations.json",
        [
            {
                "job_id": "crest_index_job",
                "app_name": "orca_auto_crest",
                "job_type": "crest_standard",
                "status": "completed",
                "original_run_dir": str(job_dir),
                "molecule_key": "mol-index",
                "selected_input_xyz": str(selected_input_xyz),
                "latest_known_path": str(job_dir),
            }
        ],
    )
    _write_crest_report(
        job_dir,
        job_id="crest_index_job",
        engine_payload={
            "retained_conformer_paths": [str(conformer)],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=index_root, target="crest_index_job")

    assert contract.job_dir == str(job_dir.resolve())
    assert contract.selected_input_xyz == str(selected_input_xyz.resolve())
    assert contract.retained_conformer_paths == (str(conformer.resolve()),)


def test_load_crest_artifact_contract_remaps_retained_paths_to_current_artifact_root(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "crest_remap"
    old_dir = tmp_path / "old_organized"
    selected_input_xyz = job_dir / "input.xyz"
    conformer = job_dir / "crest_best.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformer)
    _write_crest_report(
        job_dir,
        job_id="crest_remap",
        selected_input_xyz=old_dir / "input.xyz",
        engine_payload={
            "retained_conformer_paths": [str(old_dir / "crest_best.xyz")],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.selected_input_xyz == str(selected_input_xyz.resolve())
    assert contract.retained_conformer_paths == (str(conformer.resolve()),)


def test_select_crest_downstream_inputs_splits_multiframe_retained_ensemble(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_multiframe"
    selected_input_xyz = job_dir / "input.xyz"
    retained_ensemble = job_dir / "crest_conformers.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz_ensemble(
        retained_ensemble,
        (
            "energy: -2.0",
            "energy: -1.7",
            "energy: -1.4",
        ),
    )
    _write_crest_report(
        job_dir,
        job_id="crest_multiframe_1",
        reason="retained",
        selected_input_xyz=selected_input_xyz,
        engine_payload={
            "mode": "standard",
            "molecule_key": "mol-frames",
            "retained_conformer_paths": [str(retained_ensemble)],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))
    stage_inputs = select_crest_downstream_inputs(
        contract, policy=CrestDownstreamPolicy.build(max_candidates=2)
    )

    assert len(stage_inputs) == 2
    assert [item.rank for item in stage_inputs] == [1, 2]
    assert all(item.artifact_path == str(retained_ensemble.resolve()) for item in stage_inputs)
    assert stage_inputs[0].selected is True
    assert stage_inputs[0].metadata == {
        "mode": "standard",
        "source_artifact_path": str(retained_ensemble.resolve()),
        "source_frame_index": 1,
        "source_frame_count": 3,
        "source_frame_energy": -2.0,
    }
    assert stage_inputs[1].selected is False
    assert stage_inputs[1].metadata["source_frame_index"] == 2
