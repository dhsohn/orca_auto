from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from orca_auto.core.admission import active_slot_count, list_slots
from orca_auto.core.indexing import get_job_location
from orca_auto.core.queue import list_queue
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_cmd
from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_cmd
from orca_auto.flow.submitters import crest as crest_submitter
from orca_auto.flow.submitters import xtb as xtb_submitter
from tests.engine_process_helpers import process_one_crest_for_test, process_one_xtb_for_test
from tests.integration.conftest import wait_for_active_slots


def _queue_status(entry: Any) -> str:
    return str(getattr(getattr(entry, "status", None), "value", "")).strip()


def _write_slow_fake_xtb(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
sleep 1.5
printf '1\\nfake xtb optimized\\nH 0.0 0.0 0.0\\n' > xtbopt.xyz
: > .xtboptok
printf '{"total energy": -4.2, "electronic energy": -4.4}\\n' > xtbout.json
printf 'charges\\n' > charges
printf 'wbo\\n' > wbo
printf 'topology\\n' > xtbtopo.mol
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _submit_manual_jobs(
    smoke_workspace: Any, xtb_opt_job: Path, crest_job: Path
) -> tuple[Any, Any]:
    return (
        xtb_submitter.submit_job_dir(
            job_dir=str(xtb_opt_job),
            priority=5,
            config_path=str(smoke_workspace.xtb_config_path),
        ),
        crest_submitter.submit_job_dir(
            job_dir=str(crest_job),
            priority=5,
            config_path=str(smoke_workspace.crest_config_path),
        ),
    )


def _start_xtb_worker_thread(
    smoke_workspace: Any,
) -> tuple[threading.Thread, list[str], list[BaseException]]:
    xtb_outcomes: list[str] = []
    xtb_errors: list[BaseException] = []

    def _run_xtb_process_one() -> None:
        try:
            xtb_outcomes.append(
                process_one_xtb_for_test(
                    xtb_queue_cmd,
                    xtb_queue_cmd.load_config(str(smoke_workspace.xtb_config_path)),
                )
            )
        except BaseException as exc:  # noqa: BLE001
            xtb_errors.append(exc)

    xtb_thread = threading.Thread(target=_run_xtb_process_one)
    xtb_thread.start()
    return xtb_thread, xtb_outcomes, xtb_errors


def _assert_job_record(root: Path, job_id: str, job_dir: Path) -> None:
    record = get_job_location(root, job_id)
    assert record is not None
    assert record.status == "completed"
    assert record.organized_output_dir == ""
    assert record.latest_known_path == str(job_dir.resolve())
    assert Path(record.latest_known_path).exists()


def test_xtb_and_crest_share_single_admission_slot(
    smoke_workspace: Any,
    xtb_opt_job: Path,
    crest_job: Path,
    capsys: Any,
) -> None:
    _write_slow_fake_xtb(smoke_workspace.fake_xtb)
    xtb_submission, crest_submission = _submit_manual_jobs(smoke_workspace, xtb_opt_job, crest_job)

    assert xtb_submission["status"] == "submitted"
    assert crest_submission["status"] == "submitted"
    assert _queue_status(list_queue(smoke_workspace.xtb_allowed_root)[0]) == "pending"
    assert _queue_status(list_queue(smoke_workspace.crest_allowed_root)[0]) == "pending"

    xtb_thread, xtb_outcomes, xtb_errors = _start_xtb_worker_thread(smoke_workspace)
    slots = wait_for_active_slots(smoke_workspace.admission_root, expected=1)
    assert active_slot_count(smoke_workspace.admission_root) == 1
    assert slots[0].app_name == "orca_auto_xtb"
    assert slots[0].source == "orca_auto.flow.engines.xtb.queue_worker"

    assert (
        process_one_crest_for_test(
            crest_queue_cmd,
            crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path)),
        )
        == "blocked"
    )
    assert active_slot_count(smoke_workspace.admission_root) == 1
    assert len(list_slots(smoke_workspace.admission_root)) == 1
    assert _queue_status(list_queue(smoke_workspace.crest_allowed_root)[0]) == "pending"

    xtb_thread.join(timeout=15)
    assert not xtb_thread.is_alive()
    assert xtb_errors == []
    assert xtb_outcomes == ["processed"]
    xtb_stdout = capsys.readouterr().out
    assert f"queue_id: {xtb_submission['queue_id']}" in xtb_stdout
    assert f"job_id: {xtb_submission['job_id']}" in xtb_stdout
    assert "status: completed" in xtb_stdout

    assert list_slots(smoke_workspace.admission_root) == []
    _assert_job_record(smoke_workspace.xtb_allowed_root, xtb_submission["job_id"], xtb_opt_job)

    assert (
        process_one_crest_for_test(
            crest_queue_cmd,
            crest_queue_cmd.load_config(str(smoke_workspace.crest_config_path)),
        )
        == "processed"
    )
    crest_stdout = capsys.readouterr().out
    assert f"queue_id: {crest_submission['queue_id']}" in crest_stdout
    assert f"job_id: {crest_submission['job_id']}" in crest_stdout
    assert "status: completed" in crest_stdout
    _assert_job_record(smoke_workspace.crest_allowed_root, crest_submission["job_id"], crest_job)

    assert _queue_status(list_queue(smoke_workspace.xtb_allowed_root)[0]) == "completed"
    assert _queue_status(list_queue(smoke_workspace.crest_allowed_root)[0]) == "completed"
    assert list_slots(smoke_workspace.admission_root) == []
