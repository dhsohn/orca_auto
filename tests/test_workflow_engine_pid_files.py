from __future__ import annotations

import os
from pathlib import Path

from orca_auto.core.queue.processes import write_worker_pid_file
from orca_auto.flow.engines.crest import queue_runtime as crest_queue
from orca_auto.flow.engines.crest.engine import ENGINE_DEFINITION as CREST_ENGINE_DEFINITION
from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue


def test_workflow_engine_workers_use_distinct_pid_files(tmp_path: Path) -> None:
    crest_worker_pid_file = CREST_ENGINE_DEFINITION.worker_pid_file_name
    assert crest_worker_pid_file != xtb_queue.WORKER_PID_FILE

    write_worker_pid_file(tmp_path, crest_worker_pid_file)

    assert crest_queue.read_worker_pid(tmp_path) == os.getpid()
    assert xtb_queue.read_worker_pid(tmp_path) is None

    write_worker_pid_file(tmp_path, xtb_queue.WORKER_PID_FILE)

    assert xtb_queue.read_worker_pid(tmp_path) == os.getpid()
