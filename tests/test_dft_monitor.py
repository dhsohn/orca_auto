"""DFT monitor tests."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from orca_auto.core.queue.engine.input_snapshot import bind_direct_generation_owner
from orca_auto.orca.dft.monitor import DFTMonitor
from tests.engine_artifact_helpers import orca_artifact_payload

_COMPLETED_OUT = "\n".join(
    [
        "! B3LYP def2-SVP Opt",
        "* xyz 0 1",
        "C 0.0 0.0 0.0",
        "H 0.0 0.0 1.0",
        "*",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "----------------------------",
        " C    0.000000    0.000000    0.000000",
        " H    0.000000    0.000000    1.000000",
        "",
        "FINAL SINGLE POINT ENERGY      -100.123456789",
        "",
        "                             ****ORCA TERMINATED NORMALLY****",
        "TOTAL RUN TIME: 0 days 0 hours 1 minutes 2 seconds 3 msec",
    ]
)

_RUNNING_OPT_OUT = "\n".join(
    [
        "! B3LYP def2-SVP Opt",
        "* xyz 0 1",
        "C 0.0 0.0 0.0",
        "H 0.0 0.0 1.0",
        "*",
        "",
        "CARTESIAN COORDINATES (ANGSTROEM)",
        "----------------------------",
        " C    0.000000    0.000000    0.000000",
        " H    0.000000    0.000000    1.000000",
        "",
        "---------------------------------------------------",
        "| Geometry Optimization Cycle   1                 |",
        "---------------------------------------------------",
        "",
        "FINAL SINGLE POINT ENERGY      -100.100000000",
    ]
)


def _write_current_generation(
    job_dir: Path,
    *,
    status: str,
    out_content: str,
    out_name: str,
) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    generation_dir = job_dir / "20260726-010203-0123abcd"
    generation_dir.mkdir()
    selected_inp = generation_dir / "calc.inp"
    selected_inp.write_text("! SP\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    out_file = generation_dir / out_name
    out_file.write_text(out_content, encoding="utf-8")
    job_status = job_dir.stat()
    generation_status = generation_dir.stat()
    selected_payload = selected_inp.read_bytes()
    owner_token = sha256(str(generation_dir.resolve()).encode()).hexdigest()
    bind_direct_generation_owner(
        job_dir,
        namespace=generation_dir.name,
        expected_job_identity=(int(job_status.st_dev), int(job_status.st_ino)),
        expected_generation_identity=(
            int(generation_status.st_dev),
            int(generation_status.st_ino),
        ),
        owner_token=owner_token,
    )
    execution_provenance = {
        "execution_dir": str(generation_dir.resolve()),
        "execution_dir_identity": {
            "device": int(generation_status.st_dev),
            "inode": int(generation_status.st_ino),
        },
        "generation_owner_token": owner_token,
        "bound_selected_identity": {
            "path": str(selected_inp.resolve()),
            "sha256": sha256(selected_payload).hexdigest(),
            "size_bytes": len(selected_payload),
        },
    }
    (job_dir / "job_state.json").write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=job_dir.name,
                run_id=job_dir.name,
                reaction_dir=str(job_dir),
                selected_inp=str(selected_inp),
                status=status,
                final_result={"status": status},
                engine_payload_extra={"execution_provenance": execution_provenance},
            )
        ),
        encoding="utf-8",
    )
    return out_file


def _advance_mtime(path: Path, *, delta_seconds: float = 5.0) -> None:
    before = os.path.getmtime(path)
    for _ in range(5):
        target = before + delta_seconds
        os.utime(path, (target, target))
        after = os.path.getmtime(path)
        if after > before:
            return
        before = after
        delta_seconds *= 2
    raise AssertionError(f"Failed to advance mtime for {path}")


def test_baseline_seed_prevents_restart_spam(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    out_file = _write_current_generation(
        kb_dir,
        status="completed",
        out_content=_COMPLETED_OUT,
        out_name="calc.out",
    )

    state_file = str(tmp_path / "automation" / "dft_monitor_state.json")

    monitor = DFTMonitor([str(kb_dir)], state_file=state_file)

    # First run: only records baseline
    report1 = monitor.scan()
    assert report1.new_results == []
    assert report1.baseline_seeded is True
    assert Path(state_file).is_file()

    # After restart (new instance), no re-notification for the same file
    monitor2 = DFTMonitor([str(kb_dir)], state_file=state_file)
    report2 = monitor2.scan()
    assert report2.new_results == []

    # Notification when file changes
    out_file.write_text(_COMPLETED_OUT + "\n# changed\n", encoding="utf-8")
    _advance_mtime(out_file)

    report3 = monitor2.scan()
    assert len(report3.new_results) == 1
    assert report3.new_results[0].status == "completed"


def test_running_calc_change_produces_running_notification(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    out_file = _write_current_generation(
        kb_dir,
        status="running",
        out_content=_RUNNING_OPT_OUT,
        out_name="running.out",
    )

    state_file = str(tmp_path / "automation" / "state.json")

    monitor = DFTMonitor([str(kb_dir)], state_file=state_file)
    monitor.scan()  # baseline

    out_file.write_text(_RUNNING_OPT_OUT + "\n# updated\n", encoding="utf-8")
    _advance_mtime(out_file)

    report = monitor.scan()
    assert len(report.new_results) == 1
    assert report.new_results[0].status == "running"


def test_running_calc_change_detected_even_if_mtime_moves_backward(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    out_file = _write_current_generation(
        kb_dir,
        status="running",
        out_content=_RUNNING_OPT_OUT,
        out_name="running.out",
    )

    monitor = DFTMonitor([str(kb_dir)], state_file=str(tmp_path / "automation" / "state.json"))
    monitor.scan()

    baseline_mtime = os.path.getmtime(out_file)
    out_file.write_text(_RUNNING_OPT_OUT + "\n# rewritten\n", encoding="utf-8")
    os.utime(out_file, (baseline_mtime - 10.0, baseline_mtime - 10.0))

    report = monitor.scan()
    assert len(report.new_results) == 1
    assert report.new_results[0].status == "running"


def test_symlink_dedup(tmp_path: Path) -> None:
    kb_dir = tmp_path / "kb"
    run_dir = kb_dir / "run_dir"
    run_dir.mkdir(parents=True)
    link_dir = tmp_path / "run_link"
    link_dir.symlink_to(run_dir, target_is_directory=True)

    _write_current_generation(
        run_dir,
        status="running",
        out_content=_RUNNING_OPT_OUT,
        out_name="running.out",
    )

    state_file = str(tmp_path / "automation" / "state.json")

    # Baseline with symlink path first
    monitor1 = DFTMonitor([str(link_dir)], state_file=state_file)
    monitor1.scan()

    # Restart with real path — should not produce duplicate notifications
    monitor2 = DFTMonitor([str(run_dir)], state_file=state_file)
    report = monitor2.scan()
    assert report.new_results == []
