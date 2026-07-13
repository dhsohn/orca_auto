from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.core.commands import queue as shared_queue_cmd
from orca_auto.core.queue.cancellable import ProcessCleanupError
from orca_auto.flow.engines.xtb import execution as worker_execution_mod
from orca_auto.flow.engines.xtb import queue_runtime as queue_cmd
from orca_auto.flow.engines.xtb import state as state_mod
from tests.flow.engines.xtb.factories import (
    make_cfg as _make_cfg,
)
from tests.flow.engines.xtb.factories import (
    make_entry as _make_entry,
)
from tests.flow.engines.xtb.factories import (
    make_result as _make_result,
)


def _record_committed_terminal(
    calls: list[tuple[object, object, object | None]],
    root: object,
    queue_id: object,
    metadata_update: object | None = None,
    **kwargs: object,
) -> bool:
    before_update_fn = kwargs.get("before_update_fn")
    if callable(before_update_fn):
        before_update_fn()
    calls.append((root, queue_id, metadata_update))
    return True


def _commit_terminal(*args: object, **kwargs: object) -> bool:
    before_update_fn = kwargs.get("before_update_fn")
    if callable(before_update_fn):
        before_update_fn()
    return True


def _record_committed_terminal_error(
    calls: list[tuple[object, object, object, object | None]],
    root: object,
    queue_id: object,
    error: object,
    metadata_update: object | None = None,
    **kwargs: object,
) -> bool:
    _commit_terminal(root, queue_id, **kwargs)
    calls.append((root, queue_id, error, metadata_update))
    return True


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            SimpleNamespace(
                cancel_requested=True,
                status=SimpleNamespace(value="running"),
            ),
            "cancel_requested",
        ),
        (
            SimpleNamespace(
                cancel_requested=False,
                status=SimpleNamespace(value=" "),
            ),
            "unknown",
        ),
    ],
)
def test_queue_display_status(entry: object, expected: str) -> None:
    assert shared_queue_cmd.display_status(entry) == expected


def test_execute_queue_entry_processes_completed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "reactant.xyz"
    selected_xyz.write_text("3\nreactant\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(
        job_dir,
        selected_xyz,
        input_summary={"candidate_count": 1, "candidate_paths": [str(selected_xyz)]},
    )
    result = _make_result(
        selected_xyz, status="completed", reason="xtb_ok", candidate_paths=(str(selected_xyz),)
    )

    completed_calls: list[tuple[object, object, object | None]] = []

    monkeypatch.setattr(
        queue_cmd,
        "start_xtb_job",
        lambda _cfg, *, job_dir, selected_input_xyz, execution_snapshot: SimpleNamespace(
            process=SimpleNamespace(poll=lambda: 0)
        ),
    )
    monkeypatch.setattr(queue_cmd, "finalize_xtb_job", lambda running, **kwargs: result)
    monkeypatch.setattr(
        queue_cmd,
        "mark_completed",
        lambda root, queue_id, metadata_update=None, **kwargs: _record_committed_terminal(
            completed_calls, root, queue_id, metadata_update, **kwargs
        ),
    )
    monkeypatch.setattr(
        queue_cmd, "mark_failed", lambda *args, **kwargs: pytest.fail("unexpected failure mark")
    )
    monkeypatch.setattr(
        queue_cmd, "mark_cancelled", lambda *args, **kwargs: pytest.fail("unexpected cancel mark")
    )
    monkeypatch.setattr(queue_cmd, "notify_job_started", lambda *args, **kwargs: True)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", lambda *args, **kwargs: True)

    outcome = queue_cmd._execute_queue_entry(
        cfg,
        queue_root=queue_root,
        entry=entry,
    )

    assert outcome.result.status == "completed"
    assert completed_calls == [
        (
            cfg.runtime.allowed_root,
            "queue-1",
            {"candidate_count": 1},
        )
    ]

    state = state_mod.load_state(job_dir)
    report = state_mod.load_report_json(job_dir)
    assert state is not None
    assert report is not None
    assert state["status"]["state"] == "completed"
    assert report["status"]["reason"] == "xtb_ok"
    assert report["engine_payload"]["selected_candidate_paths"] == [str(selected_xyz.resolve())]


def test_execute_queue_entry_marks_runner_errors_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "reactant.xyz"
    selected_xyz.write_text("3\nreactant\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)

    failed_calls: list[tuple[object, object, object, object | None]] = []

    monkeypatch.setattr(
        queue_cmd,
        "start_xtb_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_completed",
        lambda *args, **kwargs: pytest.fail("unexpected completed mark"),
    )
    monkeypatch.setattr(
        queue_cmd, "mark_cancelled", lambda *args, **kwargs: pytest.fail("unexpected cancel mark")
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_failed",
        lambda root, queue_id, error, metadata_update=None, **kwargs: (
            _record_committed_terminal_error(
                failed_calls, root, queue_id, error, metadata_update, **kwargs
            )
        ),
    )
    monkeypatch.setattr(queue_cmd, "notify_job_started", lambda *args, **kwargs: True)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", lambda *args, **kwargs: True)

    outcome = queue_cmd._execute_queue_entry(cfg, queue_root=queue_root, entry=entry)

    assert outcome.result.status == "failed"
    assert outcome.result.reason == "runner_error:boom"
    assert failed_calls == [
        (
            cfg.runtime.allowed_root,
            "queue-1",
            "runner_error:boom",
            {"candidate_count": 0},
        )
    ]

    state = state_mod.load_state(job_dir)
    report = state_mod.load_report_json(job_dir)
    assert state is not None
    assert report is not None
    assert state["status"]["state"] == "failed"
    assert report["status"]["reason"] == "runner_error:boom"


def test_execute_queue_entry_cancels_running_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "reactant.xyz"
    selected_xyz.write_text("3\nreactant\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)
    result = _make_result(selected_xyz, status="cancelled", reason="cancel_requested")

    cancelled_calls: list[tuple[object, object, object, object | None]] = []
    finalize_calls: list[tuple[object | None, object | None]] = []

    class _Process:
        pid = 12345
        exited = False

        def poll(self) -> int | None:
            return -15 if self.exited else None

    terminated: list[_Process] = []

    monkeypatch.setattr(
        queue_cmd,
        "start_xtb_job",
        lambda _cfg, *, job_dir, selected_input_xyz, execution_snapshot: SimpleNamespace(
            process=_Process()
        ),
    )

    def terminate(process: _Process) -> bool:
        terminated.append(process)
        process.exited = True
        return True

    monkeypatch.setattr(queue_cmd, "_terminate_process", terminate)

    def fake_finalize_xtb_job(
        running: object, forced_status: object = None, forced_reason: object = None
    ) -> queue_cmd.XtbRunResult:
        finalize_calls.append((forced_status, forced_reason))
        return result

    monkeypatch.setattr(queue_cmd, "finalize_xtb_job", fake_finalize_xtb_job)
    monkeypatch.setattr(
        queue_cmd,
        "mark_completed",
        lambda *args, **kwargs: pytest.fail("unexpected completed mark"),
    )
    monkeypatch.setattr(
        queue_cmd, "mark_failed", lambda *args, **kwargs: pytest.fail("unexpected failed mark")
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_cancelled",
        lambda root, queue_id, error, metadata_update=None, **kwargs: (
            _record_committed_terminal_error(
                cancelled_calls, root, queue_id, error, metadata_update, **kwargs
            )
        ),
    )
    monkeypatch.setattr(queue_cmd, "notify_job_started", lambda *args, **kwargs: True)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", lambda *args, **kwargs: True)

    cancel_checks = iter([False, True])

    outcome = queue_cmd._execute_queue_entry(
        cfg,
        queue_root=queue_root,
        entry=entry,
        should_cancel=lambda: next(cancel_checks, True),
    )

    assert outcome.result.status == "cancelled"
    assert terminated and terminated[0].pid == 12345
    assert finalize_calls == [("cancelled", "cancel_requested")]
    assert cancelled_calls == [
        (
            cfg.runtime.allowed_root,
            "queue-1",
            "cancel_requested",
            {"candidate_count": 0},
        )
    ]

    state = state_mod.load_state(job_dir)
    report = state_mod.load_report_json(job_dir)
    assert state is not None
    assert report is not None
    assert state["status"]["state"] == "cancelled"
    assert report["status"]["reason"] == "cancel_requested"


def test_execute_queue_entry_cancels_before_start_and_updates_terminal_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "reactant.xyz"
    selected_xyz.write_text("3\nreactant\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(
        job_dir,
        selected_xyz,
        input_summary={"candidate_paths": [str(selected_xyz)]},
    )

    events: list[str] = []
    cancelled_calls: list[tuple[object, object, object, object | None]] = []

    monkeypatch.setattr(
        queue_cmd,
        "start_xtb_job",
        lambda *args, **kwargs: pytest.fail("start_xtb_job should not run"),
    )
    monkeypatch.setattr(
        queue_cmd,
        "finalize_xtb_job",
        lambda *args, **kwargs: pytest.fail("finalize_xtb_job should not run"),
    )
    monkeypatch.setattr(
        queue_cmd,
        "run_xtb_ranking_job",
        lambda *args, **kwargs: pytest.fail("ranking runner should not run"),
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_completed",
        lambda *args, **kwargs: pytest.fail("unexpected completed mark"),
    )
    monkeypatch.setattr(
        queue_cmd, "mark_failed", lambda *args, **kwargs: pytest.fail("unexpected failed mark")
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_cancelled",
        lambda root, queue_id, error, metadata_update=None, **kwargs: (
            _record_committed_terminal_error(
                cancelled_calls, root, queue_id, error, metadata_update, **kwargs
            )
        ),
    )

    def fake_notify_job_started(*args: object, **kwargs: object) -> bool:
        events.append("notify_started")
        return True

    def fake_notify_job_finished(*args: object, **kwargs: object) -> bool:
        events.append(f"notify_finished:{kwargs['status']}")
        return True

    monkeypatch.setattr(queue_cmd, "notify_job_started", fake_notify_job_started)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", fake_notify_job_finished)

    outcome = queue_cmd._execute_queue_entry(
        cfg,
        queue_root=queue_root,
        entry=entry,
        should_cancel=lambda: True,
    )

    assert outcome.result.status == "cancelled"
    assert outcome.result.reason == "cancel_requested"
    assert events == ["notify_started", "notify_finished:cancelled"]
    assert cancelled_calls == [
        (
            cfg.runtime.allowed_root,
            "queue-1",
            "cancel_requested",
            {"candidate_count": 0},
        )
    ]

    state = state_mod.load_state(job_dir)
    report = state_mod.load_report_json(job_dir)
    assert state is not None
    assert report is not None
    assert state["status"]["state"] == "cancelled"
    assert report["status"]["reason"] == "cancel_requested"
    assert report["engine_payload"]["candidate_count"] == 0


def test_process_dequeued_entry_uses_queue_cancel_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "reactant.xyz"
    selected_xyz.write_text("3\nreactant\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)
    cancelled_calls: list[tuple[object, object, object, object | None]] = []

    monkeypatch.setattr(
        queue_cmd,
        "get_cancel_requested",
        lambda _root, _queue_id, **_kwargs: True,
    )
    monkeypatch.setattr(
        queue_cmd,
        "start_xtb_job",
        lambda *args, **kwargs: pytest.fail("xTB job should not start after cancel request"),
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_completed",
        lambda *args, **kwargs: pytest.fail("unexpected completed mark"),
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_failed",
        lambda *args, **kwargs: pytest.fail("unexpected failed mark"),
    )
    monkeypatch.setattr(
        queue_cmd,
        "mark_cancelled",
        lambda root, queue_id, error, metadata_update=None, **_kwargs: cancelled_calls.append(
            (root, queue_id, error, metadata_update)
        ),
    )
    monkeypatch.setattr(queue_cmd, "notify_job_started", lambda *args, **kwargs: True)
    monkeypatch.setattr(queue_cmd, "notify_job_finished", lambda *args, **kwargs: True)

    outcome = worker_execution_mod.process_dequeued_entry(
        cfg,
        entry,
        queue_root=queue_root,
        dependencies=queue_cmd._worker_execution_dependencies(),
    )

    assert outcome.result.status == "cancelled"
    assert outcome.result.reason == "cancel_requested"
    assert cancelled_calls == [
        (
            cfg.runtime.allowed_root,
            "queue-1",
            "cancel_requested",
            {"candidate_count": 0},
        )
    ]


def test_execute_queue_entry_processes_ranking_job_without_auto_organizing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "ranking-job"
    job_dir.mkdir()
    selected_xyz = job_dir / "candidate.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, job_type="ranking", reaction_key="")
    result = _make_result(
        selected_xyz,
        status="completed",
        reason="completed",
        job_type="ranking",
        reaction_key=str(entry.metadata["reaction_key"]),
        candidate_paths=(str(selected_xyz),),
    )
    finished_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        queue_cmd,
        "run_xtb_ranking_job",
        lambda cfg_obj, **kwargs: result,
    )
    monkeypatch.setattr(
        queue_cmd,
        "start_xtb_job",
        lambda *args, **kwargs: pytest.fail("start_xtb_job should not be called for ranking"),
    )
    monkeypatch.setattr(queue_cmd, "mark_completed", _commit_terminal)
    monkeypatch.setattr(queue_cmd, "notify_job_started", lambda *args, **kwargs: True)

    def fake_notify_job_finished(cfg_obj: object, **kwargs: object) -> bool:
        finished_calls.append(kwargs)
        return True

    monkeypatch.setattr(queue_cmd, "notify_job_finished", fake_notify_job_finished)

    outcome = queue_cmd._execute_queue_entry(cfg, queue_root=queue_root, entry=entry)

    assert outcome.result.status == "completed"
    assert finished_calls
    assert "organized_output_dir" not in finished_calls[0]


def test_ranking_cleanup_failure_is_not_converted_to_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "ranking-job"
    job_dir.mkdir()
    selected_xyz = job_dir / "candidate.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, job_type="ranking", reaction_key="")

    monkeypatch.setattr(
        queue_cmd,
        "run_xtb_ranking_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProcessCleanupError("ranking process cleanup failed")
        ),
    )
    monkeypatch.setattr(queue_cmd, "notify_job_started", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        queue_cmd,
        "mark_failed",
        lambda *args, **kwargs: pytest.fail("cleanup failure must not be terminalized here"),
    )

    with pytest.raises(ProcessCleanupError, match="ranking process cleanup failed"):
        queue_cmd._execute_queue_entry(cfg, queue_root=queue_root, entry=entry)
