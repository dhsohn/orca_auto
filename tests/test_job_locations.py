from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RuntimeConfig
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
