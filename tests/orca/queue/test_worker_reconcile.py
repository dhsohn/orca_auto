"""``orca_auto.orca.queue.worker``: the recovery pass, driven through one poll pass."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from orca_auto.orca.machine_observation import report_json_path
from orca_auto.orca.queue.adapter import enqueue
from orca_auto.orca.queue.worker import OrcaQueueWorker
from tests.conftest import claim_next_entry
from tests.engine_artifact_helpers import orca_artifact_payload

# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_orphaned_running_ignores_root_report_even_with_worker_pid_file(
    make_worker: Callable[..., OrcaQueueWorker], queue_root: Path
) -> None:
    worker = make_worker(sleep=lambda _seconds: None)
    rxn = queue_root / "mol_done"
    rxn.mkdir()
    entry = enqueue(queue_root, str(rxn))
    claim_next_entry(queue_root)
    worker._write_pid_file()
    report_json_path(rxn).write_text(
        json.dumps(
            orca_artifact_payload(
                job_id=entry.task_id,
                run_id="run_done_1",
                reaction_dir=str(rxn),
                status="completed",
                final_result={
                    "status": "completed",
                    "completed_at": "2026-03-10T04:59:59+00:00",
                },
            )
        ),
        encoding="utf-8",
    )

    # The first poll pass admits nothing (the row is running), then its upkeep
    # runs the due recovery pass, which requeues the row its dead child left.
    worker.run_pass()

    queue_data = json.loads((queue_root / "queue.json").read_text(encoding="utf-8"))
    found = next(item for item in queue_data if item["queue_id"] == entry.queue_id)
    assert found["status"] == "pending"
    assert "run_id" not in found["metadata"]
