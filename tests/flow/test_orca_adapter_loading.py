from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.flow.adapters import _orca_local_lookup, _orca_tracking
from orca_auto.flow.adapters import orca as orca_adapter
from tests.engine_artifact_helpers import orca_artifact_payload


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _disable_tracking_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_orca_tracking, "load_orca_contract_payload_impl", lambda **kwargs: None)
    monkeypatch.setattr(_orca_tracking, "tracked_runtime_context_impl", lambda **kwargs: None)
    monkeypatch.setattr(
        _orca_tracking,
        "tracked_artifact_context_impl",
        lambda **kwargs: (None, None, {}, {}),
    )


def test_load_orca_artifact_contract_short_circuits_on_tracked_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "run_id": " run_payload_1 ",
        "status": " completed ",
        "reason": " normal_termination ",
        "state_status": " completed ",
        "reaction_dir": f" {tmp_path / 'rxn_payload'} ",
        "latest_known_path": f" {tmp_path / 'outputs' / 'run_payload_1'} ",
        "optimized_xyz_path": f" {tmp_path / 'outputs' / 'run_payload_1' / 'final.xyz'} ",
        "queue_id": " q_payload_1 ",
        "queue_status": " COMPLETED ",
        "cancel_requested": "yes",
        "selected_inp": f" {tmp_path / 'outputs' / 'run_payload_1' / 'rxn.inp'} ",
        "selected_input_xyz": f" {tmp_path / 'outputs' / 'run_payload_1' / 'rxn.xyz'} ",
        "analyzer_status": " completed ",
        "completed_at": " 2026-04-19T00:10:00+00:00 ",
        "last_out_path": f" {tmp_path / 'outputs' / 'run_payload_1' / 'rxn.out'} ",
        "run_state_path": f" {tmp_path / 'outputs' / 'run_payload_1' / 'job_state.json'} ",
        "report_json_path": f" {tmp_path / 'outputs' / 'run_payload_1' / 'job_report.json'} ",
        "report_md_path": f" {tmp_path / 'outputs' / 'run_payload_1' / 'job_report.md'} ",
        "attempt_count": "2",
        "max_retries": "3",
        "attempts": [{"attempt_number": 1, "analyzer_status": "completed"}, "skip"],
        "final_result": {"reason": "normal_termination"},
        "resource_request": {"max_cores": "8", "max_memory_gb": "16"},
        "resource_actual": {"max_cores": "6", "max_memory_gb": "12"},
    }

    monkeypatch.setattr(_orca_tracking, "load_orca_contract_payload_impl", lambda **kwargs: payload)
    monkeypatch.setattr(
        _orca_tracking,
        "tracked_runtime_context_impl",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("tracked runtime fallback should not run")
        ),
    )
    monkeypatch.setattr(
        _orca_local_lookup,
        "resolve_job_dir_impl",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("job-location fallback should not run")
        ),
    )

    contract = orca_adapter.load_orca_artifact_contract(
        target="job_payload_1",
        orca_allowed_root=tmp_path / "orca_runs",
    )

    assert contract.run_id == "run_payload_1"
    assert contract.status == "completed"
    assert contract.queue_status == "completed"
    assert contract.cancel_requested is True
    assert contract.attempt_count == 2
    assert contract.max_retries == 3
    assert contract.attempts == ({"attempt_number": 1, "analyzer_status": "completed"},)
    assert contract.resource_request == {"max_cores": 8, "max_memory_gb": 16}
    assert contract.resource_actual == {"max_cores": 6, "max_memory_gb": 12}


def test_tracked_contract_payload_returns_indexed_job_location_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "run_id": "run_job_location_helper",
        "status": "completed",
        "reaction_dir": str(tmp_path / "rxn"),
    }
    monkeypatch.setattr(
        _orca_tracking,
        "load_orca_contract_payload",
        lambda *_args, **_kwargs: payload,
    )

    assert (
        _orca_tracking.load_orca_contract_payload_impl(
            index_root=tmp_path / "orca_runs",
            target="job_location_helper",
            queue_id="",
            run_id="",
            reaction_dir="",
        )
        == payload
    )


def test_tracked_contract_payload_returns_none_for_missing_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _orca_tracking,
        "load_orca_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    assert (
        _orca_tracking.load_orca_contract_payload_impl(
            index_root=tmp_path / "orca_runs",
            target="missing_job",
            queue_id="",
            run_id="",
            reaction_dir="",
        )
        is None
    )


def test_tracked_contract_payload_propagates_corrupt_payload_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _orca_tracking,
        "load_orca_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("corrupt payload")),
    )

    with pytest.raises(ValueError, match="corrupt payload"):
        _orca_tracking.load_orca_contract_payload_impl(
            index_root=tmp_path / "orca_runs",
            target="corrupt_job",
            queue_id="",
            run_id="",
            reaction_dir="",
        )


def test_tracked_runtime_context_propagates_corrupt_runtime_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _orca_tracking,
        "load_job_runtime_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("corrupt runtime")),
    )

    with pytest.raises(ValueError, match="corrupt runtime"):
        _orca_tracking.tracked_runtime_context_impl(
            index_root=tmp_path / "orca_runs",
            target="corrupt_runtime",
            queue_id="",
            run_id="",
            reaction_dir="",
        )


def test_tracked_artifact_context_propagates_corrupt_context_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _orca_tracking,
        "load_job_artifact_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("corrupt context")),
    )

    with pytest.raises(ValueError, match="corrupt context"):
        _orca_tracking.tracked_artifact_context_impl(
            index_root=tmp_path / "orca_runs",
            targets=("corrupt_context",),
        )


def test_load_orca_artifact_contract_uses_tracked_record_job_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    job_dir = allowed_root / "opt" / "H2" / "run_tracked_output"
    job_dir.mkdir(parents=True)

    inp = job_dir / "rxn.inp"
    xyz = job_dir / "rxn.xyz"
    out = job_dir / "rxn.out"
    inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
    xyz.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    _write_json(
        job_dir / "job_state.json",
        orca_artifact_payload(
            job_id="run_tracked_output",
            run_id="run_tracked_output",
            reaction_dir=str(job_dir),
            selected_inp=str(inp),
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-04-19T00:20:00+00:00",
                "last_out_path": str(out),
            },
        ),
    )
    _write_json(
        job_dir / "job_report.json",
        orca_artifact_payload(
            job_id="run_tracked_output",
            run_id="run_tracked_output",
            reaction_dir=str(job_dir),
            selected_inp=str(inp),
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-04-19T00:20:00+00:00",
                "last_out_path": str(out),
            },
        ),
    )
    tracked_record = SimpleNamespace(
        app_name="orca_auto_orca",
        status="completed",
        selected_input_xyz=str(inp),
        latest_known_path=str(job_dir),
        original_run_dir=str(job_dir),
        resource_request={},
        resource_actual={},
    )

    monkeypatch.setattr(_orca_tracking, "load_orca_contract_payload_impl", lambda **kwargs: None)
    monkeypatch.setattr(_orca_tracking, "tracked_runtime_context_impl", lambda **kwargs: None)
    monkeypatch.setattr(
        _orca_tracking,
        "tracked_artifact_context_impl",
        lambda **kwargs: (job_dir, tracked_record, {}, {}),
    )
    monkeypatch.setattr(
        _orca_local_lookup,
        "resolve_job_dir_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("job-dir fallback should not run")
        ),
    )
    monkeypatch.setattr(
        _orca_local_lookup,
        "find_queue_entry_impl",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("queue fallback should not run")),
    )

    contract = orca_adapter.load_orca_artifact_contract(
        target="job_tracked_output",
        orca_allowed_root=allowed_root,
    )

    assert contract.status == "completed"
    assert contract.queue_id == ""
    assert contract.queue_status == ""
    assert contract.run_id == "run_tracked_output"
    assert contract.reaction_dir == str(job_dir.resolve())
    assert contract.latest_known_path == str(job_dir.resolve())
    assert contract.run_state_path == str((job_dir / "job_state.json").resolve())
    assert contract.report_json_path == str((job_dir / "job_report.json").resolve())
    assert contract.selected_inp == str(inp.resolve())
    assert contract.selected_input_xyz == str(xyz.resolve())


def test_load_orca_artifact_contract_resolves_selected_input_and_prefers_last_out_xyz(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    run_dir = allowed_root / "rxn_paths"
    run_dir.mkdir(parents=True)

    inp = run_dir / "job_step.inp"
    source_xyz = run_dir / "source.xyz"
    final_out = run_dir / "final.out"
    final_xyz = run_dir / "final.xyz"
    inp.write_text("! Opt\n* xyzfile 0 1 source.xyz\n", encoding="utf-8")
    source_xyz.write_text("2\nsource\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    final_out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")
    final_xyz.write_text("2\noptimized\nH 0 0 0\nH 0 0 0.75\n", encoding="utf-8")

    _write_json(
        run_dir / "job_state.json",
        orca_artifact_payload(
            job_id="run_paths_1",
            run_id="run_paths_1",
            reaction_dir=str(run_dir),
            selected_inp="job_step.inp",
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "last_out_path": "final.out",
            },
        ),
    )

    _disable_tracking_helpers(monkeypatch)
    monkeypatch.setattr(
        _orca_local_lookup,
        "resolve_job_dir_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("job-dir fallback should not run")
        ),
    )

    contract = orca_adapter.load_orca_artifact_contract(
        target=str(run_dir),
        orca_allowed_root=allowed_root,
    )

    assert contract.selected_inp == str(inp.resolve())
    assert contract.selected_input_xyz == str(source_xyz.resolve())
    assert contract.last_out_path == str(final_out.resolve())
    assert contract.optimized_xyz_path == str(final_xyz.resolve())


@pytest.mark.parametrize(
    (
        "queue_request",
        "queue_actual",
        "record_request",
        "record_actual",
        "expected_request",
        "expected_actual",
    ),
    [
        (
            {"max_cores": "8", "max_memory_gb": "16"},
            None,
            {"max_cores": 4},
            {"max_cores": "6", "max_memory_gb": "12"},
            {"max_cores": 8, "max_memory_gb": 16},
            {"max_cores": 6, "max_memory_gb": 12},
        ),
        (
            {"max_cores": "10"},
            None,
            {},
            {},
            {"max_cores": 10},
            {"max_cores": 10},
        ),
    ],
)
def test_load_orca_artifact_contract_propagates_resource_request_and_actual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    queue_request: dict[str, object],
    queue_actual: dict[str, object] | None,
    record_request: dict[str, object],
    record_actual: dict[str, object],
    expected_request: dict[str, int],
    expected_actual: dict[str, int],
) -> None:
    allowed_root = tmp_path / "orca_runs"
    run_dir = allowed_root / "rxn_resources"
    run_dir.mkdir(parents=True)

    _write_json(
        run_dir / "job_state.json",
        orca_artifact_payload(
            job_id="run_resources_1",
            run_id="run_resources_1",
            reaction_dir=str(run_dir),
            status="running",
        ),
    )
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_resources_1",
                "task_id": "job_resources_1",
                "status": "pending",
                "cancel_requested": False,
                "metadata": {
                    "run_id": "run_resources_1",
                    "reaction_dir": str(run_dir),
                    "resource_request": queue_request,
                    "resource_actual": queue_actual,
                },
            }
        ],
    )
    tracked_record = SimpleNamespace(
        app_name="orca_auto_orca",
        status="queued",
        original_run_dir=str(run_dir),
        selected_input_xyz="",
        latest_known_path=str(run_dir),
        resource_request=record_request,
        resource_actual=record_actual,
    )
    monkeypatch.setattr(_orca_tracking, "load_orca_contract_payload_impl", lambda **kwargs: None)
    monkeypatch.setattr(_orca_tracking, "tracked_runtime_context_impl", lambda **kwargs: None)
    monkeypatch.setattr(
        _orca_tracking,
        "tracked_artifact_context_impl",
        lambda **kwargs: (run_dir, tracked_record, {}, {}),
    )
    monkeypatch.setattr(
        _orca_local_lookup,
        "resolve_job_dir_impl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("job-dir fallback should not run")
        ),
    )

    contract = orca_adapter.load_orca_artifact_contract(
        target="job_resources_1",
        orca_allowed_root=allowed_root,
        queue_id="q_resources_1",
    )

    assert contract.resource_request == expected_request
    assert contract.resource_actual == expected_actual
