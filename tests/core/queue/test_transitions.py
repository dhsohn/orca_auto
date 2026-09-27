from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orca_auto.core.queue import publication, store, transitions
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_ABORTED,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_PREPARING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
    process_start_token,
)
from orca_auto.core.queue.types import QueueStatus
from tests.queue_store_helpers import (
    _claim_next,
    _enqueue,
    _entry,
    _install_deterministic_helpers,
    _queue_file,
    _without_sync_metadata,
)


def test_request_cancel_rejects_replacement_with_same_queue_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", task_id="task-a")], indent=2),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", task_id="task-b")], indent=2),
        encoding="utf-8",
    )

    assert transitions.request_cancel(tmp_path, "q-same", expected_entry=selected) is None
    [replacement] = store.list_queue(tmp_path)
    assert replacement.task_id == "task-b"
    assert replacement.status == QueueStatus.PENDING
    assert replacement.cancel_requested is False


def test_request_cancel_accepts_same_generation_after_publication_transition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    preparing_metadata = {
        "job_dir": str(tmp_path / "job"),
        **publication.queue_record_sync_metadata(
            publication.QUEUE_RECORD_SYNC_PREPARING,
            token="publication-token",
            owner_pid=os.getpid(),
        ),
    }
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", metadata=preparing_metadata)], indent=2),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    complete_metadata = {
        **preparing_metadata,
        **publication.queue_record_sync_metadata(
            publication.QUEUE_RECORD_SYNC_COMPLETE,
            token="publication-token",
            owner_pid=0,
        ),
    }
    _queue_file(tmp_path).write_text(
        json.dumps([_entry("q-same", metadata=complete_metadata)], indent=2),
        encoding="utf-8",
    )

    cancelled = transitions.request_cancel(tmp_path, "q-same", expected_entry=selected)

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED


def test_pending_cancel_metadata_callback_failure_aborts_queue_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id="pending-metadata-failure",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    before = _queue_file(tmp_path).read_bytes()
    save_calls = 0

    def reject_metadata(_candidate: store.QueueEntry) -> dict[str, object]:
        raise OSError("metadata generation failed")

    def count_save(_root: Path, _entries: Sequence[store.QueueEntry]) -> None:
        nonlocal save_calls
        save_calls += 1

    with pytest.raises(OSError, match="metadata generation failed"):
        transitions.request_cancel(
            tmp_path,
            entry.queue_id,
            pending_metadata_update_fn=reject_metadata,
            save_entries_fn=count_save,
        )

    assert save_calls == 0
    assert _queue_file(tmp_path).read_bytes() == before
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING
    assert _without_sync_metadata(unchanged.metadata) == {"keep": "yes"}


def test_running_cancel_does_not_invoke_pending_metadata_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    pending = _enqueue(
        tmp_path,
        app_name="app",
        task_id="running-callback",
        task_kind="kind",
        engine="engine",
    )
    running = _claim_next(tmp_path)
    assert running is not None

    requested = transitions.request_cancel(
        tmp_path,
        pending.queue_id,
        expected_entry=running,
        pending_metadata_update_fn=lambda _candidate: pytest.fail(
            "pending metadata callback must not run for a running cancellation"
        ),
    )

    assert requested is not None
    assert requested.status == QueueStatus.RUNNING
    assert requested.cancel_requested is True


def test_terminal_mark_rejects_replacement_with_same_queue_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [_entry("q-same", task_id="task-a", status=QueueStatus.RUNNING)],
            indent=2,
        ),
        encoding="utf-8",
    )
    [selected] = store.list_queue(tmp_path)
    _queue_file(tmp_path).write_text(
        json.dumps(
            [_entry("q-same", task_id="task-b", status=QueueStatus.RUNNING)],
            indent=2,
        ),
        encoding="utf-8",
    )

    assert transitions.mark_completed(tmp_path, "q-same", expected_entry=selected) is None
    [replacement] = store.list_queue(tmp_path)
    assert replacement.task_id == "task-b"
    assert replacement.status == QueueStatus.RUNNING


def test_terminal_completion_does_not_overwrite_acknowledged_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id="task-a",
        task_kind="kind",
        engine="engine",
    )
    running = _claim_next(tmp_path)
    assert running is not None
    assert transitions.request_cancel(tmp_path, entry.queue_id, expected_entry=running) is not None

    assert transitions.mark_completed(tmp_path, entry.queue_id, expected_entry=running) is None
    [cancel_requested] = store.list_queue(tmp_path)
    assert cancel_requested.status == QueueStatus.RUNNING
    assert cancel_requested.cancel_requested is True
    assert transitions.mark_cancelled(tmp_path, entry.queue_id, expected_entry=running) is not None
    [cancelled] = store.list_queue(tmp_path)
    assert cancelled.status == QueueStatus.CANCELLED


def test_request_cancel_handles_pending_running_and_terminal_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    pending = _enqueue(
        tmp_path,
        app_name="app",
        task_id="pending",
        task_kind="kind",
        engine="engine",
    )
    pending_cancelled = transitions.request_cancel(tmp_path, pending.queue_id)
    assert pending_cancelled is not None
    assert pending_cancelled.status == QueueStatus.CANCELLED
    assert pending_cancelled.cancel_requested is True
    assert pending_cancelled.finished_at == "2026-04-19T00:00:02+00:00"

    running = _enqueue(
        tmp_path,
        app_name="app",
        task_id="running",
        task_kind="kind",
        engine="engine",
    )
    dequeued = _claim_next(tmp_path)
    assert dequeued is not None
    assert dequeued.queue_id == running.queue_id

    running_cancelled = transitions.request_cancel(tmp_path, running.queue_id)
    assert running_cancelled is not None
    assert running_cancelled.status == QueueStatus.RUNNING
    assert running_cancelled.cancel_requested is True
    assert running_cancelled.finished_at == ""
    assert store.get_cancel_requested(tmp_path, running.queue_id) is True
    assert store.get_cancel_requested(tmp_path, "missing-queue-id") is False
    assert transitions.request_cancel(tmp_path, "missing-queue-id") is None

    terminal = _enqueue(
        tmp_path,
        app_name="app",
        task_id="terminal",
        task_kind="kind",
        engine="engine",
    )
    assert transitions.mark_completed(tmp_path, terminal.queue_id) is not None
    assert transitions.request_cancel(tmp_path, terminal.queue_id) is None


def test_request_cancel_revalidates_selected_identity_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="foreign-app",
        task_id="foreign-task",
        task_kind="foreign-kind",
        engine="foreign",
    )

    assert (
        transitions.request_cancel(
            tmp_path,
            entry.queue_id,
            accept_entry_fn=lambda current: current.engine == "owned",
        )
        is None
    )
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING


def test_request_cancel_revokes_transient_publication_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id="publishing",
        task_kind="kind",
        engine="engine",
        metadata={
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_PREPARING,
            QUEUE_RECORD_SYNC_OWNER_PID_KEY: os.getpid(),
            QUEUE_RECORD_SYNC_OWNER_START_KEY: process_start_token(os.getpid()),
            QUEUE_RECORD_SYNC_TOKEN_KEY: "publisher-token",
            QUEUE_RECORD_SYNC_UPDATED_AT_KEY: datetime.now(UTC).isoformat(),
        },
    )

    cancelled = transitions.request_cancel(tmp_path, entry.queue_id)

    assert cancelled is not None
    assert cancelled.status == QueueStatus.CANCELLED
    assert cancelled.metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_ABORTED
    assert cancelled.metadata[QUEUE_RECORD_SYNC_OWNER_PID_KEY] == 0
    assert cancelled.metadata[QUEUE_RECORD_SYNC_OWNER_START_KEY] == ""
    assert cancelled.metadata[QUEUE_RECORD_SYNC_TOKEN_KEY] == ""


def test_requeue_running_entry_returns_running_entry_to_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    running = _enqueue(
        tmp_path,
        app_name="app",
        task_id="running",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    dequeued = _claim_next(tmp_path)
    assert dequeued is not None
    assert dequeued.queue_id == running.queue_id
    assert dequeued.status == QueueStatus.RUNNING

    updated = transitions.requeue_running_entry(
        tmp_path,
        running.queue_id,
        cancel_metadata_update_fn=lambda _candidate: pytest.fail(
            "cancel metadata callback must not run for an ordinary requeue"
        ),
    )
    assert updated is not None
    assert updated.status == QueueStatus.PENDING
    assert updated.started_at == ""
    assert updated.cancel_requested is False
    assert updated.error == ""
    assert _without_sync_metadata(updated.metadata) == {"keep": "yes"}

    entries = store.list_queue(tmp_path)
    assert len(entries) == 1
    assert entries[0].status == QueueStatus.PENDING
    assert transitions.requeue_running_entry(tmp_path, "missing-queue-id") is None


def test_requeue_running_entry_cancels_when_cancel_requested(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A cancel arriving while the entry runs must not be resurrected by the
    # worker-shutdown requeue path: workers deliver cancellation as a SIGTERM the
    # run treats as a shutdown requeue, which previously cleared cancel_requested
    # and let the cancelled job be dequeued and resumed.
    _install_deterministic_helpers(monkeypatch)

    running = _enqueue(
        tmp_path,
        app_name="app",
        task_id="running",
        task_kind="kind",
        engine="engine",
    )
    assert _claim_next(tmp_path) is not None
    assert transitions.request_cancel(tmp_path, running.queue_id) is not None

    lock_held = False

    @contextmanager
    def mutation_lock(_root: Path, *, timeout_seconds: float = 10.0):
        nonlocal lock_held
        del timeout_seconds
        assert lock_held is False
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(store, "queue_lock", mutation_lock)

    def cancel_metadata(candidate: store.QueueEntry) -> dict[str, object]:
        assert lock_held is True
        assert candidate.status == QueueStatus.CANCELLED
        assert candidate.cancel_requested is False
        return {"terminal_replay": {"status": candidate.status.value}}

    updated = transitions.requeue_running_entry(
        tmp_path,
        running.queue_id,
        cancel_metadata_update_fn=cancel_metadata,
    )
    assert updated is not None
    assert updated.status == QueueStatus.CANCELLED
    assert updated.finished_at != ""
    # The cancel has been honored; the terminal entry no longer advertises it.
    assert updated.cancel_requested is False
    assert updated.metadata["terminal_replay"] == {"status": "cancelled"}
    assert lock_held is False

    # The cancelled entry is terminal and is never handed back out for a resume.
    assert _claim_next(tmp_path) is None


def test_requeue_cancel_metadata_callback_failure_aborts_queue_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id="running-metadata-failure",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    running = _claim_next(tmp_path)
    assert running is not None
    requested = transitions.request_cancel(tmp_path, entry.queue_id)
    assert requested is not None and requested.cancel_requested is True
    before = _queue_file(tmp_path).read_bytes()
    save_calls = 0

    def reject_metadata(_candidate: store.QueueEntry) -> dict[str, object]:
        raise OSError("metadata generation failed")

    def count_save(_root: Path, _entries: Sequence[store.QueueEntry]) -> None:
        nonlocal save_calls
        save_calls += 1

    with pytest.raises(OSError, match="metadata generation failed"):
        transitions.requeue_running_entry(
            tmp_path,
            entry.queue_id,
            cancel_metadata_update_fn=reject_metadata,
            save_entries_fn=count_save,
        )

    assert save_calls == 0
    assert _queue_file(tmp_path).read_bytes() == before
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.RUNNING
    assert unchanged.cancel_requested is True
    assert _without_sync_metadata(unchanged.metadata) == {"keep": "yes"}


@pytest.mark.parametrize(
    ("helper_name", "helper_kwargs", "expected_status"),
    [
        ("mark_completed", {}, QueueStatus.COMPLETED),
        ("mark_failed", {"error": "  boom  "}, QueueStatus.FAILED),
        ("mark_cancelled", {"error": "  stop  "}, QueueStatus.CANCELLED),
    ],
)
def test_mark_helpers_merge_metadata_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    helper_name: str,
    helper_kwargs: dict[str, object],
    expected_status: QueueStatus,
) -> None:
    _install_deterministic_helpers(monkeypatch)

    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id=f"task-{helper_name}",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes", "shared": "old"},
    )

    helper = getattr(transitions, helper_name)
    updated = helper(
        tmp_path,
        entry.queue_id,
        metadata_update={"shared": "new", "added": 42},
        **helper_kwargs,
    )

    assert updated is not None
    assert updated.status == expected_status
    assert _without_sync_metadata(updated.metadata) == {
        "keep": "yes",
        "shared": "new",
        "added": 42,
    }
    if helper_name != "mark_completed":
        assert updated.error == str(helper_kwargs["error"]).strip()
    assert helper(tmp_path, "missing-queue-id", **helper_kwargs) is None


@pytest.mark.parametrize(
    ("helper_name", "helper_kwargs", "expected_status"),
    [
        ("mark_completed", {}, QueueStatus.COMPLETED),
        ("mark_failed", {"error": "boom"}, QueueStatus.FAILED),
        ("mark_cancelled", {}, QueueStatus.CANCELLED),
    ],
)
def test_mark_helpers_merge_callback_metadata_under_queue_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    helper_name: str,
    helper_kwargs: dict[str, object],
    expected_status: QueueStatus,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id=f"callback-{helper_name}",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes", "shared": "original"},
    )
    lock_held = False

    @contextmanager
    def mutation_lock(_root: Path, *, timeout_seconds: float = 10.0):
        nonlocal lock_held
        del timeout_seconds
        assert lock_held is False
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(store, "queue_lock", mutation_lock)
    callback_entries: list[store.QueueEntry] = []

    def dynamic_metadata(current: store.QueueEntry) -> dict[str, object]:
        assert lock_held is True
        assert current.status == QueueStatus.PENDING
        callback_entries.append(current)
        return {"shared": "dynamic", "terminal_replay": expected_status.value}

    helper = getattr(transitions, helper_name)
    updated = helper(
        tmp_path,
        entry.queue_id,
        metadata_update={"shared": "static", "static": True},
        metadata_update_fn=dynamic_metadata,
        **helper_kwargs,
    )

    assert updated is not None and updated.status == expected_status
    assert _without_sync_metadata(updated.metadata) == {
        "keep": "yes",
        "shared": "dynamic",
        "static": True,
        "terminal_replay": expected_status.value,
    }
    assert callback_entries == [entry]
    assert lock_held is False


def test_mark_metadata_callback_failure_aborts_queue_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    entry = _enqueue(
        tmp_path,
        app_name="app",
        task_id="mark-metadata-failure",
        task_kind="kind",
        engine="engine",
        metadata={"keep": "yes"},
    )
    before = _queue_file(tmp_path).read_bytes()
    save_calls = 0

    def reject_metadata(_current: store.QueueEntry) -> dict[str, object]:
        raise OSError("metadata generation failed")

    def count_save(_root: Path, _entries: Sequence[store.QueueEntry]) -> None:
        nonlocal save_calls
        save_calls += 1

    with pytest.raises(OSError, match="metadata generation failed"):
        transitions.mark_failed(
            tmp_path,
            entry.queue_id,
            error="boom",
            metadata_update_fn=reject_metadata,
            save_entries_fn=count_save,
        )

    assert save_calls == 0
    assert _queue_file(tmp_path).read_bytes() == before
    [unchanged] = store.list_queue(tmp_path)
    assert unchanged.status == QueueStatus.PENDING
    assert unchanged.error == ""
    assert _without_sync_metadata(unchanged.metadata) == {"keep": "yes"}


def _write_single_running_entry(root: Path) -> None:
    _queue_file(root).write_text(
        json.dumps([_entry("q-1", status=QueueStatus.RUNNING)], indent=2),
        encoding="utf-8",
    )


def test_mark_status_never_flips_a_terminal_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cancel landing just after a natural completion (or the reverse) must
    # not rewrite the recorded result; only update_terminal reconciles.
    _install_deterministic_helpers(monkeypatch)
    _write_single_running_entry(tmp_path)

    completed = transitions.mark_completed(tmp_path, "q-1")
    assert completed is not None and completed.status == QueueStatus.COMPLETED

    refused = transitions.mark_cancelled(tmp_path, "q-1")
    assert refused is None
    assert store.list_queue(tmp_path)[0].status == QueueStatus.COMPLETED


def test_mark_status_does_not_resurrect_a_cancelled_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _write_single_running_entry(tmp_path)

    cancelled = transitions.mark_cancelled(tmp_path, "q-1")
    assert cancelled is not None and cancelled.status == QueueStatus.CANCELLED

    refused = transitions.mark_completed(tmp_path, "q-1")
    assert refused is None
    assert store.list_queue(tmp_path)[0].status == QueueStatus.CANCELLED


def test_mark_status_replays_same_terminal_side_effect_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_deterministic_helpers(monkeypatch)
    _write_single_running_entry(tmp_path)
    [running] = store.list_queue(tmp_path)
    assert (
        transitions.mark_cancelled(
            tmp_path,
            "q-1",
            metadata_update={"candidate_count": 2},
            expected_entry=running,
        )
        is not None
    )

    replayed = transitions.mark_cancelled(tmp_path, "q-1", expected_entry=running)

    assert replayed is not None
    assert replayed.status == QueueStatus.CANCELLED
    assert replayed.metadata["candidate_count"] == 2


def _terminal_source_entry(**overrides: object) -> store.QueueEntry:
    base: dict[str, object] = {
        "queue_id": "q-terminal",
        "app_name": "app",
        "task_id": "task",
        "task_kind": "kind",
        "engine": "engine",
        "status": QueueStatus.RUNNING,
        "enqueued_at": "2026-04-19T00:00:00+00:00",
        "started_at": "2026-04-19T00:00:01+00:00",
        "finished_at": "",
        "cancel_requested": False,
        "error": "recorded",
        "metadata": {"reaction_dir": "/tmp/rxn"},
    }
    base.update(overrides)
    return store.QueueEntry(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [QueueStatus.PENDING, QueueStatus.RUNNING])
def test_terminal_entry_rejects_non_terminal_status(status: QueueStatus) -> None:
    with pytest.raises(ValueError, match="terminal status"):
        transitions.terminal_entry(_terminal_source_entry(), status=status, error=None)


def test_terminal_entry_finished_at_defaults_to_now_and_keeps_an_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    source = _terminal_source_entry()

    stamped = transitions.terminal_entry(source, status=QueueStatus.COMPLETED, error="")
    assert stamped.status == QueueStatus.COMPLETED
    assert stamped.finished_at == "2026-04-19T00:00:01+00:00"

    kept = transitions.terminal_entry(
        source,
        status=QueueStatus.FAILED,
        error="boom",
        finished_at="2026-01-01T00:00:00+00:00",
    )
    assert kept.finished_at == "2026-01-01T00:00:00+00:00"
    # Only the terminal fields change; identity and lifecycle stamps are kept.
    assert (kept.queue_id, kept.task_id, kept.enqueued_at, kept.started_at) == (
        source.queue_id,
        source.task_id,
        source.enqueued_at,
        source.started_at,
    )


def test_terminal_entry_clears_cancel_requested_only_for_cancelled_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    requested = _terminal_source_entry(cancel_requested=True)

    cancelled = transitions.terminal_entry(requested, status=QueueStatus.CANCELLED, error=None)
    assert cancelled.cancel_requested is False

    failed = transitions.terminal_entry(requested, status=QueueStatus.FAILED, error="boom")
    assert failed.cancel_requested is True
    completed = transitions.terminal_entry(requested, status=QueueStatus.COMPLETED, error="")
    assert completed.cancel_requested is True

    # A writer that records the flag on purpose (pending-row cancellation, the
    # ambiguous enqueue fence) overrides the default rule explicitly.
    fenced = transitions.terminal_entry(
        _terminal_source_entry(cancel_requested=False),
        status=QueueStatus.CANCELLED,
        error=None,
        cancel_requested=True,
    )
    assert fenced.cancel_requested is True


def test_terminal_entry_error_and_metadata_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_deterministic_helpers(monkeypatch)
    source = _terminal_source_entry()

    kept = transitions.terminal_entry(source, status=QueueStatus.FAILED, error=None)
    assert kept.error == "recorded"
    assert kept.metadata == source.metadata

    stripped = transitions.terminal_entry(source, status=QueueStatus.FAILED, error="  boom \n")
    assert stripped.error == "boom"
    cleared = transitions.terminal_entry(source, status=QueueStatus.COMPLETED, error="")
    assert cleared.error == ""

    replaced = transitions.terminal_entry(
        source,
        status=QueueStatus.COMPLETED,
        error="",
        metadata={"run_id": "run-1"},
    )
    assert replaced.metadata == {"run_id": "run-1"}
    assert source.metadata == {"reaction_dir": "/tmp/rxn"}


def _failed_row(tmp_path: Path) -> store.QueueEntry:
    submitted = _enqueue(
        tmp_path,
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
    )
    assert _claim_next(tmp_path) is not None
    failed = transitions.mark_failed(tmp_path, submitted.queue_id, error="crashed")
    assert failed is not None
    return failed


def test_correct_terminal_status_refuses_non_terminal_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    submitted = _enqueue(
        tmp_path,
        app_name="app",
        task_id="task",
        task_kind="kind",
        engine="engine",
    )
    running = _claim_next(tmp_path)
    assert running is not None

    assert (
        transitions.correct_terminal_status(
            tmp_path,
            submitted.queue_id,
            status=QueueStatus.COMPLETED,
        )
        is None
    )
    assert store.list_queue(tmp_path) == [running]
    with pytest.raises(ValueError, match="terminal status"):
        transitions.correct_terminal_status(
            tmp_path,
            submitted.queue_id,
            status=QueueStatus.RUNNING,
        )


def test_correct_terminal_status_refuses_generation_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    failed = _failed_row(tmp_path)
    stale_generation = replace(failed, task_id="replacement-task")

    assert (
        transitions.correct_terminal_status(
            tmp_path,
            failed.queue_id,
            status=QueueStatus.COMPLETED,
            expected_entry=stale_generation,
        )
        is None
    )
    assert store.list_queue(tmp_path) == [failed]


def test_correct_terminal_status_refuses_task_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    failed = _failed_row(tmp_path)

    assert (
        transitions.correct_terminal_status(
            tmp_path,
            failed.queue_id,
            status=QueueStatus.COMPLETED,
            expected_task_id="other-task",
        )
        is None
    )
    assert (
        transitions.correct_terminal_status(
            tmp_path,
            failed.queue_id,
            status=QueueStatus.COMPLETED,
            accept_entry_fn=lambda _entry: False,
        )
        is None
    )
    assert (
        transitions.correct_terminal_status(tmp_path, "missing", status=QueueStatus.COMPLETED)
        is None
    )
    assert store.list_queue(tmp_path) == [failed]


def test_correct_terminal_status_rebuilds_the_row_and_merges_metadata_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_deterministic_helpers(monkeypatch)
    failed = _failed_row(tmp_path)
    lock_held = False

    @contextmanager
    def mutation_lock(_root: Path, *, timeout_seconds: float = 10.0):
        nonlocal lock_held
        del timeout_seconds
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(store, "queue_lock", mutation_lock)

    def metadata_update(current: store.QueueEntry) -> dict[str, object]:
        assert lock_held is True
        assert current == failed
        return {"seen_status": current.status.value}

    corrected = transitions.correct_terminal_status(
        tmp_path,
        failed.queue_id,
        status=QueueStatus.COMPLETED,
        metadata_update={"run_id": "run-1"},
        metadata_update_fn=metadata_update,
        expected_entry=failed,
        expected_task_id=failed.task_id,
    )

    assert corrected is not None
    assert corrected.status == QueueStatus.COMPLETED
    # ``error`` None keeps the recorded error; ``finished_at`` is re-stamped.
    assert corrected.error == "crashed"
    assert corrected.finished_at != failed.finished_at
    assert corrected.metadata["run_id"] == "run-1"
    assert corrected.metadata["seen_status"] == "failed"
    assert store.list_queue(tmp_path) == [corrected]


def test_terminal_rows_are_built_only_by_store_terminal_entry() -> None:
    """Static contract: no writer may build a terminal row around ``terminal_entry``.

    ``replace(..., status=QueueStatus.<terminal>)`` encodes the terminal rules
    (finished_at, cancel_requested, error) at the call site. Every writer must
    go through ``transitions.terminal_entry`` so the rules cannot silently
    diverge.
    """
    import ast

    package_root = Path(transitions.__file__).resolve().parents[2]
    terminal_names = {status.name for status in transitions.TERMINAL_QUEUE_STATUSES}
    offenders: list[str] = []
    for source_path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            callee_name = (
                callee.id
                if isinstance(callee, ast.Name)
                else callee.attr
                if isinstance(callee, ast.Attribute)
                else ""
            )
            if callee_name != "replace":
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (
                    keyword.arg == "status"
                    and isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "QueueStatus"
                    and value.attr in terminal_names
                ):
                    offenders.append(f"{source_path.relative_to(package_root)}:{node.lineno}")

    # ``terminal_entry`` itself passes its validated ``status`` variable, so a
    # literal terminal status in a ``replace`` call is always a bypass.
    assert offenders == [], offenders
