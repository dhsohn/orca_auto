from __future__ import annotations

from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue import (
    QueueEntry,
    QueueStatus,
    enqueue,
    list_queue,
    mark_cancelled,
    request_cancel,
)
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.flow.engines.crest import job_inputs as crest_job_inputs
from orca_auto.flow.engines.crest import state as crest_state
from orca_auto.flow.engines.xtb import job_inputs as xtb_job_inputs
from orca_auto.flow.engines.xtb import state as xtb_state
from orca_auto.flow.submitters import crest as crest_submitter
from orca_auto.flow.submitters import xtb as xtb_submitter


@pytest.mark.parametrize(
    ("module", "engine", "target", "displayed_status", "expected_status", "queue_id", "job_id"),
    [
        (
            xtb_submitter,
            "xtb",
            "xtb-job-1",
            "cancel_requested",
            "cancel_requested",
            "q-1",
            "xtb-job-1",
        ),
        (xtb_submitter, "xtb", "xtb-job-2", "cancelled", "cancelled", "q-2", "xtb-job-2"),
        (
            crest_submitter,
            "crest",
            "crest-job-1",
            "cancel_requested",
            "cancel_requested",
            "c-1",
            "crest-job-1",
        ),
        (crest_submitter, "crest", "crest-job-2", "cancelled", "cancelled", "c-2", "crest-job-2"),
    ],
)
def test_cancel_target_uses_structured_queue_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
    engine: str,
    target: str,
    displayed_status: str,
    expected_status: str,
    queue_id: str,
    job_id: str,
) -> None:
    captured: dict[str, Any] = {}
    queue_root = tmp_path / "queue"
    cfg = SimpleNamespace(name=f"{engine}-queue-config")
    original_entry = SimpleNamespace(queue_id=queue_id, task_id=job_id)

    def fake_load_queue_config(config_path: str) -> Any:
        captured["config_path"] = config_path
        return cfg

    def fake_queue_entries_with_roots(cfg_arg: Any) -> list[tuple[Path, Any]]:
        captured["listed_cfg"] = cfg_arg
        return [(queue_root, original_entry)]

    def fake_request_cancel(
        root: Path,
        requested_queue_id: str,
        **kwargs: object,
    ) -> Any:
        captured["request_cancel"] = (root, requested_queue_id, kwargs)
        return SimpleNamespace(
            queue_id=requested_queue_id,
            task_id=job_id,
            cancel_requested=displayed_status == "cancel_requested",
            status=SimpleNamespace(
                value="running" if displayed_status == "cancel_requested" else "cancelled"
            ),
        )

    def fake_display_status(entry: Any) -> str:
        captured["display_entry"] = entry
        return displayed_status

    monkeypatch.setattr(module, "load_queue_config", fake_load_queue_config)
    monkeypatch.setattr(module, "queue_entries_with_roots", fake_queue_entries_with_roots)
    monkeypatch.setattr(module, "request_cancel", fake_request_cancel)
    monkeypatch.setattr(module, "display_status", fake_display_status)

    result = module.cancel_target(
        target=target,
        config_path="/tmp/config.yaml",
    )

    assert captured["config_path"] == "/tmp/config.yaml"
    assert captured["listed_cfg"] is cfg
    request_root, requested_queue_id, request_kwargs = captured["request_cancel"]
    assert (request_root, requested_queue_id) == (queue_root, queue_id)
    assert request_kwargs["expected_entry"] is original_entry
    assert callable(request_kwargs["accept_entry_fn"])
    assert callable(request_kwargs["before_pending_cancel_fn"])
    assert result["status"] == expected_status
    assert result["returncode"] == 0
    assert result["command_argv"] == [
        f"orca_auto.{engine}.queue_runtime.direct_cancel",
        "config=/tmp/config.yaml",
        f"target={target}",
    ]
    assert result["stdout"] == f"status: {expected_status}\nqueue_id: {queue_id}\njob_id: {job_id}"
    assert result["stderr"] == ""
    assert result["parsed_stdout"]["status"] == expected_status
    assert result["queue_id"] == queue_id
    assert result["job_id"] == job_id


@pytest.mark.parametrize(
    ("module", "engine"),
    [(crest_submitter, "crest"), (xtb_submitter, "xtb")],
)
def test_pending_cancel_publishes_generation_bound_terminal_state(
    tmp_path: Path,
    module: Any,
    engine: str,
) -> None:
    runs_root = tmp_path / "runs"
    job_dir = runs_root / f"{engine}-job"
    job_dir.mkdir(parents=True)
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\nH\nH 0 0 0\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    identity_metadata: dict[str, Any]
    if engine == "crest":
        identity_metadata = {"mode": "nci", "molecule_key": "mol-1"}
        queued_state = crest_job_inputs.queued_state_payload(
            job_id="crest-pending-cancel",
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            mode="nci",
            molecule_key="mol-1",
        )
        write_state = crest_state.write_state
        load_state = crest_state.load_state
    else:
        identity_metadata = {
            "job_type": "opt",
            "reaction_key": "rxn-1",
            "input_summary": {"input_xyz": str(selected_xyz)},
        }
        queued_state = xtb_job_inputs.queued_state_payload(
            job_id="xtb-pending-cancel",
            job_dir=job_dir,
            selected_input_xyz=selected_xyz,
            job_type="opt",
            reaction_key="rxn-1",
            input_summary=identity_metadata["input_summary"],
        )
        write_state = xtb_state.write_state
        load_state = xtb_state.load_state
    queue_root = runs_root / "queue"
    entry = enqueue(
        queue_root,
        app_name=f"orca_auto_{engine}",
        task_id=f"{engine}-pending-cancel",
        task_kind=f"{engine}_test",
        engine=engine,
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            **identity_metadata,
        },
    )
    queued_state["job"].update(
        {
            "queue_id": entry.queue_id,
            "app_name": entry.app_name,
            "task_id": entry.task_id,
            "generation": queue_entry_generation_token(entry),
        }
    )
    write_state(job_dir, queued_state)

    cancelled = request_cancel(
        queue_root,
        entry.queue_id,
        expected_entry=entry,
        before_pending_cancel_fn=partial(
            module._before_pending_cancel,
            config_path=str(config_path),
        ),
    )

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED
    terminal = load_state(job_dir)
    assert terminal is not None
    assert terminal["status"]["state"] == "cancelled"
    assert terminal["status"]["reason"] == "cancel_requested"
    assert terminal["status"]["exit_code"] == 1
    assert terminal["job"]["id"] == entry.task_id
    assert terminal["job"]["queue_id"] == entry.queue_id
    assert terminal["job"]["generation"] == queue_entry_generation_token(entry)


@pytest.mark.parametrize(
    ("module", "engine"),
    [(crest_submitter, "crest"), (xtb_submitter, "xtb")],
)
def test_pending_cancel_rejects_foreign_generation_artifact(
    tmp_path: Path,
    module: Any,
    engine: str,
) -> None:
    runs_root = tmp_path / "runs"
    job_dir = runs_root / f"{engine}-job"
    job_dir.mkdir(parents=True)
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\nH\nH 0 0 0\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")

    identity_metadata: dict[str, Any]
    if engine == "crest":
        identity_metadata = {"mode": "nci", "molecule_key": "mol-1"}
        queued_state = crest_job_inputs.queued_state_payload(
            job_id="crest-pending-cancel",
            job_dir=job_dir,
            selected_xyz=selected_xyz,
            mode="nci",
            molecule_key="mol-1",
        )
        write_state = crest_state.write_state
    else:
        identity_metadata = {
            "job_type": "opt",
            "reaction_key": "rxn-1",
            "input_summary": {"input_xyz": str(selected_xyz)},
        }
        queued_state = xtb_job_inputs.queued_state_payload(
            job_id="xtb-pending-cancel",
            job_dir=job_dir,
            selected_input_xyz=selected_xyz,
            job_type="opt",
            reaction_key="rxn-1",
            input_summary=identity_metadata["input_summary"],
        )
        write_state = xtb_state.write_state

    queue_root = runs_root / "queue"
    entry = enqueue(
        queue_root,
        app_name=f"orca_auto_{engine}",
        task_id=f"{engine}-pending-cancel",
        task_kind=f"{engine}_test",
        engine=engine,
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            **identity_metadata,
        },
    )
    queued_state["job"].update(
        {
            "queue_id": entry.queue_id,
            "app_name": entry.app_name,
            "task_id": entry.task_id,
            "generation": "foreign-generation-token",
        }
    )
    write_state(job_dir, queued_state)
    state_path = job_dir / "job_state.json"
    original_state = state_path.read_bytes()

    with pytest.raises(ValueError, match="exact queue generation"):
        request_cancel(
            queue_root,
            entry.queue_id,
            expected_entry=entry,
            before_pending_cancel_fn=partial(
                module._before_pending_cancel,
                config_path=str(config_path),
            ),
        )

    assert state_path.read_bytes() == original_state
    [persisted] = list_queue(queue_root)
    assert persisted.status == QueueStatus.PENDING
    assert not persisted.cancel_requested


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_cancel_target_reports_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
) -> None:
    def fake_load_queue_config(_config_path: str) -> Any:
        return object()

    def fake_queue_entries_with_roots(_cfg: Any) -> list[tuple[Path, Any]]:
        return [(tmp_path / "queue", SimpleNamespace(queue_id="q-1", task_id="job-1"))]

    def fake_request_cancel(_root: Path, _queue_id: str, **_kwargs: object) -> Any:
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(module, "load_queue_config", fake_load_queue_config)
    monkeypatch.setattr(module, "queue_entries_with_roots", fake_queue_entries_with_roots)
    monkeypatch.setattr(module, "request_cancel", fake_request_cancel)

    result = module.cancel_target(
        target="job-1",
        config_path="/tmp/config.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "cancel_command_failed"
    assert result["returncode"] == 1
    assert result["stdout"] == ""
    assert result["stderr"] == "RuntimeError: cancel failed\n"


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_internal_cancel_adopts_same_generation_cancelled_terminal_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
) -> None:
    queue_root = tmp_path / "queue"
    entry = enqueue(
        queue_root,
        app_name=f"orca_auto_{'xtb' if module is xtb_submitter else 'crest'}",
        task_id="cancel-terminal-replay",
        task_kind="test",
        engine="xtb" if module is xtb_submitter else "crest",
        metadata={"job_dir": str(tmp_path / "job")},
    )
    monkeypatch.setattr(module, "load_queue_config", lambda _path: object())
    monkeypatch.setattr(
        module,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_root, current) for current in list_queue(queue_root)],
    )

    def concurrently_cancel_then_report_no_transition(
        root: Path,
        queue_id: str,
        **_kwargs: Any,
    ) -> None:
        cancelled = mark_cancelled(root, queue_id, expected_entry=entry)
        assert cancelled is not None
        return None

    monkeypatch.setattr(module, "request_cancel", concurrently_cancel_then_report_no_transition)
    monkeypatch.setattr(
        module,
        "_before_pending_cancel",
        lambda _entry, *, config_path: None,
    )

    result = module.cancel_target(
        target=entry.queue_id,
        config_path="/tmp/engine.yaml",
    )

    assert result["status"] == "cancelled"
    assert result["returncode"] == 0
    assert result["queue_id"] == entry.queue_id
    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED


@pytest.mark.parametrize("module", [xtb_submitter, crest_submitter])
def test_internal_cancel_rejects_cancelled_successor_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: Any,
) -> None:
    queue_root = tmp_path / "queue"
    entry = enqueue(
        queue_root,
        app_name=f"orca_auto_{'xtb' if module is xtb_submitter else 'crest'}",
        task_id="cancel-successor",
        task_kind="test",
        engine="xtb" if module is xtb_submitter else "crest",
        metadata={"job_dir": str(tmp_path / "job")},
    )
    first_listing = True

    def list_original_then_successor(_cfg: Any) -> list[tuple[Path, QueueEntry]]:
        nonlocal first_listing
        if first_listing:
            first_listing = False
            return [(queue_root, entry)]
        return [
            (
                queue_root,
                replace(entry, status=QueueStatus.CANCELLED, task_id="successor-task"),
            )
        ]

    monkeypatch.setattr(module, "load_queue_config", lambda _path: object())
    monkeypatch.setattr(module, "queue_entries_with_roots", list_original_then_successor)
    monkeypatch.setattr(module, "request_cancel", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_before_pending_cancel",
        lambda _entry, *, config_path: None,
    )

    result = module.cancel_target(
        target=entry.queue_id,
        config_path="/tmp/engine.yaml",
    )

    assert result["status"] == "failed"
    assert result["reason"] == "cancel_command_failed"
    assert result["stderr"] == f"queue target already terminal: {entry.queue_id}\n"


def test_internal_cancel_recovers_durable_post_commit_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    job_dir = queue_root / "job"
    job_dir.mkdir(parents=True)
    entry = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="xtb-cancel-post-commit",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={"job_dir": str(job_dir), "job_type": "opt"},
    )
    monkeypatch.setattr(xtb_submitter, "load_queue_config", lambda _path: object())
    monkeypatch.setattr(
        xtb_submitter,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_root, current) for current in list_queue(queue_root)],
    )

    def persist_then_raise(root: Path, queue_id: str, **kwargs: Any) -> Any:
        updated = request_cancel(root, queue_id, **kwargs)
        assert updated is not None
        raise OSError("cancel durability barrier failed")

    monkeypatch.setattr(xtb_submitter, "request_cancel", persist_then_raise)
    monkeypatch.setattr(
        xtb_submitter,
        "_before_pending_cancel",
        lambda _entry, *, config_path: None,
    )

    result = xtb_submitter.cancel_target(
        target=entry.queue_id,
        config_path="/tmp/xtb.yaml",
    )

    assert result["status"] == "cancelled"
    assert result["returncode"] == 0
    [cancelled] = list_queue(queue_root)
    assert cancelled.status == QueueStatus.CANCELLED
