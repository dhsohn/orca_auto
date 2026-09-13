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
from orca_auto.flow.contracts.crest import CrestArtifactContract, CrestDownstreamPolicy
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


def test_crest_contract_rejects_existing_artifacts_outside_job_dir(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.xyz"
    _write_xyz(outside)
    crest_job_dir = tmp_path / "crest_job"
    _write_crest_state(
        crest_job_dir,
        job_id="crest-job",
        engine_payload={"retained_conformer_paths": [str(outside)]},
    )
    with pytest.raises(ValueError, match="escapes job_dir"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(crest_job_dir))


def test_crest_adapter_requires_durable_index_record(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_unindexed"
    _write_json(
        job_dir / "job_state.json",
        artifact_payload(
            engine="crest",
            job_id="crest-unindexed",
            queue_id="queue-crest-unindexed",
            app_name="orca_auto_crest",
            generation="generation-crest-unindexed",
            job_dir=str(job_dir),
        ),
    )

    with pytest.raises(FileNotFoundError, match="index record not found"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


def test_crest_adapter_rejects_invalid_artifact_json(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_invalid_json"
    _write_crest_state(job_dir, job_id="crest-invalid-json")
    (job_dir / "job_state.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="not valid JSON"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


def test_crest_adapter_rejects_foreign_index_app(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_foreign_app"
    _write_crest_state(job_dir, job_id="crest-foreign-app")
    index_path = tmp_path / "job_locations.json"
    records = json.loads(index_path.read_text(encoding="utf-8"))
    records[0]["app_name"] = "orca_auto_xtb"
    _write_json(index_path, records)

    with pytest.raises(ValueError, match="Expected orca_auto_crest index record"):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("app_name", "index record app_name is missing"),
        ("job_id", "index record job_id is missing"),
        ("status", "index record status is missing"),
    ),
)
def test_crest_adapter_rejects_blank_required_index_identity(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    job_dir = tmp_path / f"crest_blank_{field}"
    job_id = f"crest-blank-{field}"
    _write_crest_state(job_dir, job_id=job_id)

    index_path = tmp_path / "job_locations.json"
    records = json.loads(index_path.read_text(encoding="utf-8"))
    matching = next(item for item in records if item.get("original_run_dir") == str(job_dir))
    matching[field] = ""
    _write_json(index_path, records)

    with pytest.raises(ValueError, match=message):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload, _job_dir: payload.update({"engine": "xtb"}), "state engine"),
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
def test_crest_adapter_rejects_invalid_state_envelope(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any], Path], None],
    message: str,
) -> None:
    job_dir = tmp_path / "crest_invalid_envelope"
    _write_crest_state(job_dir, job_id="crest-invalid-envelope")
    state_path = job_dir / "job_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    mutation(payload, job_dir)
    _write_json(state_path, payload)

    with pytest.raises(ValueError, match=message):
        load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))


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


def test_completed_crest_artifacts_require_terminal_output_identities(
    tmp_path: Path,
) -> None:
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


def test_load_crest_artifact_contract_carries_rejected_retained_outputs(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_rejections"
    selected_input_xyz = job_dir / "input.xyz"
    rotamers = job_dir / "crest_rotamers.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(rotamers)
    _write_crest_state(
        job_dir,
        job_id="crest_rejections_1",
        selected_input_xyz=selected_input_xyz,
        engine_payload={
            "retained_conformer_paths": [str(rotamers)],
            "rejected_retained_outputs": [
                {"name": "crest_conformers.xyz", "reason": "no_valid_frames"},
            ],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.rejected_retained_outputs == (
        {"name": "crest_conformers.xyz", "reason": "no_valid_frames"},
    )
    assert contract.to_dict()["rejected_retained_outputs"] == [
        {"name": "crest_conformers.xyz", "reason": "no_valid_frames"}
    ]


def test_load_crest_artifact_contract_without_rejections_key_yields_empty(tmp_path: Path) -> None:
    # Every job_state.json written before the field existed omits it, and those
    # jobs must keep loading unchanged.
    job_dir = tmp_path / "crest_no_rejections"
    selected_input_xyz = job_dir / "input.xyz"
    conformers = job_dir / "crest_conformers.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformers)
    _write_crest_state(
        job_dir,
        job_id="crest_no_rejections_1",
        selected_input_xyz=selected_input_xyz,
        engine_payload={"retained_conformer_paths": [str(conformers)]},
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.rejected_retained_outputs == ()


def test_load_crest_artifact_contract_drops_malformed_rejection_rows(tmp_path: Path) -> None:
    # A refusal row is commentary on a job that already finished, so a bad row
    # loses itself rather than the whole contract — unlike a retained path.
    job_dir = tmp_path / "crest_bad_rejections"
    selected_input_xyz = job_dir / "input.xyz"
    conformers = job_dir / "crest_conformers.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformers)
    _write_crest_state(
        job_dir,
        job_id="crest_bad_rejections_1",
        selected_input_xyz=selected_input_xyz,
        engine_payload={
            "retained_conformer_paths": [str(conformers)],
            "rejected_retained_outputs": [
                "crest_rotamers.xyz",
                {"reason": "no_valid_frames"},
                {"name": "crest_best.xyz"},
                {"name": 7, "reason": "no_valid_frames"},
                {"name": "crest_ensemble.xyz", "reason": ["no_valid_frames"]},
                {"name": "   ", "reason": "no_valid_frames"},
                {"name": "crest_rotamers.xyz", "reason": "identity_unreadable"},
            ],
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.rejected_retained_outputs == (
        {"name": "crest_rotamers.xyz", "reason": "identity_unreadable"},
    )


def test_load_crest_artifact_contract_ignores_a_non_list_rejection_field(tmp_path: Path) -> None:
    job_dir = tmp_path / "crest_scalar_rejections"
    selected_input_xyz = job_dir / "input.xyz"
    conformers = job_dir / "crest_conformers.xyz"

    _write_xyz(selected_input_xyz)
    _write_xyz(conformers)
    _write_crest_state(
        job_dir,
        job_id="crest_scalar_rejections_1",
        selected_input_xyz=selected_input_xyz,
        engine_payload={
            "retained_conformer_paths": [str(conformers)],
            "rejected_retained_outputs": "crest_conformers.xyz",
        },
    )

    contract = load_crest_artifact_contract(crest_index_root=tmp_path, target=str(job_dir))

    assert contract.rejected_retained_outputs == ()
