"""Submission evidence survives source edits and remains bound to the result."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.execution_binding import (
    build_orca_execution_snapshot,
    orca_execution_provenance,
)
from orca_auto.orca.machine_observation import artifact_receipt
from orca_auto.orca.queue.run_state_replay import record_cancelled_run_state
from orca_auto.orca.report.publication import write_report_files, write_report_json
from orca_auto.orca.state import new_state, save_state
from orca_auto.orca.state_reading import load_report_json, load_state
from tests.conftest import write_fake_orca


@pytest.fixture
def submitted_snapshot(tmp_path: Path) -> dict[str, Any]:
    job = tmp_path / "job"
    job.mkdir()
    selected = job / "sample.inp"
    original = b"! SP\n* xyzfile 0 1 geometry.xyz\n"
    selected.write_bytes(original)
    (job / "geometry.xyz").write_text("2\nsource\nH 0 0 0\nH 0 0 0.74\n")
    # Submission owns resource normalization; the original bytes stay untouched.
    normalized = b"%pal nprocs 2 end\n%maxcore 1024\n" + original
    return build_orca_execution_snapshot(
        job,
        selected,
        selected_input_xyz="",
        resource_request={"max_cores": 2, "max_memory_gb": 2},
        orca_executable=write_fake_orca(tmp_path / "orca"),
        normalized_selected_payload=normalized,
        source_selected_sha256=hashlib.sha256(original).hexdigest(),
    )


def test_execution_provenance_preserves_detached_submission_evidence(
    submitted_snapshot: dict[str, Any],
) -> None:
    snapshot = submitted_snapshot
    snapshot["recovery"] = {
        "previous_generation_name": "20260925-100000-aabbccdd",
        "seeded_roles": {"dependency_000000": {"sha256": "a" * 64}},
    }
    expected = copy.deepcopy(snapshot)
    provenance = orca_execution_provenance(snapshot)
    for field in ("source_inputs", "resource_request", "recovery"):
        assert provenance[field] == expected[field]
    assert (
        provenance["source_inputs"]["selected_source"]["sha256"]
        != (provenance["bound_selected_identity"]["sha256"])
    )
    snapshot["source_inputs"]["selected_source"]["sha256"] = "b" * 64
    snapshot["materialized_inputs"]["dependency_000000"]["sha256"] = "b" * 64
    snapshot["recovery"]["seeded_roles"].clear()
    for field in ("source_inputs", "materialized_inputs", "recovery"):
        assert provenance[field] == expected[field]


@pytest.fixture
def provenance_report(submitted_snapshot: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    snapshot = submitted_snapshot
    generation = Path(snapshot["execution_dir"])
    # Report publication must use captured evidence, not today's source bytes.
    Path(snapshot["source_selected_inp"]).write_text("changed after submission\n")
    (generation.parent / "geometry.xyz").unlink()
    selected = Path(snapshot["selected_inp"])
    output = selected.with_suffix(".out")
    output.write_text("****ORCA TERMINATED NORMALLY****\n")
    state = dict(new_state(generation.parent, selected))
    state.update(
        status="completed",
        execution_provenance=orca_execution_provenance(snapshot),
        attempts=[{"index": 1, "inp_path": str(selected), "out_path": str(output)}],
        final_result={
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "last_out_path": str(output),
        },
    )
    save_state(generation.parent, state)
    assert write_report_json(generation.parent, state) == generation / "machine.json"
    return generation, state


def test_result_binds_original_and_execution_input_without_rereading_sources(
    provenance_report: tuple[Path, dict[str, Any]],
    submitted_snapshot: dict[str, Any],
) -> None:
    generation, state = provenance_report
    machine = json.loads((generation / "machine.json").read_text())
    data = machine["payload"]["data"]
    artifact_id = data["results"]["execution_provenance_artifact"]
    assert artifact_id == "execution-provenance"
    assert artifact_id in data["artifact_refs"]
    receipt = machine["artifacts"][artifact_id]
    assert receipt == artifact_receipt(
        generation,
        generation / "execution_provenance.json",
        required=True,
        role="supporting-information",
        media_type="application/json",
    )
    provenance = json.loads((generation / receipt["path"]).read_text())
    assert provenance == state["execution_provenance"]
    assert provenance["source_inputs"] == submitted_snapshot["source_inputs"]
    assert provenance["resource_request"] == submitted_snapshot["resource_request"]
    assert (
        provenance["bound_selected_identity"]["sha256"]
        == (machine["artifacts"]["input"]["byte_sha256"])
    )
    assert (
        provenance["source_inputs"]["selected_source"]["sha256"]
        != (machine["artifacts"]["input"]["byte_sha256"])
    )
    assert load_report_json(generation, require_consumable_success=True) is not None


@pytest.mark.parametrize("change", ["missing", "content", "forged-receipt", "wrong-path"])
def test_report_rejects_missing_or_substituted_provenance(
    provenance_report: tuple[Path, dict[str, Any]], change: str
) -> None:
    generation, _ = provenance_report
    path = generation / "execution_provenance.json"
    machine_path = generation / "machine.json"
    machine = json.loads(machine_path.read_text())
    if change == "missing":
        path.unlink()
    elif change == "wrong-path":
        other = generation / "unowned-provenance.json"
        other.write_bytes(path.read_bytes())
        machine["artifacts"]["execution-provenance"]["path"] = other.name
    else:
        provenance = json.loads(path.read_text())
        provenance["source_inputs"]["selected_source"]["sha256"] = "f" * 64
        path.write_text(json.dumps(provenance))
        if change == "forged-receipt":
            machine["artifacts"]["execution-provenance"] = artifact_receipt(
                generation,
                path,
                required=True,
                role="supporting-information",
                media_type="application/json",
            )
    machine_path.write_text(json.dumps(machine))
    assert load_report_json(generation) is None


@pytest.mark.parametrize("change", ["reference", "receipt", "declared-reference"])
def test_report_rejects_broken_provenance_link(
    provenance_report: tuple[Path, dict[str, Any]], change: str
) -> None:
    generation, _ = provenance_report
    path = generation / "machine.json"
    machine = json.loads(path.read_text())
    if change == "reference":
        del machine["payload"]["data"]["results"]["execution_provenance_artifact"]
    elif change == "receipt":
        del machine["artifacts"]["execution-provenance"]
    else:
        machine["payload"]["data"]["artifact_refs"].remove("execution-provenance")
    path.write_text(json.dumps(machine))
    assert load_report_json(generation) is None


def test_terminal_republication_does_not_rewrite_provenance(
    provenance_report: tuple[Path, dict[str, Any]],
) -> None:
    generation, state = provenance_report
    path = generation / "execution_provenance.json"
    original = path.read_bytes()
    before = path.stat()
    assert write_report_json(generation.parent, state) == generation / "machine.json"
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    state["execution_provenance"]["source_inputs"]["selected_source"]["sha256"] = "f" * 64
    with pytest.raises(RuntimeError, match="immutable"):
        write_report_json(generation.parent, state)
    assert path.read_bytes() == original
    assert write_report_files(generation.parent, state)["report_json"] == str(
        generation / "machine.json"
    )
    assert path.read_bytes() == original


@pytest.mark.parametrize("legacy", [False, True])
def test_terminal_replay_preserves_published_provenance(
    provenance_report: tuple[Path, dict[str, Any]],
    submitted_snapshot: dict[str, Any],
    legacy: bool,
) -> None:
    generation, state = provenance_report
    machine_path = generation / "machine.json"
    if legacy:
        # An existing report from before provenance artifacts were introduced.
        machine = json.loads(machine_path.read_text())
        del machine["payload"]["data"]["results"]["execution_provenance_artifact"]
        machine["payload"]["data"]["artifact_refs"].remove("execution-provenance")
        del machine["artifacts"]["execution-provenance"]
        machine_path.write_text(json.dumps(machine))
        (generation / "execution_provenance.json").unlink()
        for field in ("source_inputs", "resource_request", "recovery"):
            state["execution_provenance"].pop(field)
        save_state(generation.parent, state)
    before = machine_path.read_bytes()
    assert load_report_json(generation, require_consumable_success=True) is not None
    replay_provenance = orca_execution_provenance(submitted_snapshot)
    replay_provenance["source_inputs"]["selected_source"]["sha256"] = "f" * 64
    _, status = record_cancelled_run_state(
        generation.parent,
        selected_inp=state["selected_inp"],
        execution_provenance=replay_provenance,
    )
    assert status == "completed"
    saved = load_state(generation.parent)
    assert saved is not None
    assert saved["execution_provenance"] == state["execution_provenance"]
    assert machine_path.read_bytes() == before
    assert load_report_json(generation, require_consumable_success=True) is not None
    assert (generation / "execution_provenance.json").exists() is not legacy
