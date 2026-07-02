"""DFT target file discovery tests."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from orca_auto.orca.dft.discovery import discover_orca_targets
from tests.engine_artifact_helpers import orca_artifact_payload


def _write_orca_state(
    run_dir: Path,
    *,
    status: str,
    reaction_dir: str = "",
    selected_inp: str = "",
    final_result: dict[str, object] | None = None,
) -> None:
    (run_dir / "job_state.json").write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=run_dir.name,
                run_id=run_dir.name,
                reaction_dir=reaction_dir or str(run_dir),
                selected_inp=selected_inp,
                status=status,
                final_result=final_result or {},
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_default_policy_uses_run_state_only(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    run_dir = kb_dir / "job_report_only"
    run_dir.mkdir(parents=True)

    out_file = run_dir / "final.out"
    out_file.write_text("result", encoding="utf-8")
    (run_dir / "job_report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "final_result": {
                    "status": "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert targets == []


def test_orca_runs_tracks_latest_out_for_running_state(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_running"
    run_dir.mkdir(parents=True)

    old_out = run_dir / "job.retry01.out"
    new_out = run_dir / "job.retry02.out"
    old_out.write_text("old", encoding="utf-8")
    new_out.write_text("new", encoding="utf-8")
    if new_out.stat().st_mtime <= old_out.stat().st_mtime:
        new_out.touch()

    _write_orca_state(
        run_dir,
        status="running",
        reaction_dir="/home/someone/orca_runs/job_running",
        selected_inp="/home/someone/orca_runs/job_running/job.inp",
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(new_out)]


def test_orca_runs_ignores_reaction_dir_and_uses_state_directory(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_reaction_dir_ignored"
    run_dir.mkdir(parents=True)

    stale_dir = tmp_path / "host_path" / "job_reaction_dir_ignored"
    stale_dir.mkdir(parents=True)
    (stale_dir / "host_only.out").write_text("host", encoding="utf-8")

    local_out = run_dir / "input.out"
    local_out.write_text("local", encoding="utf-8")

    _write_orca_state(
        run_dir,
        status="running",
        reaction_dir=str(stale_dir),
        selected_inp=str(stale_dir / "input.inp"),
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(local_out)]


def test_orca_runs_tracks_latest_out_for_failed_state(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "job_failed"
    run_dir.mkdir(parents=True)

    old_out = run_dir / "job.retry01.out"
    new_out = run_dir / "job.retry02.out"
    old_out.write_text("old", encoding="utf-8")
    new_out.write_text("new", encoding="utf-8")
    if new_out.stat().st_mtime <= old_out.stat().st_mtime:
        new_out.touch()

    _write_orca_state(run_dir, status="failed")

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(new_out)]


def test_orca_runs_ignores_report_only_directory(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_runs"
    run_dir = kb_dir / "report_only"
    run_dir.mkdir(parents=True)

    (run_dir / "result.out").write_text("x", encoding="utf-8")
    (run_dir / "job_report.json").write_text(
        json.dumps({"status": "completed", "final_result": {"status": "completed"}}),
        encoding="utf-8",
    )

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert targets == []


def test_orca_outputs_tracks_latest_out_from_run_state_dir(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_outputs"
    run_dir = kb_dir / "run_ok"
    run_dir.mkdir(parents=True)

    final_out = run_dir / "final.out"
    newer_retry = run_dir / "final.retry01.out"
    final_out.write_text("final", encoding="utf-8")
    newer_retry.write_text("retry", encoding="utf-8")
    mtime = final_out.stat().st_mtime
    os.utime(newer_retry, (mtime + 5.0, mtime + 5.0))

    _write_orca_state(run_dir, status="completed", final_result={"status": "completed"})

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)
    assert [str(t.path) for t in targets] == [str(newer_retry)]


def test_orca_outputs_prefers_run_state_status_when_report_also_exists(
    tmp_path: Path,
) -> None:
    kb_dir = tmp_path / "orca_outputs"
    run_dir = kb_dir / "run_status_priority"
    run_dir.mkdir(parents=True)

    (run_dir / "final.out").write_text("result", encoding="utf-8")
    _write_orca_state(run_dir, status="running")
    (run_dir / "job_report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "final_result": {
                    "status": "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "last_out_path": "/home/someone/orca_outputs/run_status_priority/final.out",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    targets = discover_orca_targets(
        kb_dir,
        max_bytes=1024 * 1024,
        recent_completed_window_minutes=60,
    )
    assert targets == []


def test_orca_outputs_ignores_job_report_when_job_state_missing(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_outputs"
    run_dir = kb_dir / "job_report_only"
    run_dir.mkdir(parents=True)

    (run_dir / "final.out").write_text("result", encoding="utf-8")
    (run_dir / "job_report.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "final_result": {
                    "status": "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "last_out_path": "/home/someone/orca_outputs/job_report_only/final.out",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    targets = discover_orca_targets(
        kb_dir,
        max_bytes=1024 * 1024,
        recent_completed_window_minutes=60,
    )
    assert targets == []


def test_orca_outputs_includes_recent_completed_with_completed_at(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_outputs"
    run_dir = kb_dir / "run_recent"
    run_dir.mkdir(parents=True)

    final_out = run_dir / "final.out"
    final_out.write_text("final", encoding="utf-8")

    completed_at = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    _write_orca_state(
        run_dir,
        status="completed",
        final_result={
            "status": "completed",
            "completed_at": completed_at,
        },
    )

    targets = discover_orca_targets(
        kb_dir,
        max_bytes=1024 * 1024,
        recent_completed_window_minutes=60,
    )
    assert [str(t.path) for t in targets] == [str(final_out)]


def test_orca_outputs_excludes_old_completed_with_completed_at(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_outputs"
    run_dir = kb_dir / "run_old"
    run_dir.mkdir(parents=True)

    final_out = run_dir / "final.out"
    final_out.write_text("final", encoding="utf-8")

    completed_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    _write_orca_state(
        run_dir,
        status="completed",
        final_result={
            "status": "completed",
            "completed_at": completed_at,
        },
    )

    targets = discover_orca_targets(
        kb_dir,
        max_bytes=1024 * 1024,
        recent_completed_window_minutes=60,
    )
    assert targets == []


def test_orca_outputs_uses_mtime_when_completed_at_missing(tmp_path: Path) -> None:
    kb_dir = tmp_path / "orca_outputs"
    run_dir = kb_dir / "run_no_completed_at"
    run_dir.mkdir(parents=True)

    final_out = run_dir / "final.out"
    final_out.write_text("final", encoding="utf-8")

    old_mtime = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(final_out, (old_mtime, old_mtime))

    _write_orca_state(run_dir, status="completed", final_result={"status": "completed"})

    targets = discover_orca_targets(
        kb_dir,
        max_bytes=1024 * 1024,
        recent_completed_window_minutes=60,
    )
    assert targets == []
