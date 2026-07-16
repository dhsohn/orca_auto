from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.engine_runner import executable_identity
from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.core.queue.engine.input_snapshot import (
    bind_direct_generation_owner,
    require_direct_generation_owner,
)
from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RuntimeConfig
from orca_auto.orca.execution_binding import orca_execution_provenance
from orca_auto.orca.job_locations import _contracts as _job_location_contracts
from orca_auto.orca.job_locations import _records as _job_location_records
from orca_auto.orca.job_locations import (
    collect_reindex_payload,
    index_root_for_cfg,
    load_job_artifact_context,
    load_job_artifacts,
    load_job_runtime_context,
    load_orca_contract_payload,
    record_from_artifacts,
    reindex_job_locations,
    resolve_latest_job_dir,
    upsert_job_record,
)
from orca_auto.orca.state import report_json_path, state_path
from tests.engine_artifact_helpers import orca_artifact_payload


def _load_job_locations(root: Path) -> list[dict[str, object]]:
    path = root / "job_locations.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else []


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _orca_payload(
    *,
    job_id: str,
    run_id: str = "",
    reaction_dir: Path,
    selected_inp: Path | str = "",
    selected_xyz_path: Path | str = "",
    status: str = "completed",
    attempts: list[dict[str, object]] | None = None,
    final_result: dict[str, object] | None = None,
    max_retries: int = 0,
    resource_request: dict[str, object] | None = None,
    resource_actual: dict[str, object] | None = None,
    engine_payload_extra: dict[str, object] | None = None,
    artifacts_extra: dict[str, object] | None = None,
) -> dict[str, object]:
    return orca_artifact_payload(
        job_id=job_id,
        run_id=run_id or job_id,
        reaction_dir=str(reaction_dir),
        selected_inp=str(selected_inp) if selected_inp else "",
        selected_xyz_path=str(selected_xyz_path) if selected_xyz_path else "",
        status=status,
        attempts=attempts,
        final_result=final_result,
        max_retries=max_retries,
        resource_request=resource_request,
        resource_actual=resource_actual,
        engine_payload_extra=engine_payload_extra,
        artifacts_extra=artifacts_extra,
    )


def _write_orca_state(reaction_dir: Path, **kwargs: Any) -> None:
    _write_json(state_path(reaction_dir), _orca_payload(reaction_dir=reaction_dir, **kwargs))


def _write_orca_report(reaction_dir: Path, **kwargs: Any) -> None:
    _write_json(report_json_path(reaction_dir), _orca_payload(reaction_dir=reaction_dir, **kwargs))


def _make_cfg(root: Path) -> AppConfig:
    fake_orca = root / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    return AppConfig(
        runtime=RuntimeConfig(
            allowed_root=str(root / "runs"),
        ),
        paths=PathsConfig(orca_executable=str(fake_orca)),
        resources=CommonResourceConfig(max_cores_per_task=8, max_memory_gb_per_task=16),
    )


def _execution_snapshot_locator(job_dir: Path, generation_dir: Path) -> dict[str, object]:
    job_stat = job_dir.stat()
    generation_stat = generation_dir.stat()
    selected_inputs = list(generation_dir.glob("*.inp"))
    assert len(selected_inputs) == 1
    selected_inp = selected_inputs[0].resolve()
    owner_token = hashlib.sha256(str(generation_dir.resolve()).encode()).hexdigest()
    try:
        require_direct_generation_owner(
            job_dir,
            namespace=generation_dir.name,
            expected_job_identity=(int(job_stat.st_dev), int(job_stat.st_ino)),
            expected_generation_identity=(
                int(generation_stat.st_dev),
                int(generation_stat.st_ino),
            ),
            owner_token=owner_token,
        )
    except ValueError:
        bind_direct_generation_owner(
            job_dir,
            namespace=generation_dir.name,
            expected_job_identity=(int(job_stat.st_dev), int(job_stat.st_ino)),
            expected_generation_identity=(
                int(generation_stat.st_dev),
                int(generation_stat.st_ino),
            ),
            owner_token=owner_token,
        )
    snapshot: dict[str, object] = {
        "version": 2,
        "job_dir_identity": {
            "device": int(job_stat.st_dev),
            "inode": int(job_stat.st_ino),
        },
        "generation_name": generation_dir.name,
        "execution_dir_identity": {
            "device": int(generation_stat.st_dev),
            "inode": int(generation_stat.st_ino),
        },
        "execution_dir": str(generation_dir.resolve()),
        "snapshot_intent_token": owner_token,
        "selected_inp": str(selected_inp),
        "bound_selected_identity": executable_identity(selected_inp),
    }
    canonical_provenance = orca_execution_provenance(snapshot)
    for artifact_path in (state_path(generation_dir), report_json_path(generation_dir)):
        if not artifact_path.is_file():
            continue
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        engine_payload = payload.get("engine_payload")
        if not isinstance(engine_payload, dict):
            continue
        engine_payload["execution_provenance"] = canonical_provenance
        _write_json(artifact_path, payload)
    return snapshot


def _write_generation_payloads(
    *,
    job_dir: Path,
    generation_dir: Path,
    job_id: str,
    run_id: str,
) -> tuple[Path, Path]:
    selected_inp = generation_dir / "nebts.inp"
    selected_inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    out = generation_dir / "nebts.out"
    out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    final_result: dict[str, object] = {
        "status": "completed",
        "analyzer_status": "completed",
        "reason": "normal_termination",
        "last_out_path": str(out.resolve()),
    }
    payload = _orca_payload(
        job_id=job_id,
        run_id=run_id,
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        status="completed",
        attempts=[
            {
                "index": 1,
                "inp_path": str(selected_inp.resolve()),
                "out_path": str(out.resolve()),
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
            }
        ],
        final_result=final_result,
    )
    _write_json(state_path(generation_dir), payload)
    _write_json(report_json_path(generation_dir), payload)
    return selected_inp, out


def test_upsert_job_record_writes_allowed_root_index_and_resolves_latest_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = _make_cfg(root)
        allowed_root = Path(cfg.runtime.allowed_root)
        allowed_root.mkdir(parents=True)
        job_dir = allowed_root / "rxn_a"
        job_dir.mkdir()
        inp = job_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        _write_orca_state(
            job_dir,
            job_id="job_live_1",
            run_id="run_live_1",
            selected_inp=inp,
            status="queued",
        )
        _write_orca_report(
            job_dir,
            job_id="job_live_1",
            run_id="run_live_1",
            selected_inp=inp,
            status="queued",
        )

        record = upsert_job_record(
            cfg,
            job_id="job_live_1",
            status="queued",
            job_dir=job_dir,
            job_type="opt",
            selected_input_xyz=str(inp),
            molecule_key="H2",
            resource_request={"max_cores": 8, "max_memory_gb": 16},
            resource_actual={"max_cores": 8, "max_memory_gb": 16},
        )

        assert record.job_id == "job_live_1"
        assert index_root_for_cfg(cfg) == allowed_root.resolve()
        assert resolve_latest_job_dir(index_root_for_cfg(cfg), "job_live_1") == job_dir.resolve()
        loaded = _load_job_locations(index_root_for_cfg(cfg))
        assert len(loaded) == 1
        assert loaded[0]["job_id"] == "job_live_1"
        assert loaded[0]["original_run_dir"] == str(job_dir.resolve())
        job_path, loaded_state, loaded_report = load_job_artifacts(
            index_root_for_cfg(cfg), "job_live_1"
        )
        assert job_path == job_dir.resolve()
        assert loaded_state is not None and loaded_state["job_id"] == "job_live_1"
        assert loaded_report is not None and loaded_report["job"]["id"] == "job_live_1"


def test_record_from_artifacts_uses_run_id_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        job_dir = root / "runs" / "rxn_b"
        job_dir.mkdir(parents=True)
        selected_inp = job_dir / "rxn.inp"
        selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        state: dict[str, object] = {
            "run_id": "run_hist_1",
            "status": "completed",
            "selected_inp": str(selected_inp),
            "attempts": [],
            "final_result": None,
        }

        record = record_from_artifacts(
            job_dir=job_dir,
            state=state,
            report=None,
        )

        assert record is not None
        assert record.job_id == "run_hist_1"
        assert record.original_run_dir == str(job_dir.resolve())
        assert record.latest_known_path == str(job_dir.resolve())
        assert record.job_type == "orca_opt"
        assert record.molecule_key == "H2"


def test_resolve_latest_job_dir_and_load_job_artifacts_cover_job_and_path_targets() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        job_dir = allowed_root / "rxn_hist_1"
        allowed_root.mkdir()
        job_dir.mkdir()

        inp = job_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        _write_orca_state(
            job_dir,
            job_id="job_hist_1",
            run_id="run_hist_1",
            selected_inp=inp,
        )
        _write_orca_report(
            job_dir,
            job_id="job_hist_1",
            run_id="run_hist_1",
            selected_inp=inp,
        )
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "job_hist_1",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(job_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "latest_known_path": str(job_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        for target in ("job_hist_1", "run_hist_1", str(job_dir)):
            assert resolve_latest_job_dir(allowed_root, target) == job_dir.resolve()
            job_path, loaded_state, loaded_report = load_job_artifacts(allowed_root, target)
            assert job_path == job_dir.resolve()
            assert loaded_state is not None and loaded_state["run_id"] == "run_hist_1"
            assert loaded_report is not None and loaded_report["job"]["id"] == "job_hist_1"


def test_load_job_artifacts_resolves_path_target_when_index_lookup_is_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        job_dir = allowed_root / "rxn_hist_2"
        allowed_root.mkdir()
        job_dir.mkdir()

        _write_orca_state(
            job_dir,
            job_id="job_hist_2",
            run_id="run_hist_2",
            selected_inp=job_dir / "rxn.inp",
        )
        _write_orca_report(
            job_dir,
            job_id="job_hist_2",
            run_id="run_hist_2",
            selected_inp=job_dir / "rxn.inp",
        )

        assert resolve_latest_job_dir(allowed_root, str(job_dir)) == job_dir.resolve()
        job_path, loaded_state, loaded_report = load_job_artifacts(allowed_root, str(job_dir))
        assert job_path == job_dir.resolve()
        assert loaded_state is not None and loaded_state["job_id"] == "job_hist_2"
        assert (
            loaded_report is not None and loaded_report["engine_payload"]["run_id"] == "run_hist_2"
        )


def test_load_job_artifact_context_includes_record_for_run_id_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        job_dir = allowed_root / "rxn_hist_3"
        allowed_root.mkdir()
        job_dir.mkdir()

        inp = job_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        _write_orca_state(
            job_dir,
            job_id="job_hist_3",
            run_id="run_hist_3",
            selected_inp=inp,
        )
        _write_orca_report(
            job_dir,
            job_id="job_hist_3",
            run_id="run_hist_3",
            selected_inp=inp,
        )
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "job_hist_3",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(job_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "latest_known_path": str(job_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        context = load_job_artifact_context(allowed_root, "run_hist_3")

        assert context.record is not None
        assert context.record.job_id == "job_hist_3"
        assert context.job_dir == job_dir.resolve()
        assert context.state is not None and context.state["run_id"] == "run_hist_3"
        assert context.report is not None and context.report["job"]["id"] == "job_hist_3"


def test_load_job_runtime_context_exposes_queue_entry() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        job_dir = allowed_root / "rxn_hist_4"
        allowed_root.mkdir()
        job_dir.mkdir()

        inp = job_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
        _write_orca_state(
            job_dir,
            job_id="job_hist_4",
            run_id="run_hist_4",
            selected_inp=inp,
        )
        _write_orca_report(
            job_dir,
            job_id="job_hist_4",
            run_id="run_hist_4",
            selected_inp=inp,
        )
        _write_json(
            allowed_root / "queue.json",
            [
                {
                    "queue_id": "q_hist_4",
                    "task_id": "job_hist_4",
                    "status": "completed",
                    "cancel_requested": False,
                    "metadata": {
                        "run_id": "run_hist_4",
                        "reaction_dir": str(job_dir),
                    },
                }
            ],
        )
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "job_hist_4",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(job_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "latest_known_path": str(job_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        context = load_job_runtime_context(
            allowed_root,
            "job_hist_4",
        )

        assert context.queue_entry is not None
        assert context.queue_entry["queue_id"] == "q_hist_4"
        assert context.artifact.job_dir == job_dir.resolve()
        assert (
            context.artifact.state is not None and context.artifact.state["run_id"] == "run_hist_4"
        )
        assert (
            context.artifact.report is not None
            and context.artifact.report["job"]["id"] == "job_hist_4"
        )


def test_load_orca_contract_payload_returns_normalized_runtime_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        job_dir = allowed_root / "rxn_hist_5"
        allowed_root.mkdir()
        job_dir.mkdir()

        inp = job_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
        xyz = job_dir / "rxn.xyz"
        xyz.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
        out = job_dir / "rxn.out"
        out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
        attempts = [
            {
                "index": 2,
                "inp_path": str(inp),
                "out_path": str(out),
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
                "markers": {"terminated_normally": True, "imaginary_frequency_count": 0},
                "patch_actions": [],
            }
        ]
        final_result = {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "2026-04-19T00:10:00+00:00",
            "last_out_path": str(out),
        }
        _write_orca_state(
            job_dir,
            job_id="job_hist_5",
            run_id="run_hist_5",
            selected_inp=inp,
            selected_xyz_path=xyz,
            attempts=attempts,
            final_result=final_result,
            max_retries=3,
        )
        _write_orca_report(
            job_dir,
            job_id="job_hist_5",
            run_id="run_hist_5",
            selected_inp=inp,
            selected_xyz_path=xyz,
            attempts=attempts,
            final_result=final_result,
            max_retries=3,
        )
        _write_json(
            allowed_root / "queue.json",
            [
                {
                    "queue_id": "q_hist_5",
                    "task_id": "job_hist_5",
                    "status": "completed",
                    "cancel_requested": False,
                    "metadata": {
                        "run_id": "run_hist_5",
                        "reaction_dir": str(job_dir),
                        "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                        "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                    },
                }
            ],
        )
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "job_hist_5",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(job_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "latest_known_path": str(job_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        payload = load_orca_contract_payload(
            allowed_root,
            "job_hist_5",
        )

        assert payload["run_id"] == "run_hist_5"
        assert payload["status"] == "completed"
        assert payload["reason"] == "normal_termination"
        assert payload["reaction_dir"] == str(job_dir.resolve())
        assert payload["latest_known_path"] == str(job_dir.resolve())
        assert payload["queue_id"] == "q_hist_5"
        assert payload["queue_status"] == "completed"
        assert payload["selected_inp"] == str(inp.resolve())
        assert payload["selected_input_xyz"] == str(xyz.resolve())
        assert payload["optimized_xyz_path"] == str(xyz.resolve())
        assert payload["last_out_path"] == str(out.resolve())
        assert payload["attempt_count"] == 1
        assert payload["attempts"][0]["markers"] == {
            "terminated_normally": True,
            "imaginary_frequency_count": 0,
        }
        assert payload["max_retries"] == 3
        assert payload["resource_request"] == {"max_cores": 8, "max_memory_gb": 16}


def test_load_orca_contract_payload_reads_exact_historical_visible_generation(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "TS8(NEB-TS)"
    first_generation = job_dir / "20260714-224054-11111111"
    second_generation = job_dir / "20260714-224155-22222222"
    first_generation.mkdir(parents=True)
    second_generation.mkdir()
    first_inp, first_out = _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=first_generation,
        job_id="job_first",
        run_id="run_first",
    )
    second_inp, _second_out = _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=second_generation,
        job_id="job_second",
        run_id="run_second",
    )

    root_state = state_path(job_dir)
    root_report = report_json_path(job_dir)
    root_state.write_bytes(state_path(first_generation).read_bytes())
    root_report.write_bytes(report_json_path(first_generation).read_bytes())
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_first",
                "task_id": "job_first",
                "status": "completed",
                "metadata": {
                    "run_id": "run_first",
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(first_inp.resolve()),
                    "execution_snapshot": _execution_snapshot_locator(job_dir, first_generation),
                },
            },
            {
                "queue_id": "q_second",
                "task_id": "job_second",
                "status": "pending",
                "metadata": {
                    "run_id": "run_second",
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(second_inp.resolve()),
                    "execution_snapshot": _execution_snapshot_locator(job_dir, second_generation),
                },
            },
        ],
    )

    before_root_advance = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_first",
    )
    root_state.write_bytes(state_path(second_generation).read_bytes())
    root_report.write_bytes(report_json_path(second_generation).read_bytes())
    root_latest_xyz = job_dir / "root-latest.xyz"
    root_latest_xyz.write_text("1\nroot latest\nH 0 0 0\n", encoding="utf-8")
    root_state_bytes = root_state.read_bytes()
    root_report_bytes = root_report.read_bytes()
    historical = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_first",
    )
    latest = load_orca_contract_payload(allowed_root, str(job_dir))

    assert before_root_advance["run_state_path"] == str(state_path(first_generation).resolve())
    assert before_root_advance["report_json_path"] == str(
        report_json_path(first_generation).resolve()
    )
    assert historical["queue_id"] == "q_first"
    assert historical["run_id"] == "run_first"
    assert historical["reaction_dir"] == str(job_dir.resolve())
    assert historical["selected_inp"] == str(first_inp.resolve())
    assert historical["selected_input_xyz"] == ""
    assert historical["optimized_xyz_path"] == ""
    assert historical["last_out_path"] == str(first_out.resolve())
    assert historical["attempt_count"] == 1
    assert historical["run_state_path"] == str(state_path(first_generation).resolve())
    assert historical["report_json_path"] == str(report_json_path(first_generation).resolve())
    assert historical["report_md_path"] == ""
    assert latest["run_id"] == "run_second"
    assert latest["run_state_path"] == str(state_path(second_generation).resolve())
    assert latest["report_json_path"] == str(report_json_path(second_generation).resolve())
    assert latest["optimized_xyz_path"] == ""
    assert root_state.read_bytes() == root_state_bytes
    assert root_report.read_bytes() == root_report_bytes


def test_pending_visible_generation_never_treats_staged_neb_xyz_as_optimized(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "TS8(NEB-TS)"
    generation = job_dir / "20260714-224054-959479f2"
    generation.mkdir(parents=True)
    selected_inp = generation / "nebts.inp"
    selected_inp.write_text(
        '! NEB-TS\n%neb Product "output.xyz" TS "guessTS.xyz" end\n* xyzfile 0 1 input.xyz\n',
        encoding="utf-8",
    )
    staged_xyz = []
    for name, comment in (
        ("input.xyz", "reactant"),
        ("output.xyz", "product"),
        ("guessTS.xyz", "guess"),
    ):
        path = generation / name
        path.write_text(f"1\n{comment}\nH 0 0 0\n", encoding="utf-8")
        staged_xyz.append(path)
    snapshot = _execution_snapshot_locator(job_dir, generation)
    snapshot["materialized_inputs"] = {
        f"dependency_{index:06d}": executable_identity(path)
        for index, path in enumerate(staged_xyz)
    }
    snapshot["runtime_mutable_input_roles"] = []
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_neb_pending",
                "task_id": "job_neb_pending",
                "status": "pending",
                "metadata": {
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(selected_inp.resolve()),
                    "selected_input_xyz": str((job_dir / "input.xyz").resolve()),
                    "execution_snapshot": snapshot,
                },
            }
        ],
    )

    payload = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_neb_pending",
    )

    assert payload["queue_status"] == "pending"
    assert payload["selected_inp"] == str(selected_inp.resolve())
    assert payload["optimized_xyz_path"] == ""


def test_same_stem_xyz_becomes_optimized_only_after_attempt_mutates_it(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "opt"
    generation = job_dir / "20260714-224054-11111111"
    generation.mkdir(parents=True)
    source_xyz = job_dir / "h2.xyz"
    source_xyz.write_text("2\nsource\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    selected_inp = generation / "h2.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    runtime_xyz = generation / "h2.xyz"
    runtime_xyz.write_bytes(source_xyz.read_bytes())
    initial_identity = executable_identity(runtime_xyz)
    snapshot = _execution_snapshot_locator(job_dir, generation)
    snapshot["materialized_inputs"] = {"dependency_000000": initial_identity}
    snapshot["runtime_mutable_input_roles"] = ["dependency_000000"]
    queue_payload = [
        {
            "queue_id": "q_same_stem",
            "task_id": "job_same_stem",
            "status": "pending",
            "metadata": {
                "run_id": "run_same_stem",
                "reaction_dir": str(job_dir.resolve()),
                "selected_inp": str(selected_inp.resolve()),
                "selected_input_xyz": str(source_xyz.resolve()),
                "execution_snapshot": snapshot,
            },
        }
    ]
    _write_json(allowed_root / "queue.json", queue_payload)

    queued = load_orca_contract_payload(allowed_root, str(job_dir), queue_id="q_same_stem")

    assert queued["optimized_xyz_path"] == ""

    out = generation / "h2.out"
    out.write_text("ORCA failed after launch\n", encoding="utf-8")
    attempted_payload = _orca_payload(
        job_id="job_same_stem",
        run_id="run_same_stem",
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        selected_xyz_path=source_xyz,
        status="failed",
        attempts=[{"index": 1, "out_path": str(out.resolve()), "return_code": 1}],
        final_result={
            "status": "failed",
            "reason": "engine_exit_nonzero",
            "last_out_path": str(out.resolve()),
        },
        engine_payload_extra={"execution_provenance": orca_execution_provenance(snapshot)},
    )
    _write_json(state_path(generation), attempted_payload)
    _write_json(report_json_path(generation), attempted_payload)
    queue_payload[0]["status"] = "failed"
    _write_json(allowed_root / "queue.json", queue_payload)

    unchanged = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_same_stem",
    )
    runtime_xyz.write_text(
        "2\noptimized\nH 0 0 0\nH 0 0 0.75\n",
        encoding="utf-8",
    )
    mutated = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_same_stem",
    )

    assert unchanged["optimized_xyz_path"] == ""
    assert mutated["optimized_xyz_path"] == str(runtime_xyz.resolve())


def test_queue_absent_visible_generation_uses_provenance_and_rejects_unowned_replacement(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "neb"
    generation = job_dir / "20260714-224054-22222222"
    generation.mkdir(parents=True)
    selected_inp = generation / "nebts.inp"
    selected_inp.write_text("! NEB-TS\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    staged_xyz: list[Path] = []
    for name, comment in (("input.xyz", "reactant"), ("output.xyz", "product")):
        path = generation / name
        path.write_text(f"1\n{comment}\nH 0 0 0\n", encoding="utf-8")
        staged_xyz.append(path)
    out = generation / "nebts.out"
    out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    calculated_xyz = generation / "nebts.xyz"
    calculated_xyz.write_text("1\ncalculated\nH 0 0 0.1\n", encoding="utf-8")
    snapshot = _execution_snapshot_locator(job_dir, generation)
    snapshot["materialized_inputs"] = {
        f"dependency_{index:06d}": executable_identity(path)
        for index, path in enumerate(staged_xyz)
    }
    snapshot["runtime_mutable_input_roles"] = []
    provenance: dict[str, object] = {
        "execution_dir": snapshot["execution_dir"],
        "execution_dir_identity": snapshot["execution_dir_identity"],
        "generation_owner_token": snapshot["snapshot_intent_token"],
        "bound_selected_identity": snapshot["bound_selected_identity"],
        "materialized_inputs": snapshot["materialized_inputs"],
        "runtime_mutable_input_roles": [],
    }

    def publish(payload_provenance: dict[str, object]) -> None:
        payload = _orca_payload(
            job_id="job_queue_absent",
            run_id="run_queue_absent",
            reaction_dir=job_dir,
            selected_inp=selected_inp,
            selected_xyz_path=staged_xyz[0],
            status="completed",
            attempts=[{"index": 1, "out_path": str(out.resolve()), "return_code": 0}],
            final_result={
                "status": "completed",
                "reason": "normal_termination",
                "last_out_path": str(out.resolve()),
            },
            engine_payload_extra={"execution_provenance": payload_provenance},
        )
        for directory in (job_dir, generation):
            _write_json(state_path(directory), payload)
            _write_json(report_json_path(directory), payload)

    publish(provenance)
    valid = load_orca_contract_payload(allowed_root, str(job_dir))

    assert valid["status"] == "completed"
    assert valid["selected_inp"] == str(selected_inp.resolve())
    assert valid["optimized_xyz_path"] == str(calculated_xyz.resolve())

    moved_generation = job_dir / "moved-original-generation"
    generation.rename(moved_generation)
    generation.mkdir()
    for name in ("nebts.inp", "input.xyz", "output.xyz", "nebts.out", "nebts.xyz"):
        (generation / name).write_bytes((moved_generation / name).read_bytes())
    replacement_status = generation.stat()
    provenance["execution_dir_identity"] = {
        "device": int(replacement_status.st_dev),
        "inode": int(replacement_status.st_ino),
    }
    publish(provenance)

    replacement = load_orca_contract_payload(allowed_root, str(job_dir))

    assert replacement["status"] == "unknown"
    assert replacement["reason"] == "queue_generation_verification_failed"
    assert replacement["selected_inp"] == ""
    assert replacement["optimized_xyz_path"] == ""
    assert replacement["last_out_path"] == ""


def test_queue_snapshot_rejects_generation_payload_with_other_provenance(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "cross-generation"
    first_generation = job_dir / "20260714-224054-33333333"
    second_generation = job_dir / "20260714-224155-44444444"
    first_generation.mkdir(parents=True)
    second_generation.mkdir()
    first_inp, _first_out = _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=first_generation,
        job_id="job_cross",
        run_id="run_cross",
    )
    _second_inp, second_out = _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=second_generation,
        job_id="job_second",
        run_id="run_second",
    )
    first_snapshot = _execution_snapshot_locator(job_dir, first_generation)
    second_snapshot = _execution_snapshot_locator(job_dir, second_generation)
    cross_payload = _orca_payload(
        job_id="job_cross",
        run_id="run_cross",
        reaction_dir=job_dir,
        selected_inp=first_inp,
        status="completed",
        attempts=[{"index": 1, "out_path": str(second_out.resolve()), "return_code": 0}],
        final_result={
            "status": "completed",
            "reason": "normal_termination",
            "last_out_path": str(second_out.resolve()),
        },
        engine_payload_extra={"execution_provenance": orca_execution_provenance(second_snapshot)},
    )
    _write_json(state_path(first_generation), cross_payload)
    _write_json(report_json_path(first_generation), cross_payload)
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_cross",
                "task_id": "job_cross",
                "status": "completed",
                "metadata": {
                    "run_id": "run_cross",
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(first_inp.resolve()),
                    "execution_snapshot": first_snapshot,
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, str(job_dir), queue_id="q_cross")

    assert payload["status"] == "unknown"
    assert payload["reason"] == "queue_generation_verification_failed"
    assert payload["selected_inp"] == ""
    assert payload["last_out_path"] == ""


def test_visible_generation_rejects_out_of_generation_output_hints(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "output-escape"
    generation = job_dir / "20260714-224054-55555555"
    generation.mkdir(parents=True)
    selected_inp, _generation_out = _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=generation,
        job_id="job_output_escape",
        run_id="run_output_escape",
    )
    snapshot = _execution_snapshot_locator(job_dir, generation)
    root_out = job_dir / "root.out"
    root_out.write_text("outside generation\n", encoding="utf-8")
    root_xyz = job_dir / "root.xyz"
    root_xyz.write_text("1\noutside generation\nH 0 0 0\n", encoding="utf-8")
    bad_payload = _orca_payload(
        job_id="job_output_escape",
        run_id="run_output_escape",
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        status="completed",
        attempts=[{"index": 1, "out_path": str(root_out.resolve()), "return_code": 0}],
        final_result={
            "status": "completed",
            "reason": "normal_termination",
            "last_out_path": str(root_out.resolve()),
        },
        engine_payload_extra={"execution_provenance": orca_execution_provenance(snapshot)},
    )
    for artifact_path in (state_path(generation), report_json_path(generation)):
        _write_json(artifact_path, bad_payload)
    queue_payload = [
        {
            "queue_id": "q_output_escape",
            "task_id": "job_output_escape",
            "status": "completed",
            "metadata": {
                "run_id": "run_output_escape",
                "reaction_dir": str(job_dir.resolve()),
                "selected_inp": str(selected_inp.resolve()),
                "execution_snapshot": snapshot,
            },
        }
    ]
    _write_json(allowed_root / "queue.json", queue_payload)

    by_queue = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_output_escape",
    )
    by_job_path = load_orca_contract_payload(allowed_root, str(job_dir))
    _write_json(state_path(job_dir), bad_payload)
    _write_json(report_json_path(job_dir), bad_payload)
    _write_json(allowed_root / "queue.json", [])
    without_queue = load_orca_contract_payload(allowed_root, str(job_dir))

    for payload in (by_queue, by_job_path, without_queue):
        assert payload["status"] == "unknown"
        assert payload["reason"] == "queue_generation_verification_failed"
        assert payload["optimized_xyz_path"] == ""
        assert payload["last_out_path"] == ""


def test_load_orca_contract_payload_validates_learned_run_id_from_running_generation(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_live_generation"
    generation = job_dir / "20260715-010203-33333333"
    generation.mkdir(parents=True)
    selected_inp = generation / "live.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    running_payload = _orca_payload(
        job_id="job_live",
        run_id="run_live",
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        status="running",
    )
    _write_json(state_path(generation), running_payload)
    _write_json(report_json_path(generation), running_payload)
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_live",
                "task_id": "job_live",
                "status": "running",
                "metadata": {
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(selected_inp.resolve()),
                    "execution_snapshot": _execution_snapshot_locator(job_dir, generation),
                },
            }
        ],
    )

    learned = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_live",
        run_id="run_live",
    )
    mismatched = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_live",
        run_id="run_wrong",
    )

    assert learned["queue_id"] == "q_live"
    assert learned["run_id"] == "run_live"
    assert learned["status"] == "running"
    assert learned["selected_inp"] == str(selected_inp.resolve())
    assert learned["run_state_path"] == str(state_path(generation).resolve())
    assert learned["report_json_path"] == str(report_json_path(generation).resolve())
    assert mismatched["queue_id"] == "q_live"
    assert mismatched["run_id"] == "run_wrong"
    assert mismatched["status"] == "unknown"
    assert mismatched["reason"] == "queue_generation_not_found"
    assert mismatched["selected_inp"] == ""
    assert mismatched["run_state_path"] == ""
    assert mismatched["report_json_path"] == ""


@pytest.mark.parametrize(
    ("queue_id", "run_id"),
    [
        ("q_missing", ""),
        ("q_current", "run_wrong"),
        ("", "run_missing"),
    ],
)
def test_load_orca_contract_payload_rejects_missing_explicit_queue_generation(
    tmp_path: Path,
    queue_id: str,
    run_id: str,
) -> None:
    from orca_auto.flow.adapters.orca import load_orca_artifact_contract

    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_selector_miss"
    job_dir.mkdir(parents=True)
    selected_inp = job_dir / "current.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    _write_orca_state(
        job_dir,
        job_id="job_current",
        run_id="run_current",
        selected_inp=selected_inp,
    )
    _write_orca_report(
        job_dir,
        job_id="job_current",
        run_id="run_current",
        selected_inp=selected_inp,
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_current",
                "task_id": "job_current",
                "status": "completed",
                "metadata": {
                    "run_id": "run_current",
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(selected_inp.resolve()),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=str(job_dir),
    )
    contract = load_orca_artifact_contract(
        target=str(job_dir),
        orca_allowed_root=allowed_root,
        queue_id=queue_id,
        run_id=run_id,
        reaction_dir=str(job_dir),
    )

    assert payload["queue_id"] == queue_id
    assert payload["run_id"] == run_id
    assert payload["status"] == "unknown"
    assert payload["reason"] == "queue_generation_not_found"
    assert payload["reaction_dir"] == ""
    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""
    assert payload["last_out_path"] == ""
    assert contract.queue_id == queue_id
    assert contract.run_id == run_id
    assert contract.status == "unknown"
    assert contract.reason == "queue_generation_not_found"
    assert contract.reaction_dir == str(job_dir)
    assert contract.run_state_path == ""
    assert contract.report_json_path == ""
    assert contract.last_out_path == ""


@pytest.mark.parametrize(
    "tamper",
    [
        "execution_escape",
        "generation_inode",
        "job_inode",
        "generation_symlink",
        "artifact_symlink",
        "bound_selected",
    ],
)
def test_load_orca_contract_payload_rejects_unverified_historical_generation(
    tmp_path: Path,
    tamper: str,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_historical_tamper"
    generation = job_dir / "20260714-224054-33333333"
    generation.mkdir(parents=True)
    selected_inp, out = _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=generation,
        job_id="job_old",
        run_id="run_old",
    )
    snapshot = _execution_snapshot_locator(job_dir, generation)
    if tamper == "execution_escape":
        outside = tmp_path / "20260714-224054-44444444"
        outside.mkdir()
        snapshot["generation_name"] = outside.name
        snapshot["execution_dir"] = str(outside)
        outside_stat = outside.stat()
        snapshot["execution_dir_identity"] = {
            "device": int(outside_stat.st_dev),
            "inode": int(outside_stat.st_ino),
        }
    elif tamper == "generation_inode":
        assert isinstance(snapshot["execution_dir_identity"], dict)
        snapshot["execution_dir_identity"]["inode"] = -1
    elif tamper == "job_inode":
        assert isinstance(snapshot["job_dir_identity"], dict)
        snapshot["job_dir_identity"]["inode"] = -1
    elif tamper == "generation_symlink":
        outside = tmp_path / generation.name
        generation.rename(outside)
        generation.symlink_to(outside, target_is_directory=True)
    elif tamper == "artifact_symlink":
        outside = tmp_path / "outside_artifacts"
        outside.mkdir()
        outside_state = outside / "job_state.json"
        outside_report = outside / "job_report.json"
        state_path(generation).replace(outside_state)
        report_json_path(generation).replace(outside_report)
        state_path(generation).symlink_to(outside_state)
        report_json_path(generation).symlink_to(outside_report)
    else:
        selected_inp.write_text("! replacement\n* xyz 0 1\nHe 0 0 0\n*\n", encoding="utf-8")

    matching_root_payload = _orca_payload(
        job_id="job_old",
        run_id="run_old",
        reaction_dir=job_dir,
        selected_inp=selected_inp,
        attempts=[{"index": 1, "out_path": str(out.resolve())}],
        final_result={
            "status": "completed",
            "reason": "normal_termination",
            "last_out_path": str(out.resolve()),
        },
    )
    _write_json(state_path(job_dir), matching_root_payload)
    _write_json(report_json_path(job_dir), matching_root_payload)
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_old",
                "task_id": "job_old",
                "status": "completed",
                "metadata": {
                    "run_id": "run_old",
                    "reaction_dir": str(job_dir.resolve()),
                    "selected_inp": str(selected_inp.resolve()),
                    "execution_snapshot": snapshot,
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, "q_old")
    payload_by_job_path = load_orca_contract_payload(allowed_root, str(job_dir))

    assert payload["status"] == "unknown"
    assert payload["reason"] == "queue_generation_verification_failed"
    assert payload["queue_status"] == "completed"
    assert payload["selected_inp"] == ""
    assert payload["selected_input_xyz"] == ""
    assert payload["optimized_xyz_path"] == ""
    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""
    assert payload["last_out_path"] == ""
    assert payload["attempt_count"] == 0
    assert payload_by_job_path["status"] == "unknown"
    assert payload_by_job_path["reason"] == "queue_generation_verification_failed"
    assert payload_by_job_path["selected_inp"] == ""
    assert payload_by_job_path["optimized_xyz_path"] == ""


@pytest.mark.parametrize(
    ("state_job_id", "report_job_id", "expected_state_path", "expected_report_paths"),
    [
        ("job_old", "job_new", False, True),
        ("job_new", "job_old", True, False),
    ],
)
def test_load_orca_contract_payload_gates_runtime_paths_per_queue_generation(
    tmp_path: Path,
    state_job_id: str,
    report_job_id: str,
    expected_state_path: bool,
    expected_report_paths: bool,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_generation"
    job_dir.mkdir(parents=True)
    selected_inp = job_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    _write_orca_state(
        job_dir,
        job_id=state_job_id,
        run_id="run_new" if state_job_id == "job_new" else "run_old",
        selected_inp=selected_inp,
        status="running",
    )
    _write_orca_report(
        job_dir,
        job_id=report_job_id,
        run_id="run_new" if report_job_id == "job_new" else "run_old",
        selected_inp=selected_inp,
        status="running",
    )
    report_md = job_dir / "job_report.md"
    report_md.write_text(
        "# Report\n"
        f"- Job ID: `{report_job_id}`\n"
        f"- run_id: `{'run_new' if report_job_id == 'job_new' else 'run_old'}`\n",
        encoding="utf-8",
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "running",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(job_dir),
                    "selected_inp": str(selected_inp),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(
        allowed_root,
        str(job_dir),
        queue_id="q_new",
    )

    assert bool(payload["run_state_path"]) is expected_state_path
    assert bool(payload["report_json_path"]) is expected_report_paths
    assert bool(payload["report_md_path"]) is expected_report_paths
    if expected_state_path:
        assert payload["run_state_path"] == str(state_path(job_dir).resolve())
    if expected_report_paths:
        assert payload["report_json_path"] == str(report_json_path(job_dir).resolve())
        assert payload["report_md_path"] == str(report_md.resolve())


def test_load_orca_contract_payload_requires_every_queue_generation_identity(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_incomplete_generation"
    job_dir.mkdir(parents=True)
    selected_inp = job_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    incomplete_payload = orca_artifact_payload(
        job_id="job_new",
        run_id="",
        reaction_dir=str(job_dir),
        selected_inp=str(selected_inp),
        status="completed",
    )
    _write_json(state_path(job_dir), incomplete_payload)
    _write_json(report_json_path(job_dir), incomplete_payload)
    (job_dir / "job_report.md").write_text(
        "# Report\n- Job ID: `job_new`\n",
        encoding="utf-8",
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "completed",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(job_dir),
                    "selected_inp": str(selected_inp),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, str(job_dir), queue_id="q_new")

    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""
    assert payload["report_md_path"] == ""


def test_load_orca_contract_payload_requires_markdown_run_identity(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_incomplete_markdown_generation"
    job_dir.mkdir(parents=True)
    selected_inp = job_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    _write_orca_state(
        job_dir,
        job_id="job_new",
        run_id="run_new",
        selected_inp=selected_inp,
        status="completed",
    )
    _write_orca_report(
        job_dir,
        job_id="job_new",
        run_id="run_new",
        selected_inp=selected_inp,
        status="completed",
    )
    (job_dir / "job_report.md").write_text(
        "# Report\n- Job ID: `job_new`\n",
        encoding="utf-8",
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "completed",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(job_dir),
                    "selected_inp": str(selected_inp),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, str(job_dir), queue_id="q_new")

    assert payload["run_state_path"] == str(state_path(job_dir).resolve())
    assert payload["report_json_path"] == str(report_json_path(job_dir).resolve())
    assert payload["report_md_path"] == ""


def test_load_orca_contract_payload_hides_stale_markdown_after_matching_report_json(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_generation"
    job_dir.mkdir(parents=True)
    selected_inp = job_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    _write_orca_state(
        job_dir,
        job_id="job_new",
        run_id="run_new",
        selected_inp=selected_inp,
        status="completed",
    )
    _write_orca_report(
        job_dir,
        job_id="job_new",
        run_id="run_new",
        selected_inp=selected_inp,
        status="completed",
    )
    (job_dir / "job_report.md").write_text(
        "# Old report\n- Job ID: `job_old`\n- run_id: `run_old`\n",
        encoding="utf-8",
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "completed",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(job_dir),
                    "selected_inp": str(selected_inp),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, str(job_dir), queue_id="q_new")

    assert payload["report_json_path"] == str(report_json_path(job_dir).resolve())
    assert payload["report_md_path"] == ""


def test_load_orca_contract_payload_rejects_runtime_artifact_symlink_escape(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "rxn_symlink_escape"
    outside_dir = tmp_path / "outside"
    job_dir.mkdir(parents=True)
    outside_dir.mkdir()
    selected_inp = job_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    matching_payload = orca_artifact_payload(
        job_id="job_new",
        run_id="run_new",
        reaction_dir=str(job_dir),
        selected_inp=str(selected_inp),
        status="completed",
    )
    outside_state = outside_dir / "job_state.json"
    outside_report = outside_dir / "job_report.json"
    outside_markdown = outside_dir / "job_report.md"
    _write_json(outside_state, matching_payload)
    _write_json(outside_report, matching_payload)
    outside_markdown.write_text(
        "# Report\n- Job ID: `job_new`\n- run_id: `run_new`\n",
        encoding="utf-8",
    )
    state_path(job_dir).symlink_to(outside_state)
    report_json_path(job_dir).symlink_to(outside_report)
    (job_dir / "job_report.md").symlink_to(outside_markdown)
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "completed",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(job_dir),
                    "selected_inp": str(selected_inp),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, str(job_dir), queue_id="q_new")

    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""
    assert payload["report_md_path"] == ""


def test_load_orca_contract_payload_binds_runtime_paths_to_payload_directory(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "runs"
    latest_dir = allowed_root / "latest"
    original_dir = allowed_root / "original"
    latest_dir.mkdir(parents=True)
    original_dir.mkdir()
    selected_inp = original_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")

    _write_json(
        state_path(latest_dir),
        {"job_id": "job_new", "run_id": "run_new", "status": "running"},
    )
    _write_json(
        report_json_path(latest_dir),
        {"job_id": "job_new", "run_id": "run_new", "status": "running"},
    )
    (latest_dir / "job_report.md").write_text(
        "# Legacy report\n- Job ID: `job_new`\n- run_id: `run_new`\n",
        encoding="utf-8",
    )
    _write_orca_state(
        original_dir,
        job_id="job_new",
        run_id="run_new",
        selected_inp=selected_inp,
        status="running",
    )
    _write_orca_report(
        original_dir,
        job_id="job_new",
        run_id="run_new",
        selected_inp=selected_inp,
        status="running",
    )
    _write_json(
        allowed_root / "job_locations.json",
        [
            {
                "job_id": "job_new",
                "app_name": "orca_auto_orca",
                "job_type": "orca_opt",
                "status": "running",
                "original_run_dir": str(original_dir),
                "molecule_key": "H",
                "selected_input_xyz": str(selected_inp),
                "latest_known_path": str(latest_dir),
                "resource_request": {},
                "resource_actual": {},
            }
        ],
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "running",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(original_dir),
                    "selected_inp": str(selected_inp),
                },
            }
        ],
    )

    payload = load_orca_contract_payload(allowed_root, "job_new", queue_id="q_new")

    assert payload["state_status"] == "running"
    assert payload["run_id"] == "run_new"
    assert payload["reaction_dir"] == str(latest_dir.resolve())
    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""
    assert payload["report_md_path"] == ""


def test_load_orca_contract_payload_preserves_physical_paths_without_generation_identity(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "legacy_run"
    job_dir.mkdir()
    state_file = state_path(job_dir)
    report_json = report_json_path(job_dir)
    report_md = job_dir / "job_report.md"
    state_file.write_text("legacy state\n", encoding="utf-8")
    report_json.write_text("legacy report\n", encoding="utf-8")
    report_md.write_text("# Legacy report\n", encoding="utf-8")

    payload = load_orca_contract_payload(tmp_path, str(job_dir))

    assert payload["run_state_path"] == str(state_file.resolve())
    assert payload["report_json_path"] == str(report_json.resolve())
    assert payload["report_md_path"] == str(report_md.resolve())


def test_load_orca_contract_payload_uses_single_dependency_resolver() -> None:
    original_deps = _job_location_contracts._job_location_deps
    call_count = 0

    def counting_deps() -> object:
        nonlocal call_count
        call_count += 1
        return original_deps()

    with tempfile.TemporaryDirectory() as td:
        with patch.object(_job_location_contracts, "_job_location_deps", counting_deps):
            assert _job_location_contracts.load_orca_contract_payload(Path(td), "missing") == {}

    assert call_count == 1


def test_job_locations_uses_core_indexing_backend() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = _make_cfg(root)
        allowed_root = Path(cfg.runtime.allowed_root)
        allowed_root.mkdir(parents=True)
        job_dir = allowed_root / "rxn_fallback"
        job_dir.mkdir()
        inp = job_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        record = upsert_job_record(
            cfg,
            job_id="job_core_1",
            status="queued",
            job_dir=job_dir,
            job_type="opt",
            selected_input_xyz=str(inp),
            molecule_key="H2",
            resource_request={"max_cores": 8, "max_memory_gb": 16},
            resource_actual={"max_cores": 8, "max_memory_gb": 16},
        )

        assert record.job_id == "job_core_1"
        assert resolve_latest_job_dir(index_root_for_cfg(cfg), "job_core_1") == job_dir.resolve()

        loaded = _load_job_locations(index_root_for_cfg(cfg))
        assert len(loaded) == 1
        assert loaded[0]["job_id"] == "job_core_1"


def test_collect_reindex_payload_reads_artifact_identity_and_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        job_dir = root / "runs" / "rxn_reindex"
        job_dir.mkdir(parents=True)
        inp = job_dir / "rxn.inp"
        inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        _write_orca_state(
            job_dir,
            job_id="job_reindex_1",
            selected_inp=inp,
            resource_request={"max_cores": 4, "max_memory_gb": 8},
        )
        _write_orca_report(
            job_dir,
            job_id="job_reindex_1",
            selected_inp=inp,
            resource_actual={"max_cores": 4, "max_memory_gb": 8},
            engine_payload_extra={
                "job_type": "single_point",
                "molecule_key": "H2",
            },
        )

        payload = collect_reindex_payload(job_dir)

        assert payload == {
            "job_id": "job_reindex_1",
            "status": "completed",
            "job_type": "orca_single_point",
            "job_dir": str(job_dir.resolve()),
            "selected_input_xyz": str(inp.resolve()),
            "molecule_key": "H2",
            "resource_request": {"max_cores": 4, "max_memory_gb": 8},
            "resource_actual": {"max_cores": 4, "max_memory_gb": 8},
        }


def test_reindex_job_locations_skips_workflow_workspace_jobs() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = _make_cfg(root)
        allowed_root = Path(cfg.runtime.allowed_root)

        standalone = allowed_root / "rxn_standalone"
        standalone.mkdir(parents=True)
        standalone_inp = standalone / "calc.inp"
        standalone_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        _write_orca_state(
            standalone,
            job_id="job_standalone",
            status="completed",
            selected_inp=standalone_inp,
        )

        workspace = allowed_root / "wf_20260704"
        stage_job = workspace / "03_orca" / "candidate_01"
        stage_job.mkdir(parents=True)
        (workspace / "workflow.json").write_text("{}", encoding="utf-8")
        stage_inp = stage_job / "calc.inp"
        stage_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        _write_orca_state(
            stage_job,
            job_id="job_workflow_internal",
            status="completed",
            selected_inp=stage_inp,
        )

        assert reindex_job_locations(cfg) == 1
        loaded = _load_job_locations(index_root_for_cfg(cfg))
        assert [record["job_id"] for record in loaded] == ["job_standalone"]


def test_reindex_excludes_production_smoke_tree_but_case_root_indexes_it(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    allowed_root = Path(cfg.runtime.allowed_root)
    standalone = allowed_root / "standalone"
    case_parent = allowed_root / SMOKE_RESULTS_DIRNAME / "batch" / "case" / "runtime"
    case_parent.mkdir(parents=True)
    case_cfg = _make_cfg(case_parent)
    case_runs_root = Path(case_cfg.runtime.allowed_root)
    smoke_job = case_runs_root / "smoke-job"

    for run_dir, job_id in ((standalone, "job-production"), (smoke_job, "job-smoke")):
        run_dir.mkdir(parents=True)
        selected_inp = run_dir / "calc.inp"
        selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        _write_orca_state(
            run_dir,
            job_id=job_id,
            status="completed",
            selected_inp=selected_inp,
        )

    assert reindex_job_locations(cfg) == 1
    assert [record["job_id"] for record in _load_job_locations(allowed_root)] == ["job-production"]

    assert reindex_job_locations(case_cfg) == 1
    assert [record["job_id"] for record in _load_job_locations(case_runs_root)] == ["job-smoke"]


def test_reindex_skips_job_when_an_artifact_symlink_escapes_runs_root(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    allowed_root = Path(cfg.runtime.allowed_root)
    job_dir = allowed_root / "linked-report"
    outside = tmp_path / "outside"
    job_dir.mkdir(parents=True)
    outside.mkdir()
    selected_inp = job_dir / "calc.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
    _write_orca_state(
        job_dir,
        job_id="job-linked",
        status="completed",
        selected_inp=selected_inp,
    )
    outside_report = outside / "job_report.json"
    _write_json(
        outside_report,
        _orca_payload(
            job_id="job-linked",
            reaction_dir=job_dir,
            selected_inp=selected_inp,
        ),
    )
    report_json_path(job_dir).symlink_to(outside_report)

    assert reindex_job_locations(cfg) == 0
    assert _load_job_locations(allowed_root) == []


def test_reindex_revalidates_candidate_after_directory_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    allowed_root = Path(cfg.runtime.allowed_root)
    normal_job = allowed_root / "normal"
    original_job = allowed_root / "normal-original"
    smoke_job = allowed_root / SMOKE_RESULTS_DIRNAME / "batch" / "smoke"

    for run_dir, job_id in ((normal_job, "job-normal"), (smoke_job, "job-smoke")):
        run_dir.mkdir(parents=True)
        selected_inp = run_dir / "calc.inp"
        selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        _write_orca_state(
            run_dir,
            job_id=job_id,
            status="completed",
            selected_inp=selected_inp,
        )

    original_candidates = _job_location_records._candidate_reindex_dirs

    def _candidates_then_replace(root: Path) -> set[Path]:
        candidates = original_candidates(root)
        normal_job.rename(original_job)
        normal_job.symlink_to(smoke_job, target_is_directory=True)
        return candidates

    monkeypatch.setattr(
        _job_location_records,
        "_candidate_reindex_dirs",
        _candidates_then_replace,
    )

    assert reindex_job_locations(cfg) == 0
    assert _load_job_locations(allowed_root) == []
    assert state_path(original_job).exists()
    assert state_path(smoke_job).exists()


def test_reindex_reads_artifacts_from_discovered_directory_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    allowed_root = Path(cfg.runtime.allowed_root)
    normal_job = allowed_root / "normal-aba"
    temporary_job = allowed_root / "normal-aba-temporary"
    smoke_job = allowed_root / SMOKE_RESULTS_DIRNAME / "batch" / "smoke-aba"

    for run_dir, job_id in ((normal_job, "job-normal"), (smoke_job, "job-smoke")):
        run_dir.mkdir(parents=True)
        selected_inp = run_dir / "calc.inp"
        selected_inp.write_text(
            "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
            encoding="utf-8",
        )
        artifact_kwargs = {
            "job_id": job_id,
            "status": "completed",
            "selected_inp": selected_inp,
        }
        _write_orca_state(run_dir, **artifact_kwargs)
        _write_orca_report(run_dir, **artifact_kwargs)

    smoke_report = json.loads(report_json_path(smoke_job).read_text(encoding="utf-8"))
    smoke_report["job"]["dir"] = ""
    _write_json(report_json_path(smoke_job), smoke_report)

    original_load_report_json = _job_location_records.load_report_json

    def _swap_only_while_report_is_read(artifact_dir: Path) -> dict[str, Any] | None:
        normal_job.rename(temporary_job)
        smoke_job.rename(normal_job)
        try:
            return original_load_report_json(artifact_dir)
        finally:
            normal_job.rename(smoke_job)
            temporary_job.rename(normal_job)

    monkeypatch.setattr(
        _job_location_records,
        "load_report_json",
        _swap_only_while_report_is_read,
    )

    assert reindex_job_locations(cfg) == 1
    assert [record["job_id"] for record in _load_job_locations(allowed_root)] == ["job-normal"]


def test_reindex_job_locations_handles_missing_root_and_skips_unidentifiable_artifacts() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = _make_cfg(root)

        assert reindex_job_locations(cfg) == 0

        allowed_root = Path(cfg.runtime.allowed_root)
        bad_dir = allowed_root / "bad"
        good_dir = allowed_root / "good"
        bad_dir.mkdir(parents=True)
        good_dir.mkdir(parents=True)
        selected_inp = good_dir / "good.inp"
        selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        _write_json(state_path(bad_dir), {"schema_version": 1, "engine": "orca"})
        _write_orca_state(
            good_dir,
            job_id="job_reindex_good",
            status="running",
            selected_inp=selected_inp,
            resource_request={"max_cores": 2, "max_memory_gb": 4},
            engine_payload_extra={
                "job_type": "opt",
                "molecule_key": "H2",
            },
        )

        assert reindex_job_locations(cfg) == 1
        loaded = _load_job_locations(index_root_for_cfg(cfg))
        assert len(loaded) == 1
        assert loaded[0]["job_id"] == "job_reindex_good"
        assert loaded[0]["job_type"] == "orca_opt"
        assert loaded[0]["original_run_dir"] == str(good_dir.resolve())
