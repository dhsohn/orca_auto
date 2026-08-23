from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.flow.submitters import orca as orca_submitter
from orca_auto.orca import config as orca_config
from orca_auto.orca import submission as submission_mod
from orca_auto.orca.queue import adapter as queue_adapter


def _queue_entry(
    *,
    queue_id: str = "q_123",
    task_id: str = "orca_job_123",
    status: QueueStatus = QueueStatus.PENDING,
    priority: int = 10,
    reaction_dir: str = "/tmp/rxn",
    cancel_requested: bool = False,
    run_id: str | None = None,
) -> QueueEntry:
    metadata: dict[str, Any] = {"reaction_dir": reaction_dir, "force": False}
    if run_id is not None:
        metadata["run_id"] = run_id
    return QueueEntry(
        queue_id=queue_id,
        app_name="orca_auto_orca",
        task_id=task_id,
        task_kind="orca_run_inp",
        engine="orca",
        status=status,
        priority=priority,
        cancel_requested=cancel_requested,
        metadata=metadata,
    )


def test_submit_reaction_dir_uses_direct_submission_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    reaction_dir = tmp_path / "rxn_input"
    selected_inp = reaction_dir / "job.inp"
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(allowed_root)))
    deps = object()
    entry = _queue_entry(
        queue_id="q_123",
        task_id="orca_job_123",
        priority=12,
        reaction_dir=str(reaction_dir),
    )
    worker_info = SimpleNamespace(
        status="running",
        pid=4321,
        log_file=tmp_path / "worker.log",
        detail="healthy",
    )
    queued_result = SimpleNamespace(
        entry=entry,
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        queue_metadata={"source": "test"},
        worker_info=worker_info,
    )
    captured: dict[str, Any] = {}

    def fake_submit_reaction_dir_to_queue(args: Any) -> Any:
        captured["args"] = args
        context = SimpleNamespace(
            cfg=cfg,
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            allowed_root=allowed_root,
        )
        captured["context"] = context
        captured["deps"] = deps
        return SimpleNamespace(
            status="submitted",
            reason="",
            stderr="",
            context=context,
            queued_result=queued_result,
        )

    monkeypatch.setattr(
        submission_mod,
        "submit_reaction_dir_to_queue",
        fake_submit_reaction_dir_to_queue,
    )

    result = orca_submitter.submit_reaction_dir(
        reaction_dir=str(reaction_dir),
        priority=12,
        config_path=" /tmp/orca.yaml ",
        max_cores=16,
        max_memory_gb=64,
        force=True,
        repo_root=" /tmp/orca_repo ",
    )

    args = captured["args"]
    assert args.config == "/tmp/orca.yaml"
    assert args.path == str(reaction_dir)
    assert args.priority == 12
    assert args.force is True
    assert args.max_cores == 16
    assert args.max_memory_gb == 64
    assert captured["context"].allowed_root == allowed_root
    assert result["status"] == "submitted"
    assert result["queue_id"] == "q_123"
    assert result["job_id"] == "orca_job_123"
    assert result["reaction_dir"] == str(reaction_dir)
    assert result["priority"] == 12
    assert result["command_argv"] == [
        "orca_auto.orca.direct_submit",
        "config=/tmp/orca.yaml",
        f"reaction_dir={reaction_dir}",
        "priority=12",
        "force=True",
    ]
    assert result["parsed_stdout"] == {
        "status": "queued",
        "job_dir": str(reaction_dir),
        "queue_id": "q_123",
        "job_id": "orca_job_123",
        "priority": "12",
        "force": "true",
        "worker": "running",
        "worker_pid": "4321",
        "worker_log": str(tmp_path / "worker.log"),
        "worker_detail": "healthy",
    }
    assert "worker_pid: 4321" in result["stdout"]


def test_workflow_submitter_injects_final_bound_payload_validator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn_input"
    selected_inp = reaction_dir / "job.inp"
    captured: dict[str, Any] = {}

    def fake_submit_reaction_dir_to_queue(args: Any) -> Any:
        captured["args"] = args
        return SimpleNamespace(
            status="failed",
            reason="invalid_submission_target",
            stderr="test stop",
            context=None,
            queued_result=None,
        )

    monkeypatch.setattr(
        submission_mod,
        "submit_reaction_dir_to_queue",
        fake_submit_reaction_dir_to_queue,
    )

    orca_submitter.submit_reaction_dir(
        reaction_dir=str(reaction_dir),
        priority=10,
        config_path="/tmp/orca.yaml",
        expected_selected_inp=str(selected_inp),
        workflow_task_kind="sp",
    )

    args = captured["args"]
    assert args.expected_selected_inp == str(selected_inp)
    assert args.workflow_task_kind == "sp"
    assert callable(args.bound_selected_validator)
    args.bound_selected_validator(
        selected_inp,
        b"! HF TightSCF\n* xyz 0 1\nH 0 0 0\n*\n",
    )
    with pytest.raises(ValueError, match="single-point"):
        args.bound_selected_validator(
            selected_inp,
            b"! HF Opt TightSCF\n* xyz 0 1\nH 0 0 0\n*\n",
        )


def test_submit_reaction_dir_reports_resolution_conflict_and_submission_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn_input"
    monkeypatch.setattr(
        submission_mod,
        "submit_reaction_dir_to_queue",
        lambda _args: SimpleNamespace(
            status="failed",
            reason="invalid_submission_target",
            stderr="failed to resolve ORCA submission target",
            context=None,
            queued_result=None,
        ),
    )

    result = orca_submitter.submit_reaction_dir(
        reaction_dir=str(reaction_dir),
        priority=4,
        config_path="/tmp/orca.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "invalid_submission_target"
    assert result["stderr"] == "failed to resolve ORCA submission target\n"

    context = SimpleNamespace(
        cfg=SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path))),
        reaction_dir=reaction_dir,
        selected_inp=reaction_dir / "job.inp",
        allowed_root=tmp_path,
    )
    monkeypatch.setattr(
        submission_mod,
        "submit_reaction_dir_to_queue",
        lambda _args: SimpleNamespace(
            status="failed",
            reason="submission_conflict",
            stderr="already running",
            context=context,
            queued_result=None,
        ),
    )

    result = orca_submitter.submit_reaction_dir(
        reaction_dir=str(reaction_dir),
        priority=4,
        config_path="/tmp/orca.yaml",
    )

    assert result["status"] == "waiting_for_slot"
    assert result["reason"] == "submission_conflict"
    assert result["returncode"] == 0
    assert result["stderr"] == "already running\n"

    def raise_submission_error(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("queue boom")

    monkeypatch.setattr(
        submission_mod,
        "submit_reaction_dir_to_queue",
        raise_submission_error,
    )
    result = orca_submitter.submit_reaction_dir(
        reaction_dir=str(reaction_dir),
        priority=4,
        config_path="/tmp/orca.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "submission_failed"
    assert result["stderr"] == "RuntimeError: queue boom\n"


@pytest.mark.parametrize(
    ("target", "updated_entry", "expected_status"),
    [
        (
            "orca_job_123",
            _queue_entry(
                queue_id="q_123",
                task_id="orca_job_123",
                status=QueueStatus.RUNNING,
                cancel_requested=True,
                reaction_dir="/tmp/rxn_input",
                run_id="run_123",
            ),
            "cancel_requested",
        ),
        (
            "run_123",
            _queue_entry(
                queue_id="q_123",
                task_id="orca_job_123",
                status=QueueStatus.CANCELLED,
                reaction_dir="/tmp/rxn_input",
                run_id="run_123",
            ),
            "cancelled",
        ),
    ],
)
def test_cancel_target_uses_direct_queue_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    updated_entry: QueueEntry,
    expected_status: str,
) -> None:
    allowed_root = tmp_path / "allowed"
    original_entry = _queue_entry(
        queue_id="q_123",
        task_id="orca_job_123",
        status=QueueStatus.RUNNING,
        reaction_dir="/tmp/rxn_input",
        run_id="run_123",
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        orca_config,
        "load_config",
        lambda _config_path: SimpleNamespace(
            runtime=SimpleNamespace(allowed_root=str(allowed_root))
        ),
    )

    def fake_list_queue(root: Path) -> list[QueueEntry]:
        captured["list_root"] = root
        return [original_entry]

    def fake_cancel(
        root: Path,
        queue_id: str,
        *,
        expected_entry: QueueEntry | None = None,
    ) -> QueueEntry:
        captured["cancel"] = (root, queue_id, expected_entry)
        return updated_entry

    monkeypatch.setattr(queue_adapter, "list_queue", fake_list_queue)
    monkeypatch.setattr(queue_adapter, "cancel", fake_cancel)

    result = orca_submitter.cancel_target(
        target=target,
        config_path=" /tmp/orca.yaml ",
        repo_root=" /tmp/orca_repo ",
    )

    resolved_allowed_root = allowed_root.resolve()
    assert captured["list_root"] == resolved_allowed_root
    assert captured["cancel"] == (resolved_allowed_root, "q_123", original_entry)
    assert result["status"] == expected_status
    assert result["returncode"] == 0
    assert result["queue_id"] == "q_123"
    assert result["job_id"] == "orca_job_123"
    assert result["command_argv"] == [
        "orca_auto.orca.direct_cancel",
        "config=/tmp/orca.yaml",
        f"target={target}",
    ]
    assert result["stdout"] == (f"status: {expected_status}\nqueue_id: q_123\njob_id: orca_job_123")


def test_cancel_target_reports_missing_and_empty_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result = orca_submitter.cancel_target(target=" ", config_path="/tmp/orca.yaml")

    assert result["status"] == "failed"
    assert result["reason"] == ""
    assert result["stderr"] == "queue cancel requires a target\n"

    allowed_root = tmp_path / "allowed"
    monkeypatch.setattr(
        orca_config,
        "load_config",
        lambda _config_path: SimpleNamespace(
            runtime=SimpleNamespace(allowed_root=str(allowed_root))
        ),
    )
    monkeypatch.setattr(queue_adapter, "list_queue", lambda _root: [])

    result = orca_submitter.cancel_target(
        target="missing",
        config_path="/tmp/orca.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "target_not_found"
    assert result["stderr"] == "queue target not found: missing\n"


def test_cancel_target_refuses_foreign_queue_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    foreign = QueueEntry(
        queue_id="q_foreign",
        app_name="orca_auto_xtb",
        task_id="xtb-task",
        task_kind="xtb_sp",
        engine="xtb",
        status=QueueStatus.PENDING,
        metadata={"job_type": "sp", "job_dir": str(tmp_path / "xtb")},
    )
    monkeypatch.setattr(
        orca_config,
        "load_config",
        lambda _config_path: SimpleNamespace(
            runtime=SimpleNamespace(allowed_root=str(allowed_root))
        ),
    )
    monkeypatch.setattr(queue_adapter, "list_queue", lambda _root: [foreign])
    monkeypatch.setattr(
        queue_adapter,
        "cancel",
        lambda *_args: pytest.fail("foreign row must not reach ORCA cancellation"),
    )

    result = orca_submitter.cancel_target(
        target=foreign.queue_id,
        config_path="/tmp/orca.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "target_not_found"


def test_cancel_target_recovers_committed_cancel_after_save_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "allowed"
    reaction_dir = tmp_path / "reaction"
    entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    monkeypatch.setattr(
        orca_config,
        "load_config",
        lambda _config_path: SimpleNamespace(
            runtime=SimpleNamespace(allowed_root=str(allowed_root))
        ),
    )
    real_save = queue_adapter._queue_store.save_entries

    def save_then_raise(root: Path, entries: Any) -> None:
        real_save(root, entries)
        raise OSError("durability barrier failed after commit")

    monkeypatch.setattr(queue_adapter._queue_store, "save_entries", save_then_raise)

    result = orca_submitter.cancel_target(
        target=entry.queue_id,
        config_path="/tmp/orca.yaml",
    )

    assert result["status"] == "cancelled"
    assert result["returncode"] == 0
    [cancelled] = queue_adapter.list_queue(allowed_root)
    assert cancelled.queue_id == entry.queue_id
    assert cancelled.status == QueueStatus.CANCELLED


def test_cancel_target_adopts_only_the_same_generation_cancelled_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancel that reports nothing is adopted only from the identical row.

    `queue_adapter.cancel` returns None when it did not transition the row
    itself. The row may still have been cancelled by a concurrent writer just
    before this call, so cancel_target re-reads it by id — but adopting that
    re-read as success is only safe when it is the same publication generation
    and actually cancelled. A successor generation or a still-live row must
    report already_terminal instead of a false cancellation.
    """
    allowed_root = tmp_path / "allowed"
    reaction_dir = tmp_path / "reaction"
    entry = queue_adapter.enqueue(allowed_root, str(reaction_dir))
    monkeypatch.setattr(
        orca_config,
        "load_config",
        lambda _config_path: SimpleNamespace(
            runtime=SimpleNamespace(allowed_root=str(allowed_root))
        ),
    )
    monkeypatch.setattr(
        queue_adapter,
        "cancel",
        lambda _root, _queue_id, *, expected_entry=None: None,
    )

    def _cancel_with_reread(current: QueueEntry | None) -> dict[str, Any]:
        monkeypatch.setattr(queue_adapter, "get_entry_by_id", lambda _root, _queue_id: current)
        return orca_submitter.cancel_target(
            target=entry.queue_id,
            config_path="/tmp/orca.yaml",
        )

    adopted = _cancel_with_reread(replace(entry, status=QueueStatus.CANCELLED))
    assert adopted["status"] == "cancelled"
    assert adopted["returncode"] == 0
    assert adopted["queue_id"] == entry.queue_id

    for label, current in (
        ("row disappeared", None),
        ("still live", replace(entry, status=QueueStatus.RUNNING)),
        (
            "successor generation",
            replace(entry, status=QueueStatus.CANCELLED, task_id=f"{entry.task_id}_2"),
        ),
        (
            "different row",
            replace(entry, status=QueueStatus.CANCELLED, queue_id=f"{entry.queue_id}_2"),
        ),
    ):
        rejected = _cancel_with_reread(current)
        assert rejected["status"] == "failed", label
        assert rejected["reason"] == "already_terminal", label
        assert rejected["stderr"] == f"queue target already terminal: {entry.queue_id}\n", label
