from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.flow.submitters import orca as orca_submitter
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

    assert result["status"] == "failed"
    assert result["reason"] == "submission_conflict"
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

    def fake_cancel(root: Path, queue_id: str) -> QueueEntry:
        captured["cancel"] = (root, queue_id)
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
    assert captured["cancel"] == (resolved_allowed_root, "q_123")
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
    )

    def fake_submit_reaction_dir(**kwargs: Any) -> dict[str, Any]:
        submit_calls.append(kwargs)
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

    assert submit_calls == [
        {
            "reaction_dir": "/tmp/rxn_submit",
            "priority": 8,
            "config_path": "/tmp/orca.yaml",
            "repo_root": "/tmp/orca_repo",
        }
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
        "skipped": [{"stage_id": "skip_stage", "reason": "already_submitted"}],
        "failed": [{"stage_id": "missing_stage", "reason": "missing_reaction_dir"}],
    }
    assert len(saved_payloads) == 1
    assert len(sync_calls) == 1
    assert sync_calls[0]["workflow_root"] == workflow_root
    assert sync_calls[0]["workspace_dir"] == workspace_dir

    saved_payload = saved_payloads[0]["payload"]
    skip_stage, missing_stage, submit_stage = saved_payload["stages"]

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

    assert submit_stage["status"] == "queued"
    assert submit_stage["metadata"] == {
        "queue_id": "q_submit",
        "submission_status": "submitted",
        "submitted_at": "2026-04-19T00:01:00+00:00",
    }
    assert submit_stage["task"]["status"] == "submitted"
    assert submit_stage["task"]["submission_result"]["status"] == "submitted"
    assert submit_stage["task"]["submission_result"]["submitted_at"] == "2026-04-19T00:01:00+00:00"

    assert skip_stage["task"]["submission_result"] == {"status": "submitted"}

    assert saved_payload["status"] == "queued"
    assert saved_payload["metadata"]["submission_summary"] == {
        "status": "partially_submitted",
        "submitted_count": 1,
        "skipped_count": 1,
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
                "stage_id": "submit_stage",
                "status": "submitted",
                "queue_id": "q_submit",
                "returncode": 0,
            },
        ],
        "updated_at": "2026-04-19T00:02:00+00:00",
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
