from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.flow.engines.xtb import queue_runtime_terminal


def _callbacks(**overrides: Any) -> queue_runtime_terminal.XtbQueueRuntimeTerminalCallbacks:
    values: dict[str, Any] = {
        "queue_terminal": SimpleNamespace(
            load_terminal_summary=lambda *args, **kwargs: ("summary", args, kwargs),
            ensure_terminal_queue_status=lambda *args, **kwargs: None,
            finalize_execution_result=lambda *args, **kwargs: ("outcome", args, kwargs),
        ),
        "queue_lifecycle": SimpleNamespace(
            sync_terminal_running_entries=lambda *args, **kwargs: ("sync", args, kwargs),
            live_worker_pid_slots=lambda *args, **kwargs: ["live-slot"],
        ),
        "worker_execution_outcome_cls": object,
        "job_dir": lambda entry: Path(entry.job_dir),
        "selected_xyz": lambda entry: Path(entry.selected_xyz),
        "queue_entry_by_id": lambda *_args: None,
        "write_execution_artifacts": lambda *_args, **_kwargs: None,
        "load_terminal_summary_fn": lambda *_args, **_kwargs: "summary",
        "ensure_terminal_queue_status_fn": lambda *_args, **_kwargs: None,
        "print_terminal_summary_fn": lambda _summary: None,
        "live_worker_pid_slots_fn": lambda _worker: ["live-slot"],
        "pid_is_alive": lambda _pid: True,
        "queue_entries_with_roots": lambda _cfg: [],
        "list_slots": lambda _root: ["slot"],
        "load_state": lambda _job_dir: None,
        "mark_completed": lambda *_args, **_kwargs: None,
        "mark_cancelled": lambda *_args, **_kwargs: None,
        "mark_failed": lambda *_args, **_kwargs: None,
        "upsert_job_record": lambda *_args, **_kwargs: None,
        "notify_job_finished": lambda *_args, **_kwargs: None,
    }
    values.update(overrides)
    return queue_runtime_terminal.XtbQueueRuntimeTerminalCallbacks(**values)


def test_load_terminal_summary_uses_callbacks(tmp_path: Path) -> None:
    entry = SimpleNamespace(job_dir=str(tmp_path / "job"), selected_xyz=str(tmp_path / "x.xyz"))
    callbacks = _callbacks()

    result = queue_runtime_terminal.load_terminal_summary(
        callbacks,
        tmp_path / "queue",
        entry,
        rc=3,
    )

    assert result[0] == "summary"
    assert result[1] == (tmp_path / "queue", entry)
    assert result[2]["rc"] == 3
    assert result[2]["job_dir_fn"](entry) == tmp_path / "job"
    assert result[2]["queue_entry_by_id_fn"]("root", "queue-1") is None
    assert "load_report_json_fn" not in result[2]


def test_finalize_execution_result_uses_callbacks(tmp_path: Path) -> None:
    entry = SimpleNamespace(job_dir=str(tmp_path / "job"), selected_xyz=str(tmp_path / "x.xyz"))
    callbacks = _callbacks(worker_execution_outcome_cls=SimpleNamespace)

    result = queue_runtime_terminal.finalize_execution_result(
        callbacks,
        "cfg",
        queue_root=tmp_path / "queue",
        entry=entry,
        result="run-result",
        emit_output=True,
        previous_state={"status": "running"},
        resumed=True,
    )

    assert result[0] == "outcome"
    assert result[1] == ("cfg",)
    kwargs = result[2]
    assert kwargs["queue_root"] == tmp_path / "queue"
    assert kwargs["entry"] is entry
    assert kwargs["result"] == "run-result"
    assert kwargs["outcome_cls"] is SimpleNamespace
    assert kwargs["selected_xyz_fn"](entry) == tmp_path / "x.xyz"


def test_finalize_completed_job_releases_slot(tmp_path: Path) -> None:
    events: list[Any] = []

    def load_summary(*args: Any, **kwargs: Any) -> str:
        events.append(("load", args, kwargs))
        return "summary"

    callbacks = _callbacks(
        load_terminal_summary_fn=load_summary,
        ensure_terminal_queue_status_fn=lambda *args: events.append(("ensure", args)),
        print_terminal_summary_fn=lambda summary: events.append(("print", summary)),
    )
    worker = SimpleNamespace(
        _release_admission_slot=lambda token: events.append(("release", token))
    )
    job = SimpleNamespace(
        queue_root=tmp_path / "queue",
        entry=SimpleNamespace(queue_id="queue-1"),
        admission_token="slot-1",
    )

    queue_runtime_terminal.finalize_completed_job(callbacks, worker, "queue-1", job, 5)

    assert events == [
        ("load", (tmp_path / "queue", job.entry), {"rc": 5}),
        ("ensure", (tmp_path / "queue", job.entry, "summary")),
        ("print", "summary"),
        ("release", "slot-1"),
    ]


def test_list_slots_preserving_live_worker_pids_combines_slot_sources() -> None:
    callbacks = _callbacks(
        list_slots=lambda root: [("slot", root)],
        live_worker_pid_slots_fn=lambda worker: [("live", worker.cfg)],
    )
    worker = SimpleNamespace(cfg="cfg")

    assert queue_runtime_terminal.list_slots_preserving_live_worker_pids(
        callbacks,
        worker,
        "/tmp/admission",
    ) == [
        ("slot", "/tmp/admission"),
        ("live", "cfg"),
    ]
