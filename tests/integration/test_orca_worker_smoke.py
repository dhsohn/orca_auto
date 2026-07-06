from __future__ import annotations

import json
from pathlib import Path

from orca_auto.cli import main as cli_main
from orca_auto.core.admission import list_slots
from orca_auto.core.queue.processes import worker_pid_file_path
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.config import load_config
from orca_auto.orca.queue.adapter import list_queue, queue_entry_reaction_dir
from orca_auto.orca.queue.worker import WORKER_PID_FILE, QueueWorker
from orca_auto.orca.state import (
    load_report_json,
    load_state,
    report_json_path,
    report_md_path,
)


def _write_fake_orca(binary_path: Path, counter_path: Path) -> None:
    binary_path.write_text(
        "\n".join(
            [
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
                "inp_name = Path(sys.argv[1]).name if len(sys.argv) > 1 else '<missing>'",
                "print(f'Fake ORCA consumed {inp_name}')",
                "print('TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds')",
                "print('****ORCA TERMINATED NORMALLY****')",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
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
                "orca": {
                    "runtime": {
                        "default_max_retries": 0,
                    },
                    "paths": {"orca_executable": str(orca_executable)},
                },
                "telegram": {
                    "bot_token": "",
                    "chat_id": "",
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

    out_path = reaction_dir / "rxn.out"
    assert out_path.exists()
    assert "****ORCA TERMINATED NORMALLY****" in out_path.read_text(encoding="utf-8")

    state = load_state(reaction_dir)
    assert state is not None
    assert state["status"] == "completed"
    assert state["selected_inp"] == str(selected_inp.resolve())
    assert state["final_result"] is not None
    assert state["final_result"]["status"] == "completed"

    report = load_report_json(reaction_dir)
    assert report is not None
    assert report["status"]["state"] == "completed"
    assert report_json_path(reaction_dir).exists()
    assert report_md_path(reaction_dir).exists()
