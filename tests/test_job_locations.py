from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core import engine_process as _engine_process
from orca_auto.core.engine_runner import executable_identity
from orca_auto.core.queue.engine.input_snapshot import (
    bind_direct_generation_owner,
    require_direct_generation_owner,
)
from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RetryRuntimeConfig
from orca_auto.orca.execution_binding import orca_execution_provenance
from orca_auto.orca.job_locations import _contract_payload as _job_location_payload
from orca_auto.orca.job_locations import _runtime_context as _job_location_runtime
from orca_auto.orca.job_locations import (
    index_root_for_cfg,
    load_job_artifact_context,
    load_job_artifacts,
    load_job_runtime_context,
    load_orca_contract_payload,
    record_from_artifacts,
    resolve_latest_job_dir,
    upsert_job_record,
)
from orca_auto.orca.job_locations._generation import payload_matches_queue_generation
from orca_auto.orca.state import REPORT_JSON_NAME, STATE_FILE_NAME, report_json_path, state_path
from tests.engine_artifact_helpers import orca_artifact_payload


def _runtime_paths(generation: Path) -> dict[str, str]:
    return _job_location_payload.runtime_paths(
        generation,
        state_file_name=STATE_FILE_NAME,
        report_json_name=REPORT_JSON_NAME,
        queue_entry=None,
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "job_id": "job_1",
                "run_id": "run_1",
                "execution_provenance": {
                    "execution_dir": "/runs/job/20260720-120000-a1b2c3d4",
                    "execution_dir_identity": {"device": 1, "inode": 2},
                    "generation_owner_token": "owner-token",
                    "bound_selected_identity": {"path": "/runs/job/generation/job.inp"},
                },
            },
            True,
        ),
        ({"job_id": "job_1", "run_id": "run_1"}, False),
        ({"job_id": "job_1"}, False),
        ({"run_id": "run_1"}, False),
        ({}, False),
    ],
)
def test_queue_absent_payload_requires_job_and_run_identity(
    payload: dict[str, str], expected: bool
) -> None:
    assert payload_matches_queue_generation(None, payload) is expected


@pytest.mark.parametrize("selector", ("queue_id", "target", "reaction_dir"))
def test_contract_queue_lookup_ignores_partial_and_foreign_rows(
    tmp_path: Path,
    selector: str,
) -> None:
    allowed_root = tmp_path / "runs"
    reaction_dir = allowed_root / "shared"
    reaction_dir.mkdir(parents=True)
    (allowed_root / "queue.json").write_text(
        json.dumps(
            [
                {
                    "queue_id": "q_partial",
                    "app_name": "orca_auto_orca",
                    "engine": "orca",
                    "task_kind": "orca_run_inp",
                    "status": "completed",
                    "metadata": {"reaction_dir": str(reaction_dir)},
                },
                {
                    "queue_id": "q_foreign",
                    "app_name": "orca_auto_crest",
                    "engine": "crest",
                    "task_kind": "crest_conformer_search",
                    "task_id": "foreign-task",
                    "status": "completed",
                    "metadata": {"reaction_dir": str(reaction_dir)},
                },
            ]
        ),
        encoding="utf-8",
    )
    target = "unused"
    queue_id = ""
    selected_reaction_dir = ""
    if selector == "queue_id":
        queue_id = "q_partial"
    elif selector == "target":
        target = "q_foreign"
    else:
        selected_reaction_dir = str(reaction_dir)

    assert (
        _job_location_runtime._find_queue_entry(
            index_root=allowed_root,
            target=target,
            queue_id=queue_id,
            run_id="",
            reaction_dir=selected_reaction_dir,
        )
        is None
    )


def _load_job_locations(root: Path) -> list[dict[str, object]]:
    path = root / "job_locations.json"
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else []


def _write_json(path: Path, payload: object) -> None:
    if path.name == "queue.json" and isinstance(payload, list):
        normalized: list[object] = []
        for item in payload:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            row = dict(item)
            row.setdefault("app_name", "orca_auto_orca")
            row.setdefault("engine", "orca")
            row.setdefault("task_kind", "orca_run_inp")
            normalized.append(row)
        payload = normalized
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
        runtime=RetryRuntimeConfig(
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


def _verified_queue_absent_report_generation(tmp_path: Path) -> tuple[Path, Path]:
    allowed_root = tmp_path / "runs"
    job_dir = allowed_root / "verified-report"
    generation = job_dir / "20260714-224054-98989898"
    generation.mkdir(parents=True)
    _write_generation_payloads(
        job_dir=job_dir,
        generation_dir=generation,
        job_id="job_verified_report",
        run_id="run_verified_report",
    )
    _execution_snapshot_locator(job_dir, generation)
    return allowed_root, generation


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
        assert loaded_report is None


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
            assert loaded_report is None


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
        assert loaded_report is None


def test_root_report_identity_is_not_a_job_location_lookup_source() -> None:
    with tempfile.TemporaryDirectory() as td:
        allowed_root = Path(td) / "runs"
        job_dir = allowed_root / "report-only-lookup"
        job_dir.mkdir(parents=True)
        selected_inp = job_dir / "calc.inp"
        selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
        _write_orca_report(
            job_dir,
            job_id="report-only-job",
            run_id="report-only-run",
            selected_inp=selected_inp,
        )
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "indexed-job",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_sp",
                    "status": "completed",
                    "original_run_dir": str(job_dir),
                    "molecule_key": "H",
                    "selected_input_xyz": str(selected_inp),
                    "latest_known_path": str(job_dir),
                    "resource_request": {},
                    "resource_actual": {},
                }
            ],
        )

        assert resolve_latest_job_dir(allowed_root, "report-only-job") is None
        assert resolve_latest_job_dir(allowed_root, "report-only-run") is None


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
        assert context.report is None


def test_load_job_runtime_context_keeps_queue_but_ignores_root_only_artifacts() -> None:
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
        assert context.artifact.state is None
        assert context.artifact.report is None
        assert state_path(job_dir).is_file()
        assert report_json_path(job_dir).is_file()


def test_root_only_payload_is_not_consumed() -> None:
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

        assert payload["status"] == "unknown"
        assert payload["reason"] == "queue_generation_verification_failed"
        assert payload["optimized_xyz_path"] == ""
        assert payload["last_out_path"] == ""
        assert payload["attempt_count"] == 0
        assert payload["attempts"] == ()
        assert payload["run_state_path"] == ""
        assert payload["report_json_path"] == ""


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
        _write_json(state_path(generation), payload)
        _write_json(report_json_path(generation), payload)

    publish(provenance)
    valid = load_orca_contract_payload(allowed_root, str(generation))
    from orca_auto.flow.adapters.orca import load_orca_artifact_contract

    with patch(
        "orca_auto.flow.adapters.orca._orca_tracking.load_orca_contract_payload_impl",
        return_value=None,
    ):
        fallback = load_orca_artifact_contract(
            target=str(generation),
            orca_allowed_root=allowed_root,
        )

    assert valid["status"] == "completed"
    assert valid["selected_inp"] == str(selected_inp.resolve())
    assert valid["optimized_xyz_path"] == str(calculated_xyz.resolve())
    assert valid["run_state_path"] == str(state_path(generation).resolve())
    assert valid["report_json_path"] == str(report_json_path(generation).resolve())
    assert fallback.run_state_path == str(state_path(generation).resolve())
    assert fallback.report_json_path == str(report_json_path(generation).resolve())

    report_json_path(generation).unlink()
    without_report = load_orca_contract_payload(allowed_root, str(generation))
    assert without_report["report_json_path"] == ""
    publish(provenance)

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

    replacement = load_orca_contract_payload(allowed_root, str(generation))
    replacement_paths = _runtime_paths(generation)

    assert replacement["status"] == "unknown"
    assert replacement["reason"] == "queue_generation_verification_failed"
    assert replacement["selected_inp"] == ""
    assert replacement["optimized_xyz_path"] == ""
    assert replacement["last_out_path"] == ""
    assert replacement_paths == {
        "run_state_path": "",
        "report_json_path": "",
    }


def test_runtime_paths_reject_mismatched_disk_payload_generations(tmp_path: Path) -> None:
    _allowed_root, generation = _verified_queue_absent_report_generation(tmp_path)
    report_payload = json.loads(report_json_path(generation).read_text(encoding="utf-8"))
    assert isinstance(report_payload, dict)
    report_job = report_payload["job"]
    report_engine_payload = report_payload["engine_payload"]
    assert isinstance(report_job, dict)
    assert isinstance(report_engine_payload, dict)
    report_job["id"] = "job_other"
    report_job["task_id"] = "job_other"
    report_engine_payload["run_id"] = "run_other"
    _write_json(report_json_path(generation), report_payload)

    paths = _runtime_paths(generation)

    assert paths == {
        "run_state_path": "",
        "report_json_path": "",
    }


def test_runtime_paths_reject_replacement_between_payload_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allowed_root, generation = _verified_queue_absent_report_generation(tmp_path)
    state_file = state_path(generation)
    report_file = report_json_path(generation)
    replacement = generation / "replacement-state.json"
    replacement.write_bytes(state_file.read_bytes())
    report_inode = report_file.stat().st_ino
    original_read = _engine_process.os.read
    replaced = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not replaced and chunk and os.fstat(descriptor).st_ino == report_inode:
            replacement.replace(state_file)
            replaced = True
        return chunk

    monkeypatch.setattr(_engine_process.os, "read", replacing_read)

    paths = _runtime_paths(generation)

    assert replaced
    assert paths == {
        "run_state_path": "",
        "report_json_path": "",
    }


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

    for payload in (by_queue, by_job_path):
        assert payload["status"] == "unknown"
        assert payload["reason"] == "queue_generation_verification_failed"
        assert payload["optimized_xyz_path"] == ""
        assert payload["last_out_path"] == ""

    assert without_queue["status"] == "unknown"
    assert without_queue["reason"] == "queue_generation_verification_failed"
    assert without_queue["optimized_xyz_path"] == ""
    assert without_queue["last_out_path"] == ""


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
    ("state_job_id", "report_job_id"),
    [
        ("job_old", "job_new"),
        ("job_new", "job_old"),
    ],
)
def test_load_orca_contract_payload_never_exposes_root_runtime_paths(
    tmp_path: Path,
    state_job_id: str,
    report_job_id: str,
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

    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""
    assert state_path(job_dir).is_file()
    assert report_json_path(job_dir).is_file()


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


def test_load_orca_contract_payload_hides_matching_root_payloads_without_generation(
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
    _write_json(outside_state, matching_payload)
    _write_json(outside_report, matching_payload)
    state_path(job_dir).symlink_to(outside_state)
    report_json_path(job_dir).symlink_to(outside_report)
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

    assert payload["state_status"] == ""
    assert payload["run_id"] == "run_new"
    assert payload["reaction_dir"] == str(latest_dir.resolve())
    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""


def test_load_orca_contract_payload_rejects_physical_paths_without_generation_identity(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "unbound_run"
    job_dir.mkdir()
    state_file = state_path(job_dir)
    report_json = report_json_path(job_dir)
    state_file.write_text("unbound state\n", encoding="utf-8")
    report_json.write_text("unbound report\n", encoding="utf-8")

    payload = load_orca_contract_payload(tmp_path, str(job_dir))

    assert payload["run_state_path"] == ""
    assert payload["report_json_path"] == ""


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
