from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RuntimeConfig
from orca_auto.orca.job_locations import _contracts as _job_location_contracts
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
            organized_root=str(root / "outputs"),
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


def test_record_from_artifacts_uses_run_id_fallback_and_organized_ref() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original_dir = root / "runs" / "rxn_b"
        original_dir.mkdir(parents=True)
        organized_dir = root / "outputs" / "opt" / "H2" / "run_hist_1"
        organized_dir.mkdir(parents=True)
        selected_inp = organized_dir / "rxn.inp"
        selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        state: dict[str, object] = {
            "run_id": "run_hist_1",
            "status": "completed",
            "selected_inp": str(selected_inp),
            "attempts": [],
            "final_result": None,
        }
        organized_ref = {
            "run_id": "run_hist_1",
            "original_run_dir": str(original_dir),
            "organized_output_dir": str(organized_dir),
            "status": "completed",
            "job_type": "opt",
            "selected_inp": str(selected_inp),
            "molecule_key": "H2",
            "resource_request": {"max_cores": 8, "max_memory_gb": 16},
            "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
        }

        record = record_from_artifacts(
            job_dir=original_dir,
            state=state,
            report=None,
            organized_ref=organized_ref,
        )

        assert record is not None
        assert record.job_id == "run_hist_1"
        assert record.original_run_dir == str(original_dir.resolve())
        assert record.organized_output_dir == str(organized_dir.resolve())
        assert record.latest_known_path == str(organized_dir.resolve())
        assert record.job_type == "orca_opt"
        assert record.molecule_key == "H2"


def test_resolve_latest_job_dir_and_load_job_artifacts_cover_job_and_path_targets() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        organized_dir = root / "outputs" / "opt" / "H2" / "job_hist_1"
        original_dir = allowed_root / "rxn_hist_1"
        allowed_root.mkdir()
        original_dir.mkdir()
        organized_dir.mkdir(parents=True)

        inp = organized_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        organized_ref = {
            "job_id": "job_hist_1",
            "run_id": "run_hist_1",
            "original_run_dir": str(original_dir),
            "organized_output_dir": str(organized_dir),
            "selected_inp": str(inp),
            "selected_input_xyz": str(inp),
        }

        _write_orca_state(
            organized_dir,
            job_id="job_hist_1",
            run_id="run_hist_1",
            selected_inp=inp,
        )
        _write_orca_report(
            organized_dir,
            job_id="job_hist_1",
            run_id="run_hist_1",
            selected_inp=inp,
        )
        _write_json(original_dir / "organized_ref.json", organized_ref)
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "job_hist_1",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(original_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "organized_output_dir": str(organized_dir),
                    "latest_known_path": str(organized_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        for target in ("job_hist_1", "run_hist_1", str(original_dir), str(organized_dir)):
            assert resolve_latest_job_dir(allowed_root, target) == organized_dir.resolve()
            job_path, loaded_state, loaded_report = load_job_artifacts(allowed_root, target)
            assert job_path == organized_dir.resolve()
            assert loaded_state is not None and loaded_state["run_id"] == "run_hist_1"
            assert loaded_report is not None and loaded_report["job"]["id"] == "job_hist_1"


def test_load_job_artifacts_follows_organized_ref_when_index_lookup_is_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        original_dir = allowed_root / "rxn_hist_2"
        organized_dir = root / "outputs" / "opt" / "H2" / "job_hist_2"
        allowed_root.mkdir()
        original_dir.mkdir()
        organized_dir.mkdir(parents=True)

        organized_ref = {
            "job_id": "job_hist_2",
            "run_id": "run_hist_2",
            "original_run_dir": str(original_dir),
            "organized_output_dir": str(organized_dir),
        }

        _write_orca_state(
            organized_dir,
            job_id="job_hist_2",
            run_id="run_hist_2",
            selected_inp=organized_dir / "rxn.inp",
        )
        _write_orca_report(
            organized_dir,
            job_id="job_hist_2",
            run_id="run_hist_2",
            selected_inp=organized_dir / "rxn.inp",
        )
        _write_json(original_dir / "organized_ref.json", organized_ref)

        assert resolve_latest_job_dir(allowed_root, str(original_dir)) == organized_dir.resolve()
        job_path, loaded_state, loaded_report = load_job_artifacts(allowed_root, str(original_dir))
        assert job_path == organized_dir.resolve()
        assert loaded_state is not None and loaded_state["job_id"] == "job_hist_2"
        assert (
            loaded_report is not None and loaded_report["engine_payload"]["run_id"] == "run_hist_2"
        )


def test_load_job_artifact_context_includes_record_and_original_stub_for_run_id_target() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        original_dir = allowed_root / "rxn_hist_3"
        organized_dir = root / "outputs" / "opt" / "H2" / "job_hist_3"
        allowed_root.mkdir()
        original_dir.mkdir()
        organized_dir.mkdir(parents=True)

        inp = organized_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")
        organized_ref = {
            "job_id": "job_hist_3",
            "run_id": "run_hist_3",
            "original_run_dir": str(original_dir),
            "organized_output_dir": str(organized_dir),
            "selected_inp": str(inp),
        }

        _write_orca_state(
            organized_dir,
            job_id="job_hist_3",
            run_id="run_hist_3",
            selected_inp=inp,
        )
        _write_orca_report(
            organized_dir,
            job_id="job_hist_3",
            run_id="run_hist_3",
            selected_inp=inp,
        )
        _write_json(original_dir / "organized_ref.json", organized_ref)
        _write_json(
            allowed_root / "job_locations.json",
            [
                {
                    "job_id": "job_hist_3",
                    "app_name": "orca_auto_orca",
                    "job_type": "orca_opt",
                    "status": "completed",
                    "original_run_dir": str(original_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "organized_output_dir": str(organized_dir),
                    "latest_known_path": str(organized_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        context = load_job_artifact_context(allowed_root, "run_hist_3")

        assert context.record is not None
        assert context.record.job_id == "job_hist_3"
        assert context.job_dir == organized_dir.resolve()
        assert context.state is not None and context.state["run_id"] == "run_hist_3"
        assert context.report is not None and context.report["job"]["id"] == "job_hist_3"
        assert context.organized_ref is not None
        assert context.organized_ref["original_run_dir"] == str(original_dir)


def test_load_job_runtime_context_exposes_queue_entry_and_organized_refresh() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        organized_root = root / "outputs"
        original_dir = allowed_root / "rxn_hist_4"
        organized_dir = organized_root / "opt" / "H2" / "job_hist_4"
        allowed_root.mkdir()
        original_dir.mkdir()
        organized_dir.mkdir(parents=True)

        inp = organized_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
        _write_orca_state(
            organized_dir,
            job_id="job_hist_4",
            run_id="run_hist_4",
            selected_inp=inp,
        )
        _write_orca_report(
            organized_dir,
            job_id="job_hist_4",
            run_id="run_hist_4",
            selected_inp=inp,
        )
        _write_json(
            original_dir / "organized_ref.json",
            {
                "job_id": "job_hist_4",
                "run_id": "run_hist_4",
                "original_run_dir": str(original_dir),
                "organized_output_dir": str(organized_dir),
                "selected_inp": str(inp),
            },
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
                        "reaction_dir": str(original_dir),
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
                    "original_run_dir": str(original_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "organized_output_dir": str(organized_dir),
                    "latest_known_path": str(organized_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        context = load_job_runtime_context(
            allowed_root,
            "job_hist_4",
            organized_root=organized_root,
        )

        assert context.queue_entry is not None
        assert context.queue_entry["queue_id"] == "q_hist_4"
        assert context.organized_dir == organized_dir.resolve()
        assert context.artifact.job_dir == organized_dir.resolve()
        assert (
            context.artifact.state is not None and context.artifact.state["run_id"] == "run_hist_4"
        )
        assert (
            context.artifact.report is not None
            and context.artifact.report["job"]["id"] == "job_hist_4"
        )
        assert context.artifact.organized_ref is not None
        assert context.artifact.organized_ref["organized_output_dir"] == str(organized_dir)


def test_load_orca_contract_payload_returns_normalized_runtime_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        allowed_root = root / "runs"
        organized_root = root / "outputs"
        original_dir = allowed_root / "rxn_hist_5"
        organized_dir = organized_root / "opt" / "H2" / "job_hist_5"
        allowed_root.mkdir()
        original_dir.mkdir()
        organized_dir.mkdir(parents=True)

        inp = organized_dir / "rxn.inp"
        inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
        xyz = organized_dir / "rxn.xyz"
        xyz.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
        out = organized_dir / "rxn.out"
        out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
        attempts = [
            {
                "index": 2,
                "inp_path": str(inp),
                "out_path": str(out),
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "normal_termination",
                "markers": [],
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
            organized_dir,
            job_id="job_hist_5",
            run_id="run_hist_5",
            selected_inp=inp,
            selected_xyz_path=xyz,
            attempts=attempts,
            final_result=final_result,
            max_retries=3,
        )
        _write_orca_report(
            organized_dir,
            job_id="job_hist_5",
            run_id="run_hist_5",
            selected_inp=inp,
            selected_xyz_path=xyz,
            attempts=attempts,
            final_result=final_result,
            max_retries=3,
        )
        _write_json(
            original_dir / "organized_ref.json",
            {
                "job_id": "job_hist_5",
                "run_id": "run_hist_5",
                "original_run_dir": str(original_dir),
                "organized_output_dir": str(organized_dir),
                "selected_inp": str(inp),
                "selected_input_xyz": str(xyz),
            },
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
                        "reaction_dir": str(original_dir),
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
                    "original_run_dir": str(original_dir),
                    "molecule_key": "H2",
                    "selected_input_xyz": str(inp),
                    "organized_output_dir": str(organized_dir),
                    "latest_known_path": str(organized_dir),
                    "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                    "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
                }
            ],
        )

        payload = load_orca_contract_payload(
            allowed_root,
            "job_hist_5",
            organized_root=organized_root,
        )

        assert payload["run_id"] == "run_hist_5"
        assert payload["status"] == "completed"
        assert payload["reason"] == "normal_termination"
        assert payload["reaction_dir"] == str(organized_dir.resolve())
        assert payload["latest_known_path"] == str(organized_dir.resolve())
        assert payload["organized_output_dir"] == str(organized_dir.resolve())
        assert payload["queue_id"] == "q_hist_5"
        assert payload["queue_status"] == "completed"
        assert payload["selected_inp"] == str(inp.resolve())
        assert payload["selected_input_xyz"] == str(xyz.resolve())
        assert payload["optimized_xyz_path"] == str(xyz.resolve())
        assert payload["last_out_path"] == str(out.resolve())
        assert payload["attempt_count"] == 1
        assert payload["max_retries"] == 3
        assert payload["resource_request"] == {"max_cores": 8, "max_memory_gb": 16}


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
        original_dir = root / "runs" / "rxn_reindex"
        organized_dir = root / "outputs" / "sp" / "H2" / "job_reindex_1"
        original_dir.mkdir(parents=True)
        organized_dir.mkdir(parents=True)
        inp = original_dir / "rxn.inp"
        inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

        _write_orca_state(
            original_dir,
            job_id="job_reindex_1",
            selected_inp=inp,
            resource_request={"max_cores": 4, "max_memory_gb": 8},
        )
        _write_orca_report(
            original_dir,
            job_id="job_reindex_1",
            selected_inp=inp,
            resource_actual={"max_cores": 4, "max_memory_gb": 8},
            engine_payload_extra={
                "job_type": "single_point",
                "molecule_key": "H2",
            },
        )
        _write_json(
            original_dir / "organized_ref.json",
            {
                "original_run_dir": str(original_dir),
                "organized_output_dir": str(organized_dir),
            },
        )

        payload = collect_reindex_payload(original_dir)

        assert payload == {
            "job_id": "job_reindex_1",
            "status": "completed",
            "job_type": "orca_single_point",
            "job_dir": str(original_dir.resolve()),
            "selected_input_xyz": str(inp.resolve()),
            "molecule_key": "H2",
            "organized_output_dir": str(organized_dir.resolve()),
            "resource_request": {"max_cores": 4, "max_memory_gb": 8},
            "resource_actual": {"max_cores": 4, "max_memory_gb": 8},
        }


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
