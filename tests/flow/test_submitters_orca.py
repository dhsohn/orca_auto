from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.core.utils import normalize_text
from orca_auto.flow.submitters import orca as orca_submitter
from orca_auto.flow.submitters.orca_submission import record_submission_outcome
from orca_auto.orca import config as orca_config
from orca_auto.orca.commands import run_inp as run_inp_cmd
from orca_auto.orca.queue import adapter as queue_adapter
from tests.flow.factories import install_orca_timestamps, install_orca_workflow_io


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
        run_inp_cmd,
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


def test_submit_reaction_dir_reports_resolution_conflict_and_submission_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn_input"
    monkeypatch.setattr(
        run_inp_cmd,
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
        run_inp_cmd,
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
        run_inp_cmd,
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


def test_submit_reaction_ts_search_workflow_updates_skip_failure_and_submit_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflow_workspace"
    workflow_root = tmp_path / "workflow_root"
    payload: dict[str, Any] = {
        "workflow_id": "wf_submit",
        "status": "planned",
        "metadata": {},
        "stages": [
            {
                "stage_id": "skip_stage",
                "status": "planned",
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_skip",
                        "priority": 3,
                        "submitter": "orca_auto_orca",
                    },
                    "submission_result": {"status": "submitted"},
                },
            },
            {
                "stage_id": "missing_stage",
                "status": "planned",
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "priority": 4,
                        "submitter": "orca_auto_orca",
                    },
                },
            },
            {
                "stage_id": "conflict_stage",
                "status": "planned",
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_conflict",
                        "priority": "6",
                        "submitter": "orca_auto_orca",
                    },
                },
            },
            {
                "stage_id": "submit_stage",
                "status": "planned",
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_submit",
                        "priority": "8",
                        "submitter": "orca_auto_orca",
                    },
                },
            },
        ],
    }
    saved_payloads, sync_calls = install_orca_workflow_io(
        monkeypatch,
        payload=payload,
        workspace_dir=workspace_dir,
    )
    submit_calls: list[dict[str, Any]] = []
    install_orca_timestamps(
        monkeypatch,
        "2026-04-19T00:00:00+00:00",
        "2026-04-19T00:01:00+00:00",
        "2026-04-19T00:02:00+00:00",
        "2026-04-19T00:03:00+00:00",
    )

    def fake_submit_reaction_dir(**kwargs: Any) -> dict[str, Any]:
        submit_calls.append(kwargs)
        if kwargs["reaction_dir"] == "/tmp/rxn_conflict":
            return {
                "status": "waiting_for_slot",
                "reason": "submission_conflict",
                "returncode": 0,
                "stdout": "",
                "stderr": "already queued\n",
                "parsed_stdout": {},
                "reaction_dir": "/tmp/rxn_conflict",
                "priority": 6,
            }
        return {
            "status": "submitted",
            "returncode": 0,
            "stdout": "status: queued\nqueue_id: q_submit\njob_dir: /tmp/rxn_stdout\n",
            "stderr": "",
            "parsed_stdout": {
                "status": "queued",
                "queue_id": "q_submit",
                "job_dir": "/tmp/rxn_stdout",
            },
            "queue_id": "q_submit",
            "reaction_dir": "/tmp/rxn_stdout",
            "priority": 8,
        }

    monkeypatch.setattr(orca_submitter, "submit_reaction_dir", fake_submit_reaction_dir)

    result = orca_submitter.submit_reaction_ts_search_workflow(
        workflow_target="wf_submit",
        workflow_root=workflow_root,
        orca_config=" /tmp/orca.yaml ",
        orca_repo_root=" /tmp/orca_repo ",
    )

    intent_tokens = [call.pop("submission_intent_token") for call in submit_calls]
    assert all(intent_tokens)
    assert len(set(intent_tokens)) == 2
    conflict_intent, submit_intent = intent_tokens
    assert submit_calls == [
        {
            "reaction_dir": "/tmp/rxn_conflict",
            "priority": 6,
            "config_path": "/tmp/orca.yaml",
            "repo_root": "/tmp/orca_repo",
        },
        {
            "reaction_dir": "/tmp/rxn_submit",
            "priority": 8,
            "config_path": "/tmp/orca.yaml",
            "repo_root": "/tmp/orca_repo",
        },
    ]
    assert result == {
        "workflow_id": "wf_submit",
        "workspace_dir": str(workspace_dir),
        "status": "queued",
        "submitted": [
            {
                "stage_id": "submit_stage",
                "queue_id": "q_submit",
                "reaction_dir": "/tmp/rxn_stdout",
            }
        ],
        "skipped": [
            {"stage_id": "skip_stage", "reason": "already_submitted"},
            {"stage_id": "conflict_stage", "reason": "submission_conflict"},
        ],
        "failed": [{"stage_id": "missing_stage", "reason": "missing_reaction_dir"}],
    }
    assert len(saved_payloads) == 2
    assert len(sync_calls) == 1
    assert sync_calls[0]["workflow_root"] == workflow_root
    assert sync_calls[0]["workspace_dir"] == workspace_dir

    saved_payload = saved_payloads[-1]["payload"]
    skip_stage, missing_stage, conflict_stage, submit_stage = saved_payload["stages"]

    assert missing_stage["status"] == "submission_failed"
    assert missing_stage["metadata"] == {
        "submission_status": "submission_failed",
        "submitted_at": "2026-04-19T00:00:00+00:00",
    }
    assert missing_stage["task"]["status"] == "submission_failed"
    assert missing_stage["task"]["submission_result"] == {
        "status": "failed",
        "reason": "missing_reaction_dir",
        "submitted_at": "2026-04-19T00:00:00+00:00",
    }

    assert conflict_stage["status"] == "planned"
    assert conflict_stage["metadata"] == {
        "submission_intent_token": conflict_intent,
        "submission_status": "waiting_for_slot",
        "submission_deferred_reason": "submission_conflict",
        "last_submission_attempt_at": "2026-04-19T00:01:00+00:00",
    }
    assert conflict_stage["task"]["status"] == "planned"
    assert conflict_stage["task"]["submission_result"]["status"] == "waiting_for_slot"
    assert (
        conflict_stage["task"]["submission_result"]["submitted_at"] == "2026-04-19T00:01:00+00:00"
    )

    assert submit_stage["status"] == "queued"
    assert submit_stage["metadata"] == {
        "queue_id": "q_submit",
        "submission_intent_token": submit_intent,
        "submission_status": "submitted",
        "submitted_at": "2026-04-19T00:02:00+00:00",
    }
    assert submit_stage["task"]["status"] == "submitted"
    assert submit_stage["task"]["submission_result"]["status"] == "submitted"
    assert submit_stage["task"]["submission_result"]["submitted_at"] == "2026-04-19T00:02:00+00:00"

    assert skip_stage["task"]["submission_result"] == {"status": "submitted"}

    assert saved_payload["status"] == "queued"
    assert saved_payload["metadata"]["submission_summary"] == {
        "status": "partially_submitted",
        "submitted_count": 1,
        "skipped_count": 2,
        "failed_count": 1,
        "stage_results": [
            {
                "stage_id": "skip_stage",
                "status": "skipped",
                "reason": "already_submitted",
            },
            {
                "stage_id": "missing_stage",
                "status": "submission_failed",
                "reason": "missing_reaction_dir",
            },
            {
                "stage_id": "conflict_stage",
                "status": "waiting_for_slot",
                "reason": "submission_conflict",
                "returncode": 0,
            },
            {
                "stage_id": "submit_stage",
                "status": "submitted",
                "queue_id": "q_submit",
                "returncode": 0,
            },
        ],
        "updated_at": "2026-04-19T00:03:00+00:00",
    }


def test_cancel_reaction_ts_search_workflow_handles_local_cancel_and_config_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflow_workspace"
    workflow_root = tmp_path / "workflow_root"
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_local",
        "status": "queued",
        "metadata": {},
        "stages": [
            {
                "stage_id": "local_stage",
                "status": "planned",
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "submitter": "orca_auto_orca",
                    },
                },
            },
            {
                "stage_id": "needs_config_stage",
                "status": "queued",
                "task": {
                    "status": "submitted",
                    "payload": {"reaction_dir": "/tmp/rxn_needs_config"},
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_needs_config",
                        "submitter": "orca_auto_orca",
                    },
                },
            },
            {
                "stage_id": "skip_cancelled_stage",
                "status": "cancelled",
                "task": {
                    "status": "cancelled",
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_cancelled",
                        "submitter": "orca_auto_orca",
                    },
                },
            },
        ],
    }
    saved_payloads, sync_calls = install_orca_workflow_io(
        monkeypatch,
        payload=payload,
        workspace_dir=workspace_dir,
    )
    install_orca_timestamps(
        monkeypatch,
        "2026-04-19T00:10:00+00:00",
        "2026-04-19T00:11:00+00:00",
        "2026-04-19T00:12:00+00:00",
    )
    monkeypatch.setattr(
        orca_submitter,
        "cancel_target",
        lambda **kwargs: pytest.fail("cancel_target should not run without config"),
    )

    result = orca_submitter.cancel_reaction_ts_search_workflow(
        workflow_target="wf_cancel_local",
        workflow_root=workflow_root,
        orca_config=None,
    )

    assert result == {
        "workflow_id": "wf_cancel_local",
        "workspace_dir": str(workspace_dir),
        "status": "cancelled",
        "cancelled": [{"stage_id": "local_stage", "mode": "local"}],
        "requested": [],
        "skipped": [{"stage_id": "skip_cancelled_stage", "reason": "already_cancelled"}],
        "failed": [{"stage_id": "needs_config_stage", "reason": "orca_config_required"}],
    }
    assert len(saved_payloads) == 1
    assert len(sync_calls) == 1

    saved_payload = saved_payloads[0]["payload"]
    local_stage, needs_config_stage, skip_cancelled_stage = saved_payload["stages"]

    assert local_stage["status"] == "cancelled"
    assert local_stage["metadata"] == {
        "cancel_status": "cancelled",
        "cancelled_at": "2026-04-19T00:10:00+00:00",
    }
    assert local_stage["task"]["status"] == "cancelled"
    assert local_stage["task"]["cancel_result"] == {
        "status": "cancelled",
        "cancelled_at": "2026-04-19T00:10:00+00:00",
        "mode": "local",
    }

    assert needs_config_stage["status"] == "queued"
    assert needs_config_stage["metadata"] == {}
    assert needs_config_stage["task"]["status"] == "submitted"
    assert needs_config_stage["task"]["cancel_result"] == {
        "status": "failed",
        "reason": "orca_config_required",
        "cancelled_at": "2026-04-19T00:11:00+00:00",
    }

    assert skip_cancelled_stage["status"] == "cancelled"
    assert skip_cancelled_stage["task"]["status"] == "cancelled"

    assert saved_payload["status"] == "cancelled"
    assert saved_payload["metadata"]["cancellation_summary"] == {
        "cancelled_count": 1,
        "requested_count": 0,
        "skipped_count": 1,
        "failed_count": 1,
        "stage_results": [
            {"stage_id": "local_stage", "status": "cancelled", "mode": "local"},
            {
                "stage_id": "needs_config_stage",
                "status": "cancel_failed",
                "reason": "orca_config_required",
            },
            {
                "stage_id": "skip_cancelled_stage",
                "status": "skipped",
                "reason": "already_cancelled",
            },
        ],
        "updated_at": "2026-04-19T00:12:00+00:00",
    }


def test_cancel_reaction_ts_search_workflow_records_requested_and_cancelled_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflow_workspace"
    workflow_root = tmp_path / "workflow_root"
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_remote",
        "status": "running",
        "metadata": {},
        "stages": [
            {
                "stage_id": "request_stage",
                "status": "running",
                "metadata": {"queue_id": "q_request"},
                "task": {
                    "status": "submitted",
                    "payload": {"reaction_dir": "/tmp/rxn_request"},
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_request",
                        "submitter": "orca_auto_orca",
                    },
                },
            },
            {
                "stage_id": "cancel_stage",
                "status": "queued",
                "task": {
                    "status": "submitted",
                    "payload": {"reaction_dir": "/tmp/rxn_cancel"},
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_cancel",
                        "submitter": "orca_auto_orca",
                    },
                },
            },
        ],
    }
    saved_payloads, sync_calls = install_orca_workflow_io(
        monkeypatch,
        payload=payload,
        workspace_dir=workspace_dir,
    )
    cancel_calls: list[dict[str, Any]] = []
    cancel_responses = iter(
        [
            {
                "status": "cancel_requested",
                "returncode": 0,
                "stdout": "Cancel requested for q_request\n",
                "stderr": "",
                "command_argv": ["orca_auto_orca_bin", "queue", "cancel", "q_request"],
            },
            {
                "status": "cancelled",
                "returncode": 0,
                "stdout": "Cancelled: /tmp/rxn_cancel\n",
                "stderr": "",
                "command_argv": ["orca_auto_orca_bin", "queue", "cancel", "/tmp/rxn_cancel"],
            },
        ]
    )

    install_orca_timestamps(
        monkeypatch,
        "2026-04-19T00:20:00+00:00",
        "2026-04-19T00:21:00+00:00",
        "2026-04-19T00:22:00+00:00",
    )

    def fake_cancel_target(**kwargs: Any) -> dict[str, Any]:
        cancel_calls.append(kwargs)
        return dict(next(cancel_responses))

    monkeypatch.setattr(orca_submitter, "cancel_target", fake_cancel_target)

    result = orca_submitter.cancel_reaction_ts_search_workflow(
        workflow_target="wf_cancel_remote",
        workflow_root=workflow_root,
        orca_config=" /tmp/orca.yaml ",
        orca_repo_root=" /tmp/orca_repo ",
    )

    assert cancel_calls == [
        {
            "target": "q_request",
            "config_path": "/tmp/orca.yaml",
            "repo_root": "/tmp/orca_repo",
        },
        {
            "target": "/tmp/rxn_cancel",
            "config_path": "/tmp/orca.yaml",
            "repo_root": "/tmp/orca_repo",
        },
    ]
    assert result == {
        "workflow_id": "wf_cancel_remote",
        "workspace_dir": str(workspace_dir),
        "status": "cancel_requested",
        "cancelled": [
            {
                "stage_id": "cancel_stage",
                "queue_id": "",
                "reaction_dir": "/tmp/rxn_cancel",
            }
        ],
        "requested": [
            {
                "stage_id": "request_stage",
                "queue_id": "q_request",
                "reaction_dir": "/tmp/rxn_request",
            }
        ],
        "skipped": [],
        "failed": [],
    }
    assert len(saved_payloads) == 1
    assert len(sync_calls) == 1

    saved_payload = saved_payloads[0]["payload"]
    request_stage, cancel_stage = saved_payload["stages"]

    assert request_stage["status"] == "cancel_requested"
    assert request_stage["metadata"] == {
        "queue_id": "q_request",
        "cancel_status": "cancel_requested",
        "cancelled_at": "2026-04-19T00:20:00+00:00",
    }
    assert request_stage["task"]["status"] == "cancel_requested"
    assert request_stage["task"]["cancel_result"] == {
        "status": "cancel_requested",
        "returncode": 0,
        "stdout": "Cancel requested for q_request\n",
        "stderr": "",
        "command_argv": ["orca_auto_orca_bin", "queue", "cancel", "q_request"],
        "cancelled_at": "2026-04-19T00:20:00+00:00",
        "target": "q_request",
    }

    assert cancel_stage["status"] == "cancelled"
    assert cancel_stage["metadata"] == {
        "cancel_status": "cancelled",
        "cancelled_at": "2026-04-19T00:21:00+00:00",
    }
    assert cancel_stage["task"]["status"] == "cancelled"
    assert cancel_stage["task"]["cancel_result"] == {
        "status": "cancelled",
        "returncode": 0,
        "stdout": "Cancelled: /tmp/rxn_cancel\n",
        "stderr": "",
        "command_argv": ["orca_auto_orca_bin", "queue", "cancel", "/tmp/rxn_cancel"],
        "cancelled_at": "2026-04-19T00:21:00+00:00",
        "target": "/tmp/rxn_cancel",
    }

    assert saved_payload["status"] == "cancel_requested"
    assert saved_payload["metadata"]["cancellation_summary"] == {
        "cancelled_count": 1,
        "requested_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
        "stage_results": [
            {"stage_id": "request_stage", "status": "cancel_requested"},
            {"stage_id": "cancel_stage", "status": "cancelled"},
        ],
        "updated_at": "2026-04-19T00:22:00+00:00",
    }


def test_submit_workflow_waits_for_prior_writer_before_loading_and_submitting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflow_root" / "wf_lock"
    workflow_root = workspace_dir.parent
    workspace_dir.mkdir(parents=True)
    initial_payload: dict[str, Any] = {
        "workflow_id": "wf_lock",
        "template_name": "reaction_ts_search",
        "status": "planned",
        "metadata": {},
        "stages": [
            {
                "stage_id": "submit_stage",
                "status": "planned",
                "metadata": {},
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_lock",
                        "priority": 7,
                        "submitter": "orca_auto_orca",
                    },
                },
            }
        ],
    }
    orca_submitter.write_workflow_payload(workspace_dir, initial_payload)

    real_acquire_workflow_lock = orca_submitter.acquire_workflow_lock
    writer_locked = Event()
    allow_writer = Event()
    submit_lock_attempted = Event()
    submit_called = Event()
    submit_calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    thread_errors: list[BaseException] = []

    @contextmanager
    def tracked_acquire_workflow_lock(current_workspace_dir: Path) -> Iterator[None]:
        submit_lock_attempted.set()
        with real_acquire_workflow_lock(current_workspace_dir):
            yield

    monkeypatch.setattr(
        orca_submitter,
        "resolve_workflow_workspace",
        lambda *, target, workflow_root: workspace_dir,
    )
    monkeypatch.setattr(
        orca_submitter,
        "acquire_workflow_lock",
        tracked_acquire_workflow_lock,
    )
    monkeypatch.setattr(orca_submitter, "sync_workflow_registry", lambda *_args: None)

    def fake_submit_reaction_dir(**kwargs: Any) -> dict[str, Any]:
        submit_calls.append(kwargs)
        submit_called.set()
        return {
            "status": "submitted",
            "returncode": 0,
            "stdout": "status: queued\nqueue_id: q_lock\njob_dir: /tmp/rxn_lock\n",
            "stderr": "",
            "parsed_stdout": {
                "status": "queued",
                "queue_id": "q_lock",
                "job_dir": "/tmp/rxn_lock",
            },
            "queue_id": "q_lock",
            "reaction_dir": "/tmp/rxn_lock",
            "priority": 7,
        }

    monkeypatch.setattr(orca_submitter, "submit_reaction_dir", fake_submit_reaction_dir)

    def run_prior_writer() -> None:
        try:
            with real_acquire_workflow_lock(workspace_dir):
                writer_locked.set()
                if not allow_writer.wait(timeout=5):
                    raise TimeoutError("test did not release prior workflow writer")
                payload = orca_submitter.load_workflow_payload(workspace_dir)
                payload["stages"].append(
                    {
                        "stage_id": "writer_stage",
                        "status": "queued",
                        "metadata": {"queue_id": "q_writer", "writer_revision": 1},
                        "task": None,
                    }
                )
                orca_submitter.write_workflow_payload(workspace_dir, payload)
        except BaseException as exc:  # noqa: BLE001
            thread_errors.append(exc)

    def run_submission() -> None:
        try:
            results.append(
                orca_submitter.submit_reaction_ts_search_workflow(
                    workflow_target="wf_lock",
                    workflow_root=workflow_root,
                    orca_config="/tmp/orca.yaml",
                )
            )
        except BaseException as exc:  # noqa: BLE001
            thread_errors.append(exc)

    writer_thread = Thread(target=run_prior_writer)
    writer_thread.start()
    assert writer_locked.wait(timeout=5)

    submission_thread = Thread(target=run_submission)
    submission_thread.start()
    assert submit_lock_attempted.wait(timeout=5)
    assert not submit_called.is_set()

    allow_writer.set()
    writer_thread.join(timeout=5)
    submission_thread.join(timeout=5)
    assert not writer_thread.is_alive()
    assert not submission_thread.is_alive()
    assert thread_errors == []

    final_payload = orca_submitter.load_workflow_payload(workspace_dir)
    stages = {stage["stage_id"]: stage for stage in final_payload["stages"]}
    assert set(stages) == {"submit_stage", "writer_stage"}
    assert stages["writer_stage"] == {
        "stage_id": "writer_stage",
        "status": "queued",
        "metadata": {"queue_id": "q_writer", "writer_revision": 1},
        "task": None,
    }
    assert stages["submit_stage"]["status"] == "queued"
    assert stages["submit_stage"]["metadata"]["queue_id"] == "q_lock"
    assert stages["submit_stage"]["task"]["submission_result"]["queue_id"] == "q_lock"
    [intent_token] = [call.pop("submission_intent_token") for call in submit_calls]
    assert intent_token == stages["submit_stage"]["metadata"]["submission_intent_token"]
    assert submit_calls == [
        {
            "reaction_dir": "/tmp/rxn_lock",
            "priority": 7,
            "config_path": "/tmp/orca.yaml",
            "repo_root": None,
        }
    ]
    assert len(results) == 1


def test_cancel_workflow_holds_lock_through_external_cancel_and_registry_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflow_workspace"
    workflow_root = tmp_path / "workflow_root"
    payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_lock",
        "status": "queued",
        "metadata": {},
        "stages": [
            {
                "stage_id": "cancel_stage",
                "status": "queued",
                "metadata": {"queue_id": "q_cancel_lock"},
                "task": {
                    "status": "submitted",
                    "enqueue_payload": {
                        "reaction_dir": "/tmp/rxn_cancel_lock",
                        "submitter": "orca_auto_orca",
                    },
                },
            }
        ],
    }
    events: list[str] = []
    lock_held = False

    @contextmanager
    def tracked_acquire_workflow_lock(current_workspace_dir: Path) -> Iterator[None]:
        nonlocal lock_held
        assert current_workspace_dir == workspace_dir
        events.append("lock_enter")
        lock_held = True
        try:
            yield
        finally:
            lock_held = False
            events.append("lock_exit")

    def fake_load_workflow_payload(current_workspace_dir: Path) -> dict[str, Any]:
        assert current_workspace_dir == workspace_dir
        assert lock_held
        events.append("load")
        return payload

    def fake_cancel_target(**_kwargs: Any) -> dict[str, Any]:
        assert lock_held
        events.append("cancel")
        return {"status": "cancelled", "returncode": 0}

    def fake_write_workflow_payload(
        current_workspace_dir: Path,
        current_payload: dict[str, Any],
    ) -> None:
        assert current_workspace_dir == workspace_dir
        assert current_payload is payload
        assert lock_held
        events.append("write")

    def fake_sync_workflow_registry(
        current_workflow_root: str | Path,
        current_workspace_dir: Path,
        current_payload: dict[str, Any],
    ) -> None:
        assert current_workflow_root == workflow_root
        assert current_workspace_dir == workspace_dir
        assert current_payload is payload
        assert lock_held
        events.append("sync")

    monkeypatch.setattr(
        orca_submitter,
        "resolve_workflow_workspace",
        lambda *, target, workflow_root: workspace_dir,
    )
    monkeypatch.setattr(
        orca_submitter,
        "acquire_workflow_lock",
        tracked_acquire_workflow_lock,
    )
    monkeypatch.setattr(orca_submitter, "load_workflow_payload", fake_load_workflow_payload)
    monkeypatch.setattr(orca_submitter, "cancel_target", fake_cancel_target)
    monkeypatch.setattr(orca_submitter, "write_workflow_payload", fake_write_workflow_payload)
    monkeypatch.setattr(orca_submitter, "sync_workflow_registry", fake_sync_workflow_registry)
    install_orca_timestamps(
        monkeypatch,
        "2026-04-19T00:30:00+00:00",
        "2026-04-19T00:31:00+00:00",
    )

    result = orca_submitter.cancel_reaction_ts_search_workflow(
        workflow_target="wf_cancel_lock",
        workflow_root=workflow_root,
        orca_config="/tmp/orca.yaml",
    )

    assert events == ["lock_enter", "load", "cancel", "write", "sync", "lock_exit"]
    assert result["status"] == "cancelled"


def _install_real_orca_workflow_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Any, Path, Path]:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "reaction"
    reaction_dir.mkdir(parents=True)
    (reaction_dir / "job.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    fake_orca = tmp_path / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = orca_config.AppConfig(
        runtime=orca_config.RetryRuntimeConfig(allowed_root=str(allowed_root)),
        paths=orca_config.PathsConfig(orca_executable=str(fake_orca)),
        resources=orca_config.CommonResourceConfig(
            max_cores_per_task=2,
            max_memory_gb_per_task=4,
        ),
    )
    monkeypatch.setattr(orca_config, "load_config", lambda _config_path: cfg)
    monkeypatch.setattr(run_inp_cmd, "load_config", lambda _config_path: cfg)
    monkeypatch.setattr(
        run_inp_cmd,
        "notify_queue_enqueued_event",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("orca_auto.orca.queue.worker.read_worker_pid", lambda _root: None)
    return cfg, allowed_root, reaction_dir


@pytest.mark.parametrize(
    ("terminal_before_retry", "force"),
    [(False, False), (True, False), (True, True)],
)
def test_submit_workflow_recovers_exact_queue_row_after_payload_write_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    terminal_before_retry: bool,
    force: bool,
) -> None:
    _cfg, allowed_root, reaction_dir = _install_real_orca_workflow_queue(
        monkeypatch,
        tmp_path,
    )
    workspace_dir = tmp_path / "workflow_root" / "wf_submit_replay"
    workflow_root = workspace_dir.parent
    workspace_dir.mkdir(parents=True)
    initial_payload: dict[str, Any] = {
        "workflow_id": "wf_submit_replay",
        "status": "planned",
        "metadata": {},
        "stages": [
            {
                "stage_id": "orca_stage",
                "status": "planned",
                "metadata": {},
                "task": {
                    "status": "planned",
                    "enqueue_payload": {
                        "reaction_dir": str(reaction_dir),
                        "priority": 7,
                        "submitter": "orca_auto_orca",
                        "force": force,
                    },
                },
            }
        ],
    }
    orca_submitter.write_workflow_payload(workspace_dir, initial_payload)
    real_write_workflow_payload = orca_submitter.write_workflow_payload
    workflow_write_count = 0

    def crash_before_workflow_commit(
        current_workspace_dir: Path,
        current_payload: dict[str, Any],
    ) -> None:
        nonlocal workflow_write_count
        workflow_write_count += 1
        if workflow_write_count == 2:
            raise OSError("simulated workflow payload write crash")
        real_write_workflow_payload(current_workspace_dir, current_payload)

    monkeypatch.setattr(
        orca_submitter,
        "resolve_workflow_workspace",
        lambda *, target, workflow_root: workspace_dir,
    )
    monkeypatch.setattr(
        orca_submitter,
        "write_workflow_payload",
        crash_before_workflow_commit,
    )
    monkeypatch.setattr(orca_submitter, "sync_workflow_registry", lambda *_args: None)

    with pytest.raises(OSError, match="simulated workflow payload write crash"):
        orca_submitter.submit_reaction_ts_search_workflow(
            workflow_target="wf_submit_replay",
            workflow_root=workflow_root,
            orca_config="/tmp/orca.yaml",
        )

    [durable_entry] = queue_adapter.list_queue(allowed_root)
    stale_payload = orca_submitter.load_workflow_payload(workspace_dir)
    assert stale_payload["stages"][0]["status"] == "planned"
    intent_token = stale_payload["stages"][0]["metadata"]["submission_intent_token"]
    assert durable_entry.metadata["submission_intent_token"] == intent_token
    if terminal_before_retry:
        assert queue_adapter.mark_completed(allowed_root, durable_entry.queue_id)
    assert (
        orca_submitter.recover_exact_reaction_dir_submission(
            reaction_dir=str(reaction_dir),
            priority=7,
            config_path="/tmp/orca.yaml",
            submission_intent_token="unrelated-workflow-intent",
        )
        is None
    )
    assert (
        orca_submitter.recover_exact_reaction_dir_submission(
            reaction_dir=str(reaction_dir),
            priority=8,
            config_path="/tmp/orca.yaml",
            submission_intent_token=intent_token,
        )
        is None
    )

    result = orca_submitter.submit_reaction_ts_search_workflow(
        workflow_target="wf_submit_replay",
        workflow_root=workflow_root,
        orca_config="/tmp/orca.yaml",
    )

    [final_entry] = queue_adapter.list_queue(allowed_root)
    assert final_entry.queue_id == durable_entry.queue_id
    assert result["submitted"] == [
        {
            "stage_id": "orca_stage",
            "queue_id": durable_entry.queue_id,
            "reaction_dir": str(reaction_dir.resolve()),
        }
    ]
    final_payload = orca_submitter.load_workflow_payload(workspace_dir)
    final_stage = final_payload["stages"][0]
    assert final_stage["status"] == "queued"
    assert final_stage["metadata"]["queue_id"] == durable_entry.queue_id
    assert final_stage["task"]["submission_result"]["queue_id"] == durable_entry.queue_id
    assert (
        f"recovered exact {'completed' if terminal_before_retry else 'pending'} queue entry"
        in final_stage["task"]["submission_result"]["parsed_stdout"]["worker_detail"]
    )


def test_cancel_workflow_recovers_queue_row_from_submission_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _cfg, allowed_root, reaction_dir = _install_real_orca_workflow_queue(
        monkeypatch,
        tmp_path,
    )
    intent_token = "workflow-intent-after-queue-commit"
    entry = queue_adapter.enqueue(
        allowed_root,
        str(reaction_dir),
        priority=7,
        metadata={
            "submission_intent_token": intent_token,
            "_orca_auto_queued_record_sync": "repair_pending",
            "_orca_auto_queued_record_sync_token": "repair-token",
        },
    )
    workspace_dir = tmp_path / "workflow_root" / "wf_cancel_intent_recovery"
    workspace_dir.mkdir(parents=True)
    orca_submitter.write_workflow_payload(
        workspace_dir,
        {
            "workflow_id": "wf_cancel_intent_recovery",
            "status": "planned",
            "metadata": {},
            "stages": [
                {
                    "stage_id": "orca_stage",
                    "status": "planned",
                    "metadata": {"submission_intent_token": intent_token},
                    "task": {
                        "status": "planned",
                        "enqueue_payload": {
                            "reaction_dir": str(reaction_dir),
                            "priority": 7,
                            "submitter": "orca_auto_orca",
                            "force": False,
                        },
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(
        orca_submitter,
        "resolve_workflow_workspace",
        lambda *, target, workflow_root: workspace_dir,
    )
    monkeypatch.setattr(orca_submitter, "sync_workflow_registry", lambda *_args: None)

    result = orca_submitter.cancel_reaction_ts_search_workflow(
        workflow_target="wf_cancel_intent_recovery",
        workflow_root=workspace_dir.parent,
        orca_config="/tmp/orca.yaml",
    )

    assert result["cancelled"] == [
        {
            "stage_id": "orca_stage",
            "queue_id": entry.queue_id,
            "reaction_dir": str(reaction_dir),
        }
    ]
    [cancelled] = queue_adapter.list_queue(allowed_root)
    assert cancelled.queue_id == entry.queue_id
    assert cancelled.status == QueueStatus.CANCELLED


def test_cancel_workflow_fails_closed_when_submission_intent_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workflow_root" / "wf_cancel_intent_error"
    workspace_dir.mkdir(parents=True)
    orca_submitter.write_workflow_payload(
        workspace_dir,
        {
            "workflow_id": "wf_cancel_intent_error",
            "status": "planned",
            "metadata": {},
            "stages": [
                {
                    "stage_id": "orca_stage",
                    "status": "planned",
                    "metadata": {"submission_intent_token": "intent-token"},
                    "task": {
                        "status": "planned",
                        "enqueue_payload": {
                            "reaction_dir": str(tmp_path / "reaction"),
                            "priority": 7,
                            "submitter": "orca_auto_orca",
                        },
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(
        orca_submitter,
        "resolve_workflow_workspace",
        lambda *, target, workflow_root: workspace_dir,
    )
    monkeypatch.setattr(orca_submitter, "sync_workflow_registry", lambda *_args: None)
    monkeypatch.setattr(
        orca_submitter,
        "recover_exact_reaction_dir_submission",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("transient lookup failure")),
    )
    cancel_calls: list[str] = []
    monkeypatch.setattr(
        orca_submitter,
        "cancel_target",
        lambda **kwargs: cancel_calls.append(str(kwargs["target"])),
    )

    result = orca_submitter.cancel_reaction_ts_search_workflow(
        workflow_target="wf_cancel_intent_error",
        workflow_root=workspace_dir.parent,
        orca_config="/tmp/orca.yaml",
    )

    assert result["status"] == "cancel_failed"
    assert result["cancelled"] == []
    assert result["failed"][0]["reason"] == "submission_intent_lookup_failed"
    assert cancel_calls == []
    stage = orca_submitter.load_workflow_payload(workspace_dir)["stages"][0]
    assert stage["status"] == "planned"
    assert stage["task"]["status"] == "planned"


def test_cancel_workflow_adopts_exact_cancelled_row_after_payload_write_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _cfg, allowed_root, reaction_dir = _install_real_orca_workflow_queue(
        monkeypatch,
        tmp_path,
    )
    entry = queue_adapter.enqueue(
        allowed_root,
        str(reaction_dir),
        priority=5,
    )
    workspace_dir = tmp_path / "workflow_root" / "wf_cancel_replay"
    workflow_root = workspace_dir.parent
    workspace_dir.mkdir(parents=True)
    initial_payload: dict[str, Any] = {
        "workflow_id": "wf_cancel_replay",
        "status": "queued",
        "metadata": {},
        "stages": [
            {
                "stage_id": "orca_stage",
                "status": "queued",
                "metadata": {"queue_id": entry.queue_id},
                "task": {
                    "status": "submitted",
                    "enqueue_payload": {
                        "reaction_dir": str(reaction_dir),
                        "submitter": "orca_auto_orca",
                    },
                },
            }
        ],
    }
    orca_submitter.write_workflow_payload(workspace_dir, initial_payload)
    real_write_workflow_payload = orca_submitter.write_workflow_payload
    fail_next_workflow_write = True

    def crash_before_workflow_commit(
        current_workspace_dir: Path,
        current_payload: dict[str, Any],
    ) -> None:
        nonlocal fail_next_workflow_write
        if fail_next_workflow_write:
            fail_next_workflow_write = False
            raise OSError("simulated cancellation payload write crash")
        real_write_workflow_payload(current_workspace_dir, current_payload)

    monkeypatch.setattr(
        orca_submitter,
        "resolve_workflow_workspace",
        lambda *, target, workflow_root: workspace_dir,
    )
    monkeypatch.setattr(
        orca_submitter,
        "write_workflow_payload",
        crash_before_workflow_commit,
    )
    monkeypatch.setattr(orca_submitter, "sync_workflow_registry", lambda *_args: None)

    with pytest.raises(OSError, match="simulated cancellation payload write crash"):
        orca_submitter.cancel_reaction_ts_search_workflow(
            workflow_target="wf_cancel_replay",
            workflow_root=workflow_root,
            orca_config="/tmp/orca.yaml",
        )

    [cancelled_entry] = queue_adapter.list_queue(allowed_root)
    assert cancelled_entry.status == QueueStatus.CANCELLED
    stale_payload = orca_submitter.load_workflow_payload(workspace_dir)
    assert stale_payload["stages"][0]["status"] == "queued"
    alias_retry = orca_submitter.cancel_target(
        target=str(reaction_dir),
        config_path="/tmp/orca.yaml",
    )
    assert alias_retry["status"] == "cancelled"
    assert alias_retry["queue_id"] == entry.queue_id

    result = orca_submitter.cancel_reaction_ts_search_workflow(
        workflow_target="wf_cancel_replay",
        workflow_root=workflow_root,
        orca_config="/tmp/orca.yaml",
    )

    assert result["status"] == "cancelled"
    assert result["failed"] == []
    assert result["cancelled"] == [
        {
            "stage_id": "orca_stage",
            "queue_id": entry.queue_id,
            "reaction_dir": str(reaction_dir),
        }
    ]
    final_payload = orca_submitter.load_workflow_payload(workspace_dir)
    final_stage = final_payload["stages"][0]
    assert final_stage["status"] == "cancelled"
    assert final_stage["task"]["status"] == "cancelled"
    assert final_stage["task"]["cancel_result"]["status"] == "cancelled"


def test_record_submission_outcome_persists_failure_reason_and_detail() -> None:
    """A rejected queue submission must leave its reason on the stage.

    Regression for the reaction_ts_search run whose three OptTS submissions
    were rejected by the execution-snapshot basename gate with no reason
    recorded anywhere: stage metadata, journal events, and logs all stayed
    silent.
    """
    stage: dict[str, Any] = {"stage_id": "orca_optts_freq_01"}
    task: dict[str, Any] = {}
    stage_metadata: dict[str, Any] = {}
    submission_record: dict[str, Any] = {
        "status": "failed",
        "reason": "invalid_submission_input",
        "returncode": 1,
        "stderr": (
            "ORCA referenced input basename conflicts with a generation "
            "runtime/output file: ts_guess.hess\n"
        ),
        "stdout": "",
        "parsed_stdout": {},
    }

    bucket, detail, stage_result = record_submission_outcome(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        reaction_dir="/tmp/rxn",
        submission_record=submission_record,
        now_utc_iso=lambda: "2026-07-17T00:00:00+00:00",
        normalize_text=normalize_text,
    )

    assert bucket == "failed"
    assert stage["status"] == "submission_failed"
    assert task["status"] == "submission_failed"
    assert stage_metadata["reason"] == "invalid_submission_input"
    assert "ts_guess.hess" in stage_metadata["submission_error_detail"]
    assert stage_result["reason"] == "invalid_submission_input"
    assert detail["reason"] == "invalid_submission_input"


def test_record_submission_outcome_clears_stale_failure_detail_on_submit() -> None:
    """A successful resubmission must clear the previous failure's traces.

    Without this a stage retried after `submission_failed` stays tagged with
    e.g. `reason: invalid_submission_input` through queued/running/completed,
    and the final workflow report surfaces the stale reason because stage
    metadata outranks the live submission result there.
    """
    stage: dict[str, Any] = {"stage_id": "orca_optts_freq_01"}
    task: dict[str, Any] = {}
    stage_metadata: dict[str, Any] = {
        "reason": "invalid_submission_input",
        "submission_error_detail": "old failure detail",
        "submission_deferred_reason": "submission_conflict",
    }
    submission_record: dict[str, Any] = {
        "status": "submitted",
        "returncode": 0,
        "stdout": "status: queued\nqueue_id: q_new\n",
        "stderr": "",
        "parsed_stdout": {"status": "queued", "queue_id": "q_new"},
    }

    bucket, _detail, _stage_result = record_submission_outcome(
        stage=stage,
        task=task,
        stage_metadata=stage_metadata,
        reaction_dir="/tmp/rxn",
        submission_record=submission_record,
        now_utc_iso=lambda: "2026-07-17T00:00:00+00:00",
        normalize_text=normalize_text,
    )

    assert bucket == "submitted"
    assert "reason" not in stage_metadata
    assert "submission_error_detail" not in stage_metadata
    assert "submission_deferred_reason" not in stage_metadata
