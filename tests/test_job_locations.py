from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.machine_observation import machine_json_bytes
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.orca.config import AppConfig, CommonResourceConfig, OrcaRuntimeConfig, PathsConfig
from orca_auto.orca.job_locations import (
    index_root_for_cfg,
    list_job_location_records,
    record_from_artifacts,
    resolve_record_job_dir,
    upsert_job_record,
)
from orca_auto.orca.job_locations._generation import payload_matches_queue_generation
from orca_auto.orca.report import publication as orca_publication
from orca_auto.orca.state_reading import (
    REPORT_JSON_NAME,
    report_json_path,
    state_path,
)
from tests.engine_artifact_helpers import orca_artifact_payload


def _indexed_job_dir(index_root: Path, job_id: str) -> Path | None:
    for record in list_job_location_records(index_root):
        if record.job_id == job_id:
            return resolve_record_job_dir(record)
    return None


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
    if (
        path.name == REPORT_JSON_NAME
        and is_visible_generation_name(path.parent.name)
        and isinstance(payload, dict)
        and payload.get("contract") is None
    ):
        payload = orca_publication._machine_observation(path.parent, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == REPORT_JSON_NAME and isinstance(payload, dict) and payload.get("contract"):
        path.write_bytes(machine_json_bytes(payload))
    else:
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
        runtime=OrcaRuntimeConfig(
            allowed_root=str(root / "runs"),
        ),
        paths=PathsConfig(orca_executable=str(fake_orca)),
        resources=CommonResourceConfig(max_cores_per_task=8, max_memory_gb_per_task=16),
    )


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
        assert _indexed_job_dir(index_root_for_cfg(cfg), "job_live_1") == job_dir.resolve()
        loaded = _load_job_locations(index_root_for_cfg(cfg))
        assert len(loaded) == 1
        assert loaded[0]["job_id"] == "job_live_1"
        assert loaded[0]["original_run_dir"] == str(job_dir.resolve())


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
        assert _indexed_job_dir(index_root_for_cfg(cfg), "job_core_1") == job_dir.resolve()

        loaded = _load_job_locations(index_root_for_cfg(cfg))
        assert len(loaded) == 1
        assert loaded[0]["job_id"] == "job_core_1"
