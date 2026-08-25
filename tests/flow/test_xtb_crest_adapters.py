from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core import engine_runner as _engine_runner
from orca_auto.flow.adapters.crest import (
    load_crest_artifact_contract,
    select_crest_downstream_inputs,
)
from orca_auto.flow.adapters.xtb import load_xtb_artifact_contract, select_xtb_downstream_inputs
from orca_auto.flow.contracts.crest import CrestArtifactContract, CrestDownstreamPolicy
from orca_auto.flow.contracts.xtb import XtbArtifactContract, XtbDownstreamPolicy
from tests.engine_artifact_helpers import artifact_payload
from tests.flow.artifact_file_helpers import _write_xyz_ensemble


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


def _write_engine_index(
    job_dir: Path,
    *,
    engine: str,
    job_id: str,
    status: str,
    selected_input_xyz: Path | str = "",
    job_type: str = "",
    molecule_key: str = "",
) -> None:
    index_path = job_dir.parent / "job_locations.json"
    records: list[dict[str, object]] = []
    if index_path.exists():
        loaded = json.loads(index_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            records = [item for item in loaded if isinstance(item, dict)]
    records = [item for item in records if item.get("job_id") != job_id]
    records.append(
        {
            "job_id": job_id,
            "app_name": f"orca_auto_{engine}",
            "job_type": job_type,
            "status": status,
            "original_run_dir": str(job_dir),
            "molecule_key": molecule_key,
            "selected_input_xyz": str(selected_input_xyz),
            "latest_known_path": str(job_dir),
        }
    )
    _write_json(index_path, records)


def _write_xtb_state(
    job_dir: Path,
    *,
    job_id: str,
    status: str = "completed",
    reason: str = "",
    selected_input_xyz: Path | str = "",
    resource_request: dict[str, object] | None = None,
    engine_payload: dict[str, object] | None = None,
    include_output_identities: bool = True,
) -> None:
    payload_fields = dict(engine_payload or {})
    raw_details = payload_fields.get("candidate_details")
    if include_output_identities and status == "completed" and isinstance(raw_details, list):
        candidate_details: list[object] = []
        for raw_detail in raw_details:
            if not isinstance(raw_detail, dict):
                candidate_details.append(raw_detail)
                continue
            detail = dict(raw_detail)
            path_text = str(detail.get("path") or "").strip()
            if path_text and not isinstance(detail.get("output_identity"), dict):
                candidate_path = Path(path_text).expanduser()
                if not candidate_path.is_absolute():
                    candidate_path = job_dir / candidate_path
                try:
                    detail["output_identity"] = _engine_runner.confined_output_identity(
                        job_dir,
                        candidate_path,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    pass
            candidate_details.append(detail)
        payload_fields["candidate_details"] = candidate_details
    _write_engine_index(
        job_dir,
        engine="xtb",
        job_id=job_id,
        status=status,
        selected_input_xyz=selected_input_xyz,
        job_type=f"xtb_{str(payload_fields.get('job_type') or 'unknown')}",
        molecule_key=str(payload_fields.get("reaction_key") or ""),
    )
    _write_json(
        job_dir / "job_state.json",
        artifact_payload(
            engine="xtb",
            job_id=job_id,
            queue_id=f"queue-{job_id}",
            app_name="orca_auto_xtb",
            generation=f"generation-{job_id}",
            job_dir=str(job_dir),
            status=status,
            reason=reason,
            primary_path=str(selected_input_xyz),
            selected_xyz_path=str(selected_input_xyz),
            resource_request=resource_request,
            engine_payload=payload_fields,
        ),
    )


def _write_crest_state(
    job_dir: Path,
    *,
    job_id: str,
    status: str = "completed",
    reason: str = "",
    selected_input_xyz: Path | str = "",
    resource_request: dict[str, object] | None = None,
    engine_payload: dict[str, object] | None = None,
    include_output_identities: bool = True,
) -> None:
    payload_fields = dict(engine_payload or {})
    raw_paths = payload_fields.get("retained_conformer_paths")
    if include_output_identities and status == "completed" and isinstance(raw_paths, list):
        raw_output_identities = payload_fields.get("output_identities")
        output_identities: dict[str, object] = (
            dict(raw_output_identities) if isinstance(raw_output_identities, dict) else {}
        )
        for raw_path in raw_paths:
            path_text = str(raw_path or "").strip()
            if not path_text or path_text in output_identities:
                continue
            candidate_path = Path(path_text).expanduser()
            if not candidate_path.is_absolute():
                candidate_path = job_dir / candidate_path
            try:
                output_identities[path_text] = _engine_runner.confined_output_identity(
                    job_dir,
                    candidate_path,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        payload_fields["output_identities"] = output_identities
    _write_engine_index(
        job_dir,
        engine="crest",
        job_id=job_id,
        status=status,
        selected_input_xyz=selected_input_xyz,
        job_type=str(payload_fields.get("mode") or "standard"),
        molecule_key=str(payload_fields.get("molecule_key") or ""),
    )
    _write_json(
        job_dir / "job_state.json",
        artifact_payload(
            engine="crest",
            job_id=job_id,
            queue_id=f"queue-{job_id}",
            app_name="orca_auto_crest",
            generation=f"generation-{job_id}",
            job_dir=str(job_dir),
            status=status,
            reason=reason,
            primary_path=str(selected_input_xyz),
            selected_xyz_path=str(selected_input_xyz),
            resource_request=resource_request,
            engine_payload=payload_fields,
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
    _write_xtb_state(
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
    assert details_by_kind["ts_guess"].metadata["source"] == "scan"
    assert details_by_kind["ts_guess"].metadata["output_identity"]["sha256"]
    assert details_by_kind["optimized_geometry"].selected is False

    stage_inputs = select_xtb_downstream_inputs(contract, require_geometry=True)

    assert len(stage_inputs) == 1
    assert stage_inputs[0].artifact_path == str(ts_guess)
    assert stage_inputs[0].kind == "ts_guess"
    assert stage_inputs[0].selected is True
    assert stage_inputs[0].metadata["source"] == "scan"
    assert stage_inputs[0].metadata["output_identity"]["sha256"]


def test_load_xtb_artifact_contract_rejects_completed_selected_paths_without_details(
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
    _write_xtb_state(
        job_dir,
        job_id="xtb_job_fallback",
        engine_payload={
            "job_type": "",
            "selected_candidate_paths": [" ", str(candidate_one), str(candidate_two)],
        },
    )

    with pytest.raises(ValueError, match="missing identity-bearing detail"):
        load_xtb_artifact_contract(xtb_index_root=index_root, target="xtb_job_fallback")


def test_load_xtb_artifact_contract_ignores_stale_report_when_state_exists(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "xtb_active_state"
    old_candidate = job_dir / "old.xyz"
    active_input = job_dir / "active_input.xyz"
    _write_xyz(old_candidate)
    _write_xyz(active_input)
    _write_json(
        job_dir / "job_report.json",
        artifact_payload(
            engine="xtb",
            job_id="old-job",
            job_dir=str(job_dir),
            engine_payload={
                "job_type": "path_search",
                "reaction_key": "old",
                "candidate_details": [{"rank": 1, "kind": "ts_guess", "path": str(old_candidate)}],
            },
        ),
    )
    _write_xtb_state(
        job_dir,
        job_id="new-job",
        status="running",
        selected_input_xyz=active_input,
        engine_payload={"job_type": "opt", "reaction_key": "new"},
    )

    contract = load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))

    assert contract.job_id == "new-job"
    assert contract.status == "running"
    assert contract.reaction_key == "new"
    assert contract.candidate_details == ()


def test_xtb_and_crest_contracts_reject_existing_artifacts_outside_job_dir(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.xyz"
    _write_xyz(outside)
    xtb_job_dir = tmp_path / "xtb_job"
    _write_xtb_state(
        xtb_job_dir,
        job_id="xtb-job",
        engine_payload={
            "candidate_details": [
                {"rank": 1, "kind": "ts_guess", "path": str(outside), "selected": True}
            ]
        },
    )
    with pytest.raises(ValueError, match="escapes job_dir"):
        load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(xtb_job_dir))

    crest_job_dir = tmp_path / "crest_job"
    _write_crest_state(
        crest_job_dir,
        job_id="crest-job",
        engine_payload={"retained_conformer_paths": [str(outside)]},
    )
    with pytest.raises(ValueError, match="escapes job_dir"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(crest_job_dir))


def test_load_xtb_artifact_contract_ignores_malformed_candidate_details(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "xtb_malformed_fallback"
    candidate_one = job_dir / "candidate_1.xyz"
    candidate_two = job_dir / "candidate_2.xyz"

    _write_xyz(candidate_one)
    _write_xyz(candidate_two)
    _write_xtb_state(
        job_dir,
        job_id="xtb_malformed_fallback",
        status="running",
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
    _write_engine_index(
        job_dir,
        engine="xtb",
        job_id="xtb-corrupt-json",
        status="completed",
    )
    (job_dir / "job_state.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))


def test_load_xtb_artifact_contract_does_not_fall_back_to_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "xtb_report_only"
    _write_engine_index(
        job_dir,
        engine="xtb",
        job_id="xtb-report-only",
        status="completed",
    )
    _write_json(
        job_dir / "job_report.json",
        artifact_payload(engine="xtb", job_id="xtb-report-only", job_dir=str(job_dir)),
    )

    with pytest.raises(FileNotFoundError, match="xTB artifact files not found"):
        load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))


def test_internal_adapter_requires_durable_index_record(tmp_path: Path) -> None:
    job_dir = tmp_path / "xtb_unindexed"
    _write_json(
        job_dir / "job_state.json",
        artifact_payload(
            engine="xtb",
            job_id="xtb-unindexed",
            queue_id="queue-xtb-unindexed",
            app_name="orca_auto_xtb",
            generation="generation-xtb-unindexed",
            job_dir=str(job_dir),
        ),
    )

    with pytest.raises(FileNotFoundError, match="index record not found"):
        load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))


@pytest.mark.parametrize("engine", ("xtb", "crest"))
@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("app_name", "index record app_name is missing"),
        ("job_id", "index record job_id is missing"),
        ("status", "index record status is missing"),
    ),
)
def test_internal_adapters_reject_blank_required_index_identity(
    tmp_path: Path,
    engine: str,
    field: str,
    message: str,
) -> None:
    job_dir = tmp_path / f"{engine}_blank_{field}"
    job_id = f"{engine}-blank-{field}"
    if engine == "xtb":
        _write_xtb_state(job_dir, job_id=job_id)
    else:
        _write_crest_state(job_dir, job_id=job_id)

    index_path = tmp_path / "job_locations.json"
    records = json.loads(index_path.read_text(encoding="utf-8"))
    matching = next(item for item in records if item.get("original_run_dir") == str(job_dir))
    matching[field] = ""
    _write_json(index_path, records)

    with pytest.raises(ValueError, match=message):
        if engine == "xtb":
            load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(job_dir))
        else:
            load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload, _job_dir: payload.update({"engine": "crest"}), "state engine"),
        (
            lambda payload, _job_dir: payload["job"].update({"generation": ""}),
            "generation identity is missing",
        ),
        (
            lambda payload, job_dir: payload["job"].update({"dir": str(job_dir / "foreign")}),
            "directory does not match",
        ),
    ),
)
def test_internal_adapter_rejects_invalid_state_envelope(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any], Path], None],
    message: str,
) -> None:
    job_dir = tmp_path / "xtb_invalid_envelope"
    _write_xtb_state(job_dir, job_id="xtb-invalid-envelope")
    state_path = job_dir / "job_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    mutation(payload, job_dir)
    _write_json(state_path, payload)

    with pytest.raises(ValueError, match=message):
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
    _write_crest_state(
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
    assert stage_inputs[0].metadata["mode"] == "nci"
    assert stage_inputs[0].metadata["output_identity"]["sha256"]
    assert stage_inputs[1].artifact_path == str(conformer_two)
    assert stage_inputs[1].selected is False


def test_load_crest_artifact_contract_ignores_stale_report_when_state_exists(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "crest_active_state"
    old_conformer = job_dir / "old_conf.xyz"
    active_input = job_dir / "active_input.xyz"
    active_conformer = job_dir / "active_conf.xyz"

    _write_xyz(old_conformer)
    _write_xyz(active_input)
    _write_xyz(active_conformer)
    _write_json(
        job_dir / "job_report.json",
        artifact_payload(
            engine="crest",
            job_id="crest_old",
            job_dir=str(job_dir),
            engine_payload={
                "mode": "standard",
                "molecule_key": "old-mol",
                "retained_conformer_paths": [str(old_conformer)],
            },
        ),
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


def test_load_crest_artifact_contract_does_not_fall_back_to_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_report_only"
    _write_engine_index(
        job_dir,
        engine="crest",
        job_id="crest-report-only",
        status="completed",
    )
    _write_json(
        job_dir / "job_report.json",
        artifact_payload(engine="crest", job_id="crest-report-only", job_dir=str(job_dir)),
    )

    with pytest.raises(FileNotFoundError, match="CREST artifact files not found"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


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
    _write_crest_state(
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


def test_load_crest_artifact_contract_rejects_relocated_unbound_paths(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "crest_remap"
    old_dir = tmp_path / "old_organized"
    selected_input_xyz = job_dir / "input.xyz"
    conformer = job_dir / "crest_best.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformer)
    _write_crest_state(
        job_dir,
        job_id="crest_remap",
        selected_input_xyz=old_dir / "input.xyz",
        engine_payload={
            "retained_conformer_paths": [str(old_dir / "crest_best.xyz")],
        },
    )

    with pytest.raises(ValueError, match="selected input artifact escapes job_dir"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


def test_completed_xtb_and_crest_artifacts_require_terminal_output_identities(
    tmp_path: Path,
) -> None:
    xtb_job_dir = tmp_path / "xtb_missing_identity"
    xtb_candidate = xtb_job_dir / "ts_guess.xyz"
    _write_xyz(xtb_candidate)
    _write_xtb_state(
        xtb_job_dir,
        job_id="xtb-missing-identity",
        engine_payload={
            "selected_candidate_paths": [str(xtb_candidate)],
            "candidate_details": [
                {
                    "rank": 1,
                    "kind": "ts_guess",
                    "path": str(xtb_candidate),
                    "selected": True,
                }
            ],
        },
        include_output_identities=False,
    )
    with pytest.raises(ValueError, match="missing its output identity"):
        load_xtb_artifact_contract(xtb_index_root=tmp_path, target=str(xtb_job_dir))

    crest_job_dir = tmp_path / "crest_missing_identity"
    crest_conformer = crest_job_dir / "crest_best.xyz"
    _write_xyz(crest_conformer)
    _write_crest_state(
        crest_job_dir,
        job_id="crest-missing-identity",
        engine_payload={"retained_conformer_paths": [str(crest_conformer)]},
        include_output_identities=False,
    )
    with pytest.raises(ValueError, match="missing its output identity"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(crest_job_dir))


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
    _write_crest_state(
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
    expected_metadata = {
        "mode": "standard",
        "source_artifact_path": str(retained_ensemble.resolve()),
        "source_frame_index": 1,
        "source_frame_count": 3,
        "source_frame_energy": -2.0,
    }
    assert {
        key: value for key, value in stage_inputs[0].metadata.items() if key != "output_identity"
    } == expected_metadata
    assert stage_inputs[0].metadata["output_identity"]["sha256"]
    assert stage_inputs[1].selected is False
    assert stage_inputs[1].metadata["source_frame_index"] == 2


def test_select_crest_downstream_inputs_deduplicates_geometry_across_retained_files(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "crest_duplicate"
    first = job_dir / "crest_conformers.xyz"
    duplicate = job_dir / "crest_best.xyz"
    distinct = job_dir / "crest_rotamers.xyz"
    _write_xyz(first, comment="first source")
    _write_xyz(duplicate, comment="duplicate source")
    distinct.parent.mkdir(parents=True, exist_ok=True)
    distinct.write_text("2\ndistinct\nH 0.2 0 0\nH 0 0 0.74\n", encoding="utf-8")
    contract = CrestArtifactContract(
        job_id="crest-duplicate",
        mode="standard",
        status="completed",
        reason="completed",
        job_dir=str(job_dir),
        latest_known_path=str(job_dir),
        retained_conformer_paths=(str(first), str(duplicate), str(distinct)),
    )

    stage_inputs = select_crest_downstream_inputs(
        contract, policy=CrestDownstreamPolicy.build(max_candidates=8)
    )

    assert [item.artifact_path for item in stage_inputs] == [str(first), str(distinct)]
    assert [item.rank for item in stage_inputs] == [1, 2]
