from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from orca_auto.cli import main as cli_main
from orca_auto.core.admission import list_slots
from orca_auto.core.artifacts import RUN_REPORT_HTML_FILE, SI_BLOCK_MD_FILE
from orca_auto.core.queue.processes import worker_pid_file_path
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.config import load_config
from orca_auto.orca.parser import parse_orca_output
from orca_auto.orca.queue.adapter import list_queue, queue_entry_reaction_dir
from orca_auto.orca.queue.worker import WORKER_PID_FILE, QueueWorker
from orca_auto.orca.state import (
    load_report_json,
    load_state,
    report_json_path,
    report_md_path,
)


def _write_fake_orca(
    binary_path: Path,
    counter_path: Path,
    *,
    normal_termination: bool = True,
) -> None:
    lines = [
        "#!/usr/bin/env python3",
        "import sys",
        "from pathlib import Path",
        f"counter = Path({str(counter_path)!r})",
        "count = 0",
        "if counter.exists():",
        "    try:",
        "        count = int(counter.read_text(encoding='utf-8').strip() or '0')",
        "    except ValueError:",
        "        count = 0",
        "counter.write_text(str(count + 1), encoding='utf-8')",
        "inp = Path(sys.argv[1]).resolve()",
        "inp_name = inp.name",
        "inp_text = inp.read_text(encoding='utf-8')",
        "print(f'Fake ORCA consumed {inp_name}')",
        "print('Program Version 6.0.1 - RELEASE -')",
        "for line_number, line in enumerate(inp_text.splitlines(), start=1):",
        "    print(f'| {line_number:2d}> {line}')",
        "print('CARTESIAN COORDINATES (ANGSTROEM)')",
        "print('---------------------------------')",
        "print(' H 0.000000 0.000000 0.000000')",
        "print(' H 0.000000 0.000000 0.740000')",
        "print('')",
        "print('FINAL SINGLE POINT ENERGY -1.100000000000')",
        "print('THE OPTIMIZATION HAS CONVERGED')",
        "print('TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds')",
    ]
    if normal_termination:
        lines.append("print('****ORCA TERMINATED NORMALLY****')")
    lines.extend(["raise SystemExit(0)", ""])
    binary_path.write_text("\n".join(lines), encoding="utf-8")
    binary_path.chmod(0o755)


def _write_orca_worker_config(
    path: Path,
    *,
    allowed_root: Path,
    admission_root: Path,
    orca_executable: Path,
) -> None:
    path.write_text(
        json.dumps(
            {
                "runs_root": str(allowed_root),
                "scheduler": {
                    "max_active_simulations": 1,
                    "admission_root": str(admission_root),
                },
                "resources": {
                    "max_cores_per_task": 1,
                    "max_memory_gb_per_task": 1,
                },
                "orca": {
                    "runtime": {
                        "default_max_retries": 0,
                    },
                    "paths": {"orca_executable": str(orca_executable)},
                },
                "messenger": {
                    "telegram": {
                        "bot_token": "",
                        "chat_id": "",
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _queue_entry_for_reaction(root: Path, reaction_dir: Path):
    matches = [
        entry
        for entry in list_queue(root)
        if queue_entry_reaction_dir(entry) == str(reaction_dir.resolve())
    ]
    assert len(matches) == 1
    return matches[0]


def test_orca_queue_worker_run_once_executes_fake_orca_child_lifecycle(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    admission_root = tmp_path / "admission"
    bin_dir = tmp_path / "bin"
    reaction_dir = allowed_root / "project_a" / "rxn_worker_lifecycle"
    for path in (allowed_root, admission_root, bin_dir, reaction_dir):
        path.mkdir(parents=True, exist_ok=True)

    counter_path = tmp_path / "fake_orca_counter.txt"
    fake_orca = bin_dir / "fake_orca.py"
    _write_fake_orca(fake_orca, counter_path)
    config_path = tmp_path / "orca_auto.yaml"
    _write_orca_worker_config(
        config_path,
        allowed_root=allowed_root,
        admission_root=admission_root,
        orca_executable=fake_orca,
    )

    selected_inp = reaction_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    queued = _queue_entry_for_reaction(allowed_root, reaction_dir)
    assert queued.status == QueueStatus.PENDING
    assert not counter_path.exists()

    worker = QueueWorker(
        load_config(str(config_path)),
        str(config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0

    completed = _queue_entry_for_reaction(allowed_root, reaction_dir)
    assert completed.queue_id == queued.queue_id
    assert completed.status == QueueStatus.COMPLETED
    assert counter_path.read_text(encoding="utf-8") == "1"
    assert list_slots(admission_root) == []
    assert not worker_pid_file_path(allowed_root, WORKER_PID_FILE).exists()

    execution_snapshot = completed.metadata["execution_snapshot"]
    bound_input = Path(execution_snapshot["selected_inp"])
    generation_dir = Path(execution_snapshot["execution_dir"])
    assert generation_dir.parent == reaction_dir.resolve()
    assert bound_input == generation_dir / selected_inp.name
    assert not (reaction_dir / ".orca_auto_orca_executions").exists()
    assert not (reaction_dir / ".orca_auto_input_snapshots").exists()
    out_path = bound_input.with_suffix(".out")
    assert out_path.exists()
    raw_output = out_path.read_text(encoding="utf-8")
    assert "****ORCA TERMINATED NORMALLY****" in raw_output
    assert "|  1> ! Opt" in raw_output
    assert "r2scan-3c" not in raw_output

    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "completed"
    assert state["selected_inp"] == str(bound_input.resolve())
    assert state["execution_provenance"]["source_selected_inp"] == str(selected_inp.resolve())
    assert state["attempts"][0]["output_identity"]["path"] == str(out_path.resolve())
    assert state["final_result"] is not None
    assert state["final_result"]["status"] == "completed"
    generation_state = load_state(generation_dir)
    assert generation_state == state

    report = load_report_json(reaction_dir)
    assert report is not None
    assert report["status"]["state"] == "completed"
    assert report_json_path(reaction_dir).exists()
    assert load_report_json(generation_dir) == report
    assert report_md_path(reaction_dir).exists()
    report_html = reaction_dir / RUN_REPORT_HTML_FILE
    assert report_html.exists()
    report_html_text = report_html.read_text(encoding="utf-8")
    assert "completed" in report_html_text
    assert "<code>! Opt</code>" in report_html_text
    assert "r2scan-3c" not in report_html_text
    assert '<td class="ok">completed' in report_html_text
    assert "AnalyzerStatus.COMPLETED" not in report_html_text
    report_markdown = report_md_path(reaction_dir).read_text(encoding="utf-8")
    assert "Status: `completed`" in report_markdown
    assert "AnalyzerStatus." not in report_markdown
    assert "<AnalyzerStatus" not in report_markdown
    si_block = reaction_dir / SI_BLOCK_MD_FILE
    assert si_block.exists()
    si_text = si_block.read_text(encoding="utf-8")
    assert "E(el)" in si_text
    assert "-1.100000 Eh" in si_text
    assert "! Opt" in si_text
    assert "r2scan-3c" not in si_text


def test_orca_queue_worker_reuses_job_directory_without_overwriting_prior_generation(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    admission_root = tmp_path / "admission"
    bin_dir = tmp_path / "bin"
    reaction_dir = allowed_root / "project_a" / "reusable_job"
    for path in (allowed_root, admission_root, bin_dir, reaction_dir):
        path.mkdir(parents=True, exist_ok=True)

    counter_path = tmp_path / "fake_orca_counter.txt"
    fake_orca = bin_dir / "fake_orca.py"
    _write_fake_orca(fake_orca, counter_path)
    config_path = tmp_path / "orca_auto.yaml"
    _write_orca_worker_config(
        config_path,
        allowed_root=allowed_root,
        admission_root=admission_root,
        orca_executable=fake_orca,
    )
    selected_inp = reaction_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    first_worker = QueueWorker(load_config(str(config_path)), str(config_path), max_concurrent=1)
    first_worker.poll_interval_seconds = 0.05
    assert first_worker.run_once(idle_message=None, blocked_message=None) == 0
    [first_entry] = list_queue(allowed_root)
    assert first_entry.status == QueueStatus.COMPLETED
    first_generation = Path(first_entry.metadata["execution_snapshot"]["execution_dir"])
    first_state = load_state(first_generation)
    first_report = load_report_json(first_generation)
    assert first_state is not None
    assert first_report is not None
    first_state_bytes = (first_generation / "job_state.json").read_bytes()
    first_report_bytes = (first_generation / "job_report.json").read_bytes()

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    entries = list_queue(allowed_root)
    assert len(entries) == 2
    [second_entry] = [entry for entry in entries if entry.status == QueueStatus.PENDING]
    second_generation = Path(second_entry.metadata["execution_snapshot"]["execution_dir"])
    assert second_generation != first_generation

    second_worker = QueueWorker(load_config(str(config_path)), str(config_path), max_concurrent=1)
    second_worker.poll_interval_seconds = 0.05
    assert second_worker.run_once(idle_message=None, blocked_message=None) == 0

    completed_entries = list_queue(allowed_root)
    assert len(completed_entries) == 2
    assert all(entry.status == QueueStatus.COMPLETED for entry in completed_entries)
    assert counter_path.read_text(encoding="utf-8") == "2"
    assert (first_generation / "job_state.json").read_bytes() == first_state_bytes
    assert (first_generation / "job_report.json").read_bytes() == first_report_bytes
    second_state = load_state(second_generation)
    second_report = load_report_json(second_generation)
    assert second_state is not None
    assert second_report is not None
    assert second_state["run_id"] != first_state["run_id"]
    assert second_report["job"]["id"] != first_report["job"]["id"]
    assert load_state(reaction_dir) == second_state
    assert load_report_json(reaction_dir) == second_report


def test_orca_worker_preflight_failure_publishes_generation_and_root_reports(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    admission_root = tmp_path / "admission"
    bin_dir = tmp_path / "bin"
    reaction_dir = allowed_root / "project_a" / "preflight_failure"
    for path in (allowed_root, admission_root, bin_dir, reaction_dir):
        path.mkdir(parents=True, exist_ok=True)

    counter_path = tmp_path / "fake_orca_counter.txt"
    fake_orca = bin_dir / "fake_orca.py"
    _write_fake_orca(fake_orca, counter_path)
    config_path = tmp_path / "orca_auto.yaml"
    _write_orca_worker_config(
        config_path,
        allowed_root=allowed_root,
        admission_root=admission_root,
        orca_executable=fake_orca,
    )
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    [entry] = list_queue(allowed_root)
    generation = Path(entry.metadata["execution_snapshot"]["execution_dir"])
    bound_input = Path(entry.metadata["selected_inp"])
    bound_input.chmod(0o600)
    bound_input.write_text(
        bound_input.read_text(encoding="utf-8") + "# corrupt\n", encoding="utf-8"
    )

    worker = QueueWorker(load_config(str(config_path)), str(config_path), max_concurrent=1)
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0

    [failed] = list_queue(allowed_root)
    assert failed.status == QueueStatus.FAILED
    assert not counter_path.exists()
    root_state = load_state(reaction_dir)
    generation_state = load_state(generation)
    root_report = load_report_json(reaction_dir)
    generation_report = load_report_json(generation)
    assert root_state is not None and root_state["status"] == "failed"
    assert generation_state == root_state
    assert root_report is not None and root_report["status"]["state"] == "failed"
    assert generation_report == root_report
    assert root_report["job"]["id"] == entry.task_id


def test_orca_worker_generation_replacement_never_receives_synthetic_artifacts(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    admission_root = tmp_path / "admission"
    bin_dir = tmp_path / "bin"
    reaction_dir = allowed_root / "project_a" / "replaced_generation"
    for path in (allowed_root, admission_root, bin_dir, reaction_dir):
        path.mkdir(parents=True, exist_ok=True)

    counter_path = tmp_path / "fake_orca_counter.txt"
    fake_orca = bin_dir / "fake_orca.py"
    _write_fake_orca(fake_orca, counter_path)
    config_path = tmp_path / "orca_auto.yaml"
    _write_orca_worker_config(
        config_path,
        allowed_root=allowed_root,
        admission_root=admission_root,
        orca_executable=fake_orca,
    )
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    [entry] = list_queue(allowed_root)
    generation = Path(entry.metadata["execution_snapshot"]["execution_dir"])
    moved_generation = generation.with_name(f"{generation.name}-moved")
    generation.rename(moved_generation)
    generation.mkdir()
    (generation / "sentinel").write_text("replacement", encoding="utf-8")

    worker = QueueWorker(load_config(str(config_path)), str(config_path), max_concurrent=1)
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0

    [failed] = list_queue(allowed_root)
    assert failed.status == QueueStatus.FAILED
    assert not counter_path.exists()
    root_state = load_state(reaction_dir)
    root_report = load_report_json(reaction_dir)
    assert root_state is not None and root_state["status"] == "failed"
    assert root_report is not None and root_report["status"]["state"] == "failed"
    assert root_report["job"]["id"] == entry.task_id
    assert {path.name for path in generation.iterdir()} == {"sentinel"}
    assert not (moved_generation / "job_state.json").exists()
    assert not (moved_generation / "job_report.json").exists()


def test_orca_queue_worker_rejects_return_code_zero_without_normal_marker(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    admission_root = tmp_path / "admission"
    bin_dir = tmp_path / "bin"
    reaction_dir = allowed_root / "project_a" / "rxn_false_success"
    for path in (allowed_root, admission_root, bin_dir, reaction_dir):
        path.mkdir(parents=True, exist_ok=True)

    counter_path = tmp_path / "fake_orca_counter.txt"
    fake_orca = bin_dir / "fake_orca.py"
    _write_fake_orca(fake_orca, counter_path, normal_termination=False)
    config_path = tmp_path / "orca_auto.yaml"
    _write_orca_worker_config(
        config_path,
        allowed_root=allowed_root,
        admission_root=admission_root,
        orca_executable=fake_orca,
    )

    selected_inp = reaction_dir / "rxn.inp"
    selected_inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8")

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    queued = _queue_entry_for_reaction(allowed_root, reaction_dir)
    assert queued.status == QueueStatus.PENDING
    assert not counter_path.exists()

    worker = QueueWorker(
        load_config(str(config_path)),
        str(config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0

    failed = _queue_entry_for_reaction(allowed_root, reaction_dir)
    assert failed.queue_id == queued.queue_id
    assert failed.status == QueueStatus.FAILED
    assert counter_path.read_text(encoding="utf-8") == "1"
    assert list_slots(admission_root) == []
    assert not worker_pid_file_path(allowed_root, WORKER_PID_FILE).exists()

    execution_snapshot = failed.metadata["execution_snapshot"]
    bound_input = Path(execution_snapshot["selected_inp"])
    out_path = bound_input.with_suffix(".out")
    raw_output = out_path.read_text(encoding="utf-8")
    assert "Fake ORCA consumed" in raw_output
    assert "TOTAL RUN TIME" in raw_output
    assert "ORCA TERMINATED NORMALLY" not in raw_output

    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "failed"
    assert len(state["attempts"]) == 1
    attempt = state["attempts"][0]
    assert attempt["return_code"] == 0
    assert attempt["analyzer_status"] == "incomplete"
    assert attempt["analyzer_reason"] == "run_incomplete"
    assert attempt["markers"]["terminated_normally"] is False
    assert state["final_result"] is not None
    assert state["final_result"]["status"] == "failed"
    assert state["final_result"]["reason"] == "run_incomplete"
    assert state["final_result"]["last_out_path"] == str(out_path.resolve())

    report = load_report_json(reaction_dir)
    assert report is not None
    assert report["status"]["state"] == "failed"
    assert report["status"]["reason"] == "run_incomplete"
    assert report_json_path(reaction_dir).exists()
    assert report_md_path(reaction_dir).exists()
    report_html = reaction_dir / RUN_REPORT_HTML_FILE
    assert report_html.exists()
    report_html_text = report_html.read_text(encoding="utf-8")
    assert "retry_limit_reached" not in report_html_text
    assert "run_incomplete" in report_html_text
    assert not (reaction_dir / SI_BLOCK_MD_FILE).exists()


def test_real_orca_h2_single_point_acceptance_when_configured(tmp_path: Path) -> None:
    executable_text = os.environ.get("ORCA_REAL_EXECUTABLE", "").strip()
    if not executable_text:
        pytest.skip("set ORCA_REAL_EXECUTABLE to run the real ORCA acceptance")
    executable = Path(executable_text).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        pytest.fail(f"ORCA_REAL_EXECUTABLE is not executable: {executable}")

    allowed_root = tmp_path / "orca_runs"
    admission_root = tmp_path / "admission"
    reaction_dir = allowed_root / "real_orca_h2_sp"
    for path in (allowed_root, admission_root, reaction_dir):
        path.mkdir(parents=True, exist_ok=True)

    config_path = tmp_path / "orca_auto.yaml"
    _write_orca_worker_config(
        config_path,
        allowed_root=allowed_root,
        admission_root=admission_root,
        orca_executable=executable,
    )
    geometry = reaction_dir / "h2.xyz"
    geometry.write_text("2\nH2\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    selected_inp = reaction_dir / "h2.inp"
    selected_inp.write_text(
        "! HF STO-3G SP TightSCF\n* xyzfile 0 1 h2.xyz\n",
        encoding="utf-8",
    )

    assert cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)]) == 0
    queued = _queue_entry_for_reaction(allowed_root, reaction_dir)
    assert queued.status == QueueStatus.PENDING

    worker = QueueWorker(
        load_config(str(config_path)),
        str(config_path),
        max_concurrent=1,
    )
    worker.poll_interval_seconds = 0.05
    assert worker.run_once(idle_message=None, blocked_message=None) == 0

    completed = _queue_entry_for_reaction(allowed_root, reaction_dir)
    assert completed.queue_id == queued.queue_id
    assert completed.status == QueueStatus.COMPLETED
    assert list_slots(admission_root) == []
    assert not worker_pid_file_path(allowed_root, WORKER_PID_FILE).exists()

    execution_snapshot = completed.metadata["execution_snapshot"]
    assert execution_snapshot["dependency_paths"] == [str(geometry.resolve())]
    assert set(execution_snapshot["materialized_inputs"]) == {"dependency_000000"}
    private_geometry = Path(execution_snapshot["materialized_inputs"]["dependency_000000"]["path"])
    assert private_geometry == Path(execution_snapshot["execution_dir"]) / geometry.name
    assert private_geometry.read_bytes() == geometry.read_bytes()
    bound_input = Path(execution_snapshot["selected_inp"])
    assert bound_input.parent == reaction_dir.resolve() / execution_snapshot["generation_name"]
    assert not (bound_input.parent / ".inputs").exists()
    bound_text = bound_input.read_text(encoding="utf-8")
    assert "! HF STO-3G SP TightSCF" in bound_text
    assert "* xyzfile 0 1 h2.xyz" in bound_text
    out_path = bound_input.with_suffix(".out")
    raw_output = out_path.read_text(encoding="utf-8")
    assert "ORCA_ReadXYZFile::Error" not in raw_output
    assert "CANNOT OPEN FILE" not in raw_output
    assert "FINAL SINGLE POINT ENERGY" in raw_output
    assert "****ORCA TERMINATED NORMALLY****" in raw_output

    parsed = parse_orca_output(str(out_path))
    assert parsed.energy_hartree is not None and math.isfinite(parsed.energy_hartree)
    assert -2.0 < parsed.energy_hartree < 0.0
    assert parsed.formula == "H2"
    assert parsed.n_atoms == 2
    assert parsed.charge == 0
    assert parsed.multiplicity == 1
    assert parsed.orca_version
    assert {"HF", "STO-3G", "SP"}.issubset(parsed.input_line.split())

    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "completed"
    assert state["final_result"] is not None
    assert state["final_result"]["status"] == "completed"
    assert state["final_result"]["reason"] == "normal_termination"
    assert state["final_result"]["last_out_path"] == str(out_path.resolve())

    report = load_report_json(reaction_dir)
    assert report is not None
    assert report["status"]["state"] == "completed"
    assert report_json_path(reaction_dir).is_file()
    assert report_md_path(reaction_dir).is_file()
    energy_text = f"{parsed.energy_hartree:.6f}"
    report_html = (reaction_dir / RUN_REPORT_HTML_FILE).read_text(encoding="utf-8")
    assert "SP report" in report_html
    assert energy_text in report_html
    assert parsed.orca_version in report_html
    assert '<td class="ok">completed' in report_html
    si_text = (reaction_dir / SI_BLOCK_MD_FILE).read_text(encoding="utf-8")
    assert "E(el)" in si_text
    assert energy_text in si_text
    assert "H2" in si_text
    assert "HF STO-3G SP TightSCF" in si_text
    assert parsed.orca_version in si_text
    report_markdown = report_md_path(reaction_dir).read_text(encoding="utf-8")
    assert "Status: `completed`" in report_markdown
    assert "AnalyzerStatus." not in report_markdown
    assert "<AnalyzerStatus" not in report_markdown
