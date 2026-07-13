"""Direct queue submission error handling."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import orca_auto.orca.commands.run_inp_submission as submission_mod
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
)
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.commands import run_inp
from orca_auto.orca.config import AppConfig, CommonResourceConfig, PathsConfig, RuntimeConfig
from orca_auto.orca.queue import adapter as queue_adapter


def _deps(context: Any) -> SimpleNamespace:
    return SimpleNamespace(
        submission=SimpleNamespace(
            resolve_submission_context=lambda _args: context,
            queue_adapter=SimpleNamespace(
                get_active_entry_for_reaction_dir=lambda _root, _reaction_dir: None,
            ),
            active_direct_run_error=lambda _reaction_dir: None,
        )
    )


def test_submit_without_selectable_inp_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reaction dir without any .inp used to leak the ValueError from
    # resource-request resolution as a CLI traceback.
    context = SimpleNamespace(
        cfg=None,
        allowed_root=tmp_path,
        reaction_dir=tmp_path / "job",
        selected_inp=None,
    )

    def raise_value_error(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("No .inp file selected for ORCA queue submission.")

    monkeypatch.setattr(submission_mod, "create_queued_submission", raise_value_error)

    result = submission_mod.submit_reaction_dir_to_queue(SimpleNamespace(), deps=_deps(context))

    assert result.status == "failed"
    assert result.reason == "invalid_submission_input"
    assert "No .inp file selected" in result.stderr


def _real_submission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Any]:
    fake_orca = tmp_path / "fake_orca"
    fake_orca.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_orca.chmod(0o755)
    cfg = AppConfig(
        runtime=RuntimeConfig(allowed_root=str(tmp_path)),
        paths=PathsConfig(orca_executable=str(fake_orca)),
        resources=CommonResourceConfig(max_cores_per_task=2, max_memory_gb_per_task=4),
    )
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    (reaction_dir / "rxn.inp").write_text(
        "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_inp, "load_config", lambda _path: cfg)
    monkeypatch.setattr(run_inp, "notify_queue_enqueued_event", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("orca_auto.orca.queue.worker.read_worker_pid", lambda _root: None)
    args = SimpleNamespace(
        config=str(tmp_path / "orca_auto.yaml"),
        reaction_dir=str(reaction_dir),
        force=False,
        priority=7,
    )
    return reaction_dir, args


def test_enqueue_save_after_commit_recovers_exact_row_and_submits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    first_save = True

    def save_then_raise(root: Path, entries: Any) -> None:
        nonlocal first_save
        original_save(root, entries)
        if first_save:
            first_save = False
            raise RuntimeError("enqueue fsync failed after replace")

    monkeypatch.setattr(queue_store, "save_entries", save_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    assert "recovered exact queued entry" in result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_complete_transition_after_commit_returns_submitted_with_truthful_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    raised = False

    def save_complete_then_raise(root: Path, entries: Any) -> None:
        nonlocal raised
        original_save(root, entries)
        if not raised and any(
            entry.metadata.get(QUEUE_RECORD_SYNC_KEY) == QUEUE_RECORD_SYNC_COMPLETE
            for entry in entries
        ):
            raised = True
            raise RuntimeError("complete fsync failed after replace")

    monkeypatch.setattr(queue_store, "save_entries", save_complete_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    assert "durable COMPLETE state recovered" in result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_enqueue_save_without_commit_fails_cleanly_without_queue_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)

    def raise_without_save(_root: Path, _entries: Any) -> None:
        raise RuntimeError("enqueue write failed before commit")

    monkeypatch.setattr(queue_store, "save_entries", raise_without_save)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_submission_failed"
    assert queue_adapter.list_queue(tmp_path) == []
    assert list((reaction_dir / ".orca_auto_orca_executions").iterdir()) == []


def test_orca_adapter_rejects_fractional_priority_before_persistence(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()

    with pytest.raises(ValueError, match="priority must be an integer"):
        queue_adapter.enqueue(tmp_path, str(reaction_dir), priority=1.5)  # type: ignore[arg-type]

    assert queue_adapter.list_queue(tmp_path) == []


def test_ambiguous_postcommit_rows_fail_closed_and_remain_unclaimable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_save = queue_store.save_entries
    first_save = True

    def save_ambiguous_then_raise(root: Path, entries: Any) -> None:
        nonlocal first_save
        if first_save:
            first_save = False
            duplicate = replace(entries[-1], queue_id="q_ambiguous_duplicate")
            original_save(root, [*entries, duplicate])
            raise RuntimeError("enqueue fsync failed with duplicate durable rows")
        original_save(root, entries)

    monkeypatch.setattr(queue_store, "save_entries", save_ambiguous_then_raise)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "failed"
    assert result.reason == "queue_enqueue_outcome_unknown"
    entries = queue_adapter.list_queue(tmp_path)
    assert len(entries) == 2
    assert all(entry.status == QueueStatus.CANCELLED for entry in entries)
    assert all(
        entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED for entry in entries
    )
    assert queue_adapter.dequeue_next(tmp_path) is None


def test_duplicate_error_after_commit_is_recovered_as_same_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    original_enqueue = queue_adapter.enqueue

    def enqueue_then_report_duplicate(*enqueue_args: Any, **enqueue_kwargs: Any) -> Any:
        entry = original_enqueue(*enqueue_args, **enqueue_kwargs)
        raise queue_adapter.DuplicateEntryError(str(reaction_dir), entry)

    monkeypatch.setattr(queue_adapter, "enqueue", enqueue_then_report_duplicate)

    result = run_inp.submit_reaction_dir_to_queue(args)

    assert result.status == "submitted"
    assert result.queued_result is not None
    assert "DuplicateEntryError" in result.queued_result.worker_info.detail
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE


def test_cancellation_waits_for_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reaction_dir, args = _real_submission(tmp_path, monkeypatch)
    publication_started = threading.Event()
    allow_publication = threading.Event()
    cancel_finished = threading.Event()
    original_upsert = submission_mod.upsert_queued_job_record
    submission_result: list[Any] = []
    cancellation_result: list[Any] = []

    def blocking_upsert(*upsert_args: Any, **upsert_kwargs: Any) -> None:
        publication_started.set()
        assert allow_publication.wait(timeout=5)
        original_upsert(*upsert_args, **upsert_kwargs)

    monkeypatch.setattr(submission_mod, "upsert_queued_job_record", blocking_upsert)

    submit_thread = threading.Thread(
        target=lambda: submission_result.append(run_inp.submit_reaction_dir_to_queue(args))
    )
    submit_thread.start()
    assert publication_started.wait(timeout=5)
    [preparing_entry] = queue_adapter.list_queue(tmp_path)

    def cancel_entry() -> None:
        cancellation_result.append(queue_adapter.cancel(tmp_path, preparing_entry.queue_id))
        cancel_finished.set()

    cancel_thread = threading.Thread(target=cancel_entry)
    cancel_thread.start()
    assert not cancel_finished.wait(timeout=0.1)
    allow_publication.set()
    submit_thread.join(timeout=5)
    cancel_thread.join(timeout=5)

    assert submission_result[0].status == "submitted"
    assert cancellation_result[0].status == QueueStatus.CANCELLED
    [entry] = queue_adapter.list_queue(tmp_path)
    assert entry.status == QueueStatus.CANCELLED
    assert entry.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
