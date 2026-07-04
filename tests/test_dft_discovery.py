"""DFT target file discovery tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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


def test_workflow_workspace_jobs_are_excluded(tmp_path: Path) -> None:
    kb_dir = tmp_path / "runs"
    standalone = kb_dir / "rxn_standalone"
    standalone.mkdir(parents=True)
    (standalone / "calc.out").write_text("standalone", encoding="utf-8")
    _write_orca_state(standalone, status="completed", final_result={"status": "completed"})

    workspace = kb_dir / "wf_20260704"
    stage_job = workspace / "03_orca" / "candidate_01"
    stage_job.mkdir(parents=True)
    (workspace / "workflow.json").write_text("{}", encoding="utf-8")
    (stage_job / "calc.out").write_text("workflow-internal", encoding="utf-8")
    _write_orca_state(stage_job, status="completed", final_result={"status": "completed"})

    targets = discover_orca_targets(kb_dir, max_bytes=1024 * 1024)

    assert [str(t.path) for t in targets] == [str(standalone / "calc.out")]
