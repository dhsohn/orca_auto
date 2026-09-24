from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.indexing import JobLocationRecord, upsert_job_location
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.orca.config import AppConfig, CommonResourceConfig, OrcaRuntimeConfig, PathsConfig
from orca_auto.orca.job_locations import (
    JobLocationRebuildConflict,
    index_root_for_cfg,
    list_job_location_records,
    rebuild_job_location_records,
    record_from_artifacts,
    resolve_record_job_dir,
    upsert_job_record,
)
from orca_auto.orca.job_locations._generation import payload_matches_queue_generation
from orca_auto.orca.machine_observation import machine_json_bytes
from orca_auto.orca.report import publication as orca_publication
from orca_auto.orca.state_reading import (
    REPORT_JSON_NAME,
    report_json_path,
    state_path,
)
from tests.conftest import make_app_cfg, write_fake_orca
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


def test_upsert_job_record_writes_allowed_root_index_and_resolves_latest_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = make_app_cfg(
            root / "runs",
            orca_executable=write_fake_orca(root / "fake_orca"),
            resources=CommonResourceConfig(max_cores_per_task=8, max_memory_gb_per_task=16),
        )
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
        cfg = make_app_cfg(
            root / "runs",
            orca_executable=write_fake_orca(root / "fake_orca"),
            resources=CommonResourceConfig(max_cores_per_task=8, max_memory_gb_per_task=16),
        )
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


def _write_state(job_dir: Path, payload: dict[str, Any]) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    state_path(job_dir).write_text(json.dumps(payload), encoding="utf-8")


def test_rebuild_job_location_records_follows_upsert_identity_rules(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    original = root / "moved-away"
    found = root / "archive" / "moved-here"
    # A run that was organized after it finished: the state still names the
    # directory it ran in, disk says where it lives now, and the existing row
    # carries a molecule key the artifacts never had.
    _write_state(
        found,
        orca_artifact_payload(
            job_id="job-moved",
            run_id="run-moved",
            reaction_dir=str(original),
            status="completed",
            selected_inp=str(original / "calc.inp"),
            resource_request={"max_cores": 4, "max_memory_gb": 8},
        ),
    )
    upsert_job_record(
        AppConfig(
            paths=PathsConfig(),
            runtime=OrcaRuntimeConfig(allowed_root=str(root)),
            resources=CommonResourceConfig(),
        ),
        job_id="job-moved",
        status="running",
        job_dir=original,
        job_type="opt",
        selected_input_xyz="",
        molecule_key="C2H6",
    )
    # report.json outranks job_state.json for the identity and status.
    reported = root / "reported"
    _write_state(
        reported,
        orca_artifact_payload(
            job_id="state-id", run_id="run-r", reaction_dir=str(reported), status="running"
        ),
    )
    report_json_path(reported).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id="report-id", run_id="run-r", reaction_dir=str(reported), status="completed"
            )
        ),
        encoding="utf-8",
    )
    # A state naming neither a job id nor a run id cannot be indexed.
    _write_state(
        root / "anonymous",
        orca_artifact_payload(job_id="", run_id="", reaction_dir=str(root / "anonymous")),
    )
    # A generation subtree is reserved and never walked as a run of its own.
    generation = reported / "20260101-000000-deadbeef"
    _write_state(
        generation,
        orca_artifact_payload(
            job_id="generation-only", run_id="run-g", reaction_dir=str(generation)
        ),
    )

    preview = rebuild_job_location_records(root, apply=False)
    assert preview.applied is False
    assert {row.job_id for row in list_job_location_records(root)} == {"job-moved"}
    assert [row.job_id for row in preview.added] == ["report-id"]
    assert [row.job_id for row in preview.updated] == ["job-moved"]
    assert preview.skipped == (str(root / "anonymous"),)
    assert preview.scanned == 3

    result = rebuild_job_location_records(root, apply=True)
    assert result.applied is True
    rows = {row.job_id: row for row in list_job_location_records(root)}
    assert set(rows) == {"job-moved", "report-id"}
    moved = rows["job-moved"]
    assert moved.status == "completed"
    assert moved.original_run_dir == str(original)
    assert moved.latest_known_path == str(found)
    assert moved.molecule_key == "C2H6"
    assert moved.resource_request == {"max_cores": 4, "max_memory_gb": 8}
    assert rows["report-id"].status == "completed"
    assert rows["report-id"].latest_known_path == str(reported)
    assert rows["report-id"].job_type.startswith("orca_")

    again = rebuild_job_location_records(root, apply=True)
    assert (again.added, again.updated, again.unchanged, again.applied) == ((), (), 2, False)


def _row(root: Path, job_id: str) -> JobLocationRecord:
    return next(row for row in list_job_location_records(root) if row.job_id == job_id)


def test_rebuild_keeps_a_terminal_row_over_a_stale_copy_that_sorts_first(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    moved = root / "z_moved"
    backup = root / "a_backup"
    # The run finished in z_moved; a_backup is a copy taken mid-run that still
    # says running. It sorts first in path order, and its state is even newer.
    _write_state(
        moved,
        orca_artifact_payload(
            job_id="J", run_id="run-j", reaction_dir=str(moved), status="completed"
        ),
    )
    _write_state(
        backup,
        orca_artifact_payload(
            job_id="J", run_id="run-j", reaction_dir=str(backup), status="running"
        ),
    )
    os.utime(state_path(backup), ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    upsert_job_location(
        root,
        JobLocationRecord(
            job_id="J",
            app_name="orca_auto_orca",
            job_type="orca_opt",
            status="completed",
            original_run_dir=str(root / "orig"),
            molecule_key="m",
            latest_known_path=str(moved),
        ),
    )

    result = rebuild_job_location_records(root, apply=True)

    kept = _row(root, "J")
    assert (kept.status, kept.latest_known_path) == ("completed", str(moved))
    assert kept.molecule_key == "m"
    assert [conflict.job_id for conflict in result.conflicts] == ["J"]
    assert result.conflicts[0].kept_path == str(moved)
    assert result.conflicts[0].ignored_paths == (str(backup),)

    # The same holds when the finished directory is gone: the row is never
    # downgraded to the copy's running status, it may only be completed.
    shutil.rmtree(moved)
    result = rebuild_job_location_records(root, apply=True)
    kept = _row(root, "J")
    assert (kept.status, kept.latest_known_path) == ("completed", str(moved))
    assert result.conflicts == (
        JobLocationRebuildConflict(job_id="J", kept_path=str(moved), ignored_paths=(str(backup),)),
    )


def test_rebuild_ranks_unindexed_duplicates_terminal_then_newest_then_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    older = root / "a_older"
    newer = root / "b_newer"
    running = root / "c_running"
    for job_dir, status in ((older, "failed"), (newer, "completed"), (running, "running")):
        _write_state(
            job_dir,
            orca_artifact_payload(
                job_id="D", run_id="run-d", reaction_dir=str(job_dir), status=status
            ),
        )
    os.utime(state_path(older), ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    os.utime(state_path(newer), ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
    os.utime(state_path(running), ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))

    result = rebuild_job_location_records(root, apply=True)

    added = _row(root, "D")
    assert (added.status, added.latest_known_path) == ("completed", str(newer))
    assert result.conflicts == (
        JobLocationRebuildConflict(
            job_id="D", kept_path=str(newer), ignored_paths=(str(older), str(running))
        ),
    )
    # The row now pins b_newer: a later rebuild follows that directory even
    # when another copy is written afterwards.
    os.utime(state_path(older), ns=(3_000_000_000_000_000_000, 3_000_000_000_000_000_000))
    again = rebuild_job_location_records(root, apply=True)
    assert _row(root, "D").latest_known_path == str(newer)
    assert again.conflicts[0].kept_path == str(newer)
    assert again.updated == ()


def test_rebuild_sees_a_terminal_row_written_between_its_plan_and_its_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orca_auto.core.indexing import store as index_store

    root = tmp_path / "runs"
    job_dir = root / "rxn"
    _write_state(
        job_dir,
        orca_artifact_payload(
            job_id="K", run_id="run-k", reaction_dir=str(job_dir), status="running"
        ),
    )
    upsert_job_location(
        root,
        JobLocationRecord(
            job_id="K",
            app_name="orca_auto_orca",
            job_type="orca_opt",
            status="running",
            original_run_dir=str(job_dir),
            latest_known_path=str(job_dir),
        ),
    )
    real_lock = index_store.file_lock
    interleaved: list[str] = []

    @contextmanager
    def lock_after_the_worker_finishes(lock_path: Path, **kwargs: Any) -> Iterator[None]:
        # The rebuild has read the state as running. Before it takes the index
        # lock, the worker finalizes: state file first, then its own upsert.
        if not interleaved:
            interleaved.append("worker")
            _write_state(
                job_dir,
                orca_artifact_payload(
                    job_id="K", run_id="run-k", reaction_dir=str(job_dir), status="completed"
                ),
            )
            monkeypatch.setattr(index_store, "file_lock", real_lock)
            upsert_job_location(root, replace(_row(root, "K"), status="completed"))
            monkeypatch.setattr(index_store, "file_lock", lock_after_the_worker_finishes)
        with real_lock(lock_path, **kwargs):
            yield

    monkeypatch.setattr(index_store, "file_lock", lock_after_the_worker_finishes)
    result = rebuild_job_location_records(root, apply=True)
    monkeypatch.setattr(index_store, "file_lock", real_lock)

    assert interleaved == ["worker"]
    assert _row(root, "K").status == "completed"
    assert result.conflicts == ()
