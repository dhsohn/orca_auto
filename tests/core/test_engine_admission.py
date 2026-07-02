from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.queue.engine import admission as engine_admission


def test_start_engine_child_process_can_include_or_omit_admission_root(tmp_path: Path) -> None:
    entry = SimpleNamespace(queue_id="queue-1")
    commands: list[dict[str, Any]] = []

    def build_command(**kwargs: Any) -> list[str]:
        commands.append(kwargs)
        return ["python", "-m", "worker"]

    assert engine_admission.start_engine_child_process(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_root="/tmp/admission",
        admission_token="slot-1",
        start_background_process_fn=lambda command: command,
        build_worker_child_command_fn=build_command,
        include_admission_root=True,
    ) == ["python", "-m", "worker"]

    assert engine_admission.start_engine_child_process(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        entry=entry,
        admission_root="/tmp/admission",
        admission_token="slot-2",
        start_background_process_fn=lambda command: command,
        build_worker_child_command_fn=build_command,
        include_admission_root=False,
    ) == ["python", "-m", "worker"]

    assert commands == [
        {
            "config_path": "/tmp/orca_auto.yaml",
            "queue_root": tmp_path / "queue",
            "queue_id": "queue-1",
            "admission_token": "slot-1",
            "admission_root": "/tmp/admission",
        },
        {
            "config_path": "/tmp/orca_auto.yaml",
            "queue_root": tmp_path / "queue",
            "queue_id": "queue-1",
            "admission_token": "slot-2",
        },
    ]


def test_attach_started_process_records_owner_and_marks_missing_slot(tmp_path: Path) -> None:
    entry = SimpleNamespace(queue_id="queue-1", metadata={"job_dir": str(tmp_path / "job")})
    process = SimpleNamespace(pid=321)
    activated: list[dict[str, Any]] = []

    def activate_reserved_slot(root: str, token: str, **kwargs: Any) -> object:
        activated.append({"root": root, "token": token, **kwargs})
        return object()

    assert engine_admission.attach_started_process(
        admission_root="/tmp/admission",
        queue_root=tmp_path / "queue",
        entry=entry,
        process=process,
        admission_token="slot-1",
        activate_reserved_slot_fn=activate_reserved_slot,
        terminate_process_fn=lambda _process: None,
        mark_entry_failed_and_release_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
        source="source",
    )

    assert activated == [
        {
            "root": "/tmp/admission",
            "token": "slot-1",
            "owner_pid": 321,
            "source": "source",
            "queue_id": "queue-1",
            "work_dir": str(tmp_path / "job"),
        }
    ]

    terminated: list[Any] = []
    failed: list[dict[str, Any]] = []
    assert not engine_admission.attach_started_process(
        admission_root="/tmp/admission",
        queue_root=tmp_path / "queue",
        entry=entry,
        process=process,
        admission_token="slot-2",
        activate_reserved_slot_fn=lambda *args, **kwargs: None,
        terminate_process_fn=terminated.append,
        mark_entry_failed_and_release_fn=lambda *args, **kwargs: failed.append(
            {"args": args, "kwargs": kwargs}
        ),
        mark_failed_fn=lambda *args, **kwargs: None,
        source="source",
    )

    assert terminated == [process]
    assert failed[0]["args"] == (tmp_path / "queue", entry, "slot-2")
    assert failed[0]["kwargs"]["error"] == "admission_slot_missing"


def test_attach_started_process_accepts_reaction_dir_work_dir(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "orca-rxn"
    entry = SimpleNamespace(queue_id="queue-1", metadata={"reaction_dir": str(reaction_dir)})
    process = SimpleNamespace(pid=654)
    activated: list[dict[str, Any]] = []

    def activate_reserved_slot(root: str, token: str, **kwargs: Any) -> object:
        activated.append({"root": root, "token": token, **kwargs})
        return object()

    assert engine_admission.queue_entry_work_dir(entry) == str(reaction_dir)
    assert engine_admission.attach_started_process(
        admission_root="/tmp/admission",
        queue_root=tmp_path / "queue",
        entry=entry,
        process=process,
        admission_token="slot-1",
        activate_reserved_slot_fn=activate_reserved_slot,
        terminate_process_fn=lambda _process: None,
        mark_entry_failed_and_release_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
        source="source",
    )

    assert activated[0]["work_dir"] == str(reaction_dir)


def test_attach_started_process_metadata_updates_identity_and_running_record(
    tmp_path: Path,
) -> None:
    cfg = object()
    entry = SimpleNamespace(
        queue_id="queue-1",
        app_name="orca_auto_orca",
        task_id="task-1",
        metadata={"reaction_dir": str(tmp_path / "rxn")},
    )
    process = SimpleNamespace(pid=321)
    updated: list[dict[str, Any]] = []
    running: list[tuple[object, object]] = []

    def update_slot_metadata(root: str, token: str, **kwargs: Any) -> bool:
        updated.append({"root": root, "token": token, **kwargs})
        return True

    assert engine_admission.attach_started_process_metadata(
        admission_root="/tmp/admission",
        queue_root=tmp_path / "queue",
        entry=entry,
        process=process,
        admission_token="slot-1",
        queue_entry_id_fn=lambda current: current.queue_id,
        queue_entry_app_name_fn=lambda current: current.app_name,
        queue_entry_task_id_fn=lambda current: current.task_id,
        update_slot_metadata_fn=update_slot_metadata,
        terminate_process_fn=lambda _process: None,
        mark_entry_failed_and_release_fn=lambda *args, **kwargs: None,
        mark_failed_fn=lambda *args, **kwargs: None,
        cfg=cfg,
        upsert_running_job_record_fn=lambda cfg_obj, current: running.append((cfg_obj, current)),
    )

    assert updated == [
        {
            "root": "/tmp/admission",
            "token": "slot-1",
            "state": "active",
            "queue_id": "queue-1",
            "app_name": "orca_auto_orca",
            "task_id": "task-1",
            "owner_pid": 321,
            "work_dir": str(tmp_path / "rxn"),
        }
    ]
    assert running == [(cfg, entry)]
