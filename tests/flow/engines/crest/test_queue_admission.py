from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.flow.engines.crest import queue_admission


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(
            admission_root="/tmp/admission",
            admission_limit=3,
            max_concurrent=4,
        )
    )


def test_reserve_admission_slot_uses_crest_worker_identity() -> None:
    calls: list[tuple[str, int, str, str, str]] = []

    def reserve_slot(
        root: str,
        limit: int,
        *,
        source: str,
        app_name: str,
        engine_process_state: str,
    ) -> str:
        calls.append((root, limit, source, app_name, engine_process_state))
        return "slot-1"

    assert queue_admission.reserve_admission_slot(_cfg(), reserve_slot_fn=reserve_slot) == "slot-1"
    assert calls == [
        (
            "/tmp/admission",
            3,
            "orca_auto.flow.engines.crest.queue_worker",
            "orca_auto_crest",
            "idle",
        )
    ]


def test_start_background_job_process_builds_crest_child_command(tmp_path: Path) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    process = object()
    commands: list[list[str]] = []

    def build_command(**kwargs: Any) -> list[str]:
        assert kwargs == {
            "config_path": "/tmp/orca_auto.yaml",
            "queue_root": tmp_path / "queue",
            "queue_id": "queue-1",
            "admission_token": "slot-1",
        }
        return ["python", "-m", "orca_auto.flow.engines.crest.execution"]

    def start_process(command: list[str]) -> object:
        commands.append(command)
        return process

    assert (
        queue_admission.start_background_job_process(
            config_path="/tmp/orca_auto.yaml",
            queue_root=tmp_path / "queue",
            entry=entry,
            admission_root="/tmp/admission",
            admission_token="slot-1",
            start_background_process_fn=start_process,
            build_worker_child_command_fn=build_command,
        )
        is process
    )
    assert commands == [["python", "-m", "orca_auto.flow.engines.crest.execution"]]


def test_attach_started_process_records_child_owner(tmp_path: Path) -> None:
    entry = SimpleNamespace(
        queue_id="queue-1",
        metadata={"job_dir": str(tmp_path / "job")},
    )
    process = SimpleNamespace(pid=4321)
    activated: list[dict[str, Any]] = []

    def activate_reserved_slot(root: str, token: str, **kwargs: Any) -> object:
        activated.append({"root": root, "token": token, **kwargs})
        return object()

    attached = queue_admission.attach_started_process(
        admission_root="/tmp/admission",
        queue_root=tmp_path / "queue",
        entry=entry,
        process=process,
        admission_token="slot-1",
        activate_reserved_slot_fn=activate_reserved_slot,
        terminate_process_fn=lambda _process: None,
        mark_entry_failed_and_release_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
    )

    assert attached is True
    assert activated == [
        {
            "root": "/tmp/admission",
            "token": "slot-1",
            "owner_pid": 4321,
            "source": "orca_auto.flow.engines.crest.queue_worker.child",
            "queue_id": "queue-1",
            "work_dir": str(tmp_path / "job"),
        }
    ]


def test_attach_started_process_terminates_and_marks_failed_when_slot_missing(
    tmp_path: Path,
) -> None:
    entry = SimpleNamespace(queue_id="queue-1", metadata={})
    process = SimpleNamespace(pid=4321)
    terminated: list[object] = []
    failed: list[dict[str, Any]] = []

    def mark_failed_and_release(*args: Any, **kwargs: Any) -> None:
        failed.append({"args": args, "kwargs": kwargs})

    attached = queue_admission.attach_started_process(
        admission_root="/tmp/admission",
        queue_root=tmp_path / "queue",
        entry=entry,
        process=process,
        admission_token="slot-1",
        activate_reserved_slot_fn=lambda *args, **kwargs: None,
        terminate_process_fn=lambda running: terminated.append(running),
        mark_entry_failed_and_release_fn=mark_failed_and_release,
        mark_failed_fn=lambda *args, **kwargs: None,
    )

    assert attached is False
    assert terminated == [process]
    assert failed[0]["args"] == (tmp_path / "queue", entry, "slot-1")
    assert failed[0]["kwargs"]["error"] == "admission_slot_missing"
