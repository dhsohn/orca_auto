"""Rule pins for queue generation identity, writer fences and requeue fields.

Each table records today's outcome of a queue rule that later refactor batches
move into one owner; the tables live in ``pins/queue_*.json``:

* ``queue_generation_identity.json``: the literal ``queue_entry_generation_token``
  of a fixed row and of one variant per field or metadata key, and whether the
  core and the ORCA publication rule call each variant the same generation.
* ``queue_writer_fences.json``: accept/refuse and the row change of every
  fenced adapter writer over (current row, expected_entry, expected_task_id).
* ``queue_requeue_fields.json``: the rows the orphan and core requeue paths leave.
* ``queue_cancel_after_notification_claim.json``: ``adapter.cancel`` with a
  snapshot taken before the queued notification claim, recorded as it is.
* ``queue_repaired_claim_notification.json``: a row repaired and claimed in one
  worker pass still gets its queued notification.
* ``queue_worker_child_argv.json``: the worker child argv.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from orca_auto.core.messaging import Message
from orca_auto.core.queue import store as queue_store
from orca_auto.core.queue.deferral import ADMISSION_DEFERRAL_METADATA_KEY, admission_deferral_update
from orca_auto.core.queue.generation import (
    queue_entries_same_generation,
    queue_entry_generation_token,
)
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_BLOCKED_KEY,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_OWNER_PID_KEY,
    QUEUE_RECORD_SYNC_OWNER_START_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    QUEUE_RECORD_SYNC_TOKEN_KEY,
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.config import load_config
from orca_auto.orca.queue import adapter, notifications
from orca_auto.orca.queue.entries import QUEUE_APP_NAME, QUEUE_ENGINE, QUEUE_TASK_KIND
from orca_auto.orca.queue.orphans import reconcile_orphaned_running_entries
from orca_auto.orca.queue.terminal_replay import (
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY,
    TERMINAL_REPLAY_METADATA_KEY,
)
from orca_auto.orca.queue.worker import OrcaQueueWorker
from orca_auto.orca.worker_execution import build_worker_child_command
from tests.contracts.conftest import Harness, _join_notification_senders
from tests.contracts.normalize import Normalizer, assert_pin

_QUEUE_ID = "q-pin"
_TASK_ID = "orca-pin"
_ENQUEUED_AT = "2026-01-02T03:04:05.000006+00:00"
_STARTED_AT = "2026-01-02T03:05:00.000006+00:00"
_FINISHED_AT = "2026-01-02T03:06:00.000006+00:00"
_LATER = "2026-02-03T04:05:06.000007+00:00"
_PENDING_KEY = notifications.QUEUED_NOTIFICATION_PENDING_KEY


def _row(reaction_dir: str, **fields: Any) -> QueueEntry:
    """A pending ORCA row as ``adapter.enqueue`` persists it, with fixed values."""
    metadata = {
        "reaction_dir": reaction_dir,
        "force": False,
        "selected_inp": f"{reaction_dir}/job.inp",
        "worker_log": f"{reaction_dir}/logs/{_QUEUE_ID}.log",
        QUEUE_RECORD_SYNC_KEY: "complete",
        QUEUE_RECORD_SYNC_UPDATED_AT_KEY: _ENQUEUED_AT,
        QUEUE_RECORD_SYNC_OWNER_PID_KEY: 0,
        QUEUE_RECORD_SYNC_OWNER_START_KEY: "",
        QUEUE_RECORD_SYNC_TOKEN_KEY: _QUEUE_ID,
        _PENDING_KEY: False,
    }
    return QueueEntry(
        queue_id=_QUEUE_ID,
        app_name=QUEUE_APP_NAME,
        task_id=_TASK_ID,
        task_kind=QUEUE_TASK_KIND,
        engine=QUEUE_ENGINE,
        status=QueueStatus.PENDING,
        priority=10,
        enqueued_at=_ENQUEUED_AT,
        metadata=metadata,
        **fields,
    )


def _with_metadata(entry: QueueEntry, update: dict[str, Any], *drop: str) -> QueueEntry:
    metadata = {key: value for key, value in entry.metadata.items() if key not in drop}
    return replace(entry, metadata={**metadata, **update})


# --- generation identity ------------------------------------------------------

_DEFERRAL = {"reason": "scratch root is full", "not_before": _LATER}

_METADATA_VARIANTS: dict[str, dict[str, Any]] = {
    TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY: {TERMINAL_REPLAY_FENCE_ONLY_METADATA_KEY: True},
    TERMINAL_REPLAY_METADATA_KEY: {TERMINAL_REPLAY_METADATA_KEY: {"version": 1}},
    "candidate_count": {"candidate_count": 3},
    "retained_conformer_count": {"retained_conformer_count": 2},
    "attempt": {"attempt": 2},
    "execution_dir": {"execution_dir": "/pins/runs/job/20260102-030405-0a1b2c3d"},
    "run_id": {"run_id": "run-1"},
    "terminal_artifacts": {"terminal_artifacts": {"report": "report.json"}},
    "terminal_repair_blocked_reason": {"terminal_repair_blocked_reason": "blocked"},
    ADMISSION_DEFERRAL_METADATA_KEY: {ADMISSION_DEFERRAL_METADATA_KEY: _DEFERRAL},
    QUEUE_RECORD_SYNC_KEY: {QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_REPAIR_PENDING},
    QUEUE_RECORD_SYNC_UPDATED_AT_KEY: {QUEUE_RECORD_SYNC_UPDATED_AT_KEY: _LATER},
    QUEUE_RECORD_SYNC_OWNER_PID_KEY: {QUEUE_RECORD_SYNC_OWNER_PID_KEY: 123},
    QUEUE_RECORD_SYNC_OWNER_START_KEY: {QUEUE_RECORD_SYNC_OWNER_START_KEY: "boot:1"},
    QUEUE_RECORD_SYNC_TOKEN_KEY: {QUEUE_RECORD_SYNC_TOKEN_KEY: "other-token"},
    QUEUE_RECORD_SYNC_BLOCKED_KEY: {QUEUE_RECORD_SYNC_BLOCKED_KEY: {"reason": "blocked"}},
    f"{_PENDING_KEY}=True": {_PENDING_KEY: True},
    "reaction_dir": {"reaction_dir": "/pins/runs/other"},
    "force": {"force": True},
    "selected_inp": {"selected_inp": "/pins/runs/job/other.inp"},
    "worker_log": {"worker_log": "/pins/runs/logs/other.log"},
    "execution_snapshot": {"execution_snapshot": {"version": 3}},
    "unknown_future_key": {"unknown_future_key": 1},
}
_FIELD_VARIANTS: dict[str, dict[str, Any]] = {
    "queue_id": {"queue_id": "q-other"},
    "app_name": {"app_name": "other_app"},
    "task_id": {"task_id": "orca-other"},
    "task_kind": {"task_kind": "other_kind"},
    "engine": {"engine": "other_engine"},
    "priority": {"priority": 3},
    "enqueued_at": {"enqueued_at": _LATER},
    "status": {"status": QueueStatus.RUNNING},
    "started_at": {"started_at": _STARTED_AT},
    "finished_at": {"finished_at": _FINISHED_AT},
    "cancel_requested": {"cancel_requested": True},
    "error": {"error": "boom"},
}


def test_generation_identity() -> None:
    base = _row("/pins/runs/job")
    variants: dict[str, QueueEntry] = {"base": base}
    for name, update in _METADATA_VARIANTS.items():
        variants[f"metadata.{name}"] = _with_metadata(base, update)
    variants[f"metadata.{_PENDING_KEY}=absent"] = _with_metadata(base, {}, _PENDING_KEY)
    for name, fields in _FIELD_VARIANTS.items():
        variants[f"field.{name}"] = replace(base, **fields)
    table = {
        name: {
            "generation_token": queue_entry_generation_token(entry),
            "core_same_generation": queue_entries_same_generation(entry, base),
            "adapter_same_publication_generation": (
                adapter.queue_entries_same_publication_generation(entry, base)
            ),
        }
        for name, entry in variants.items()
    }
    assert_pin("queue_generation_identity.json", table)


# --- writer fences ------------------------------------------------------------


def _current_rows(job: Path) -> dict[str, QueueEntry]:
    pending = _row(str(job))
    running = replace(pending, status=QueueStatus.RUNNING, started_at=_STARTED_AT)
    return {
        "pending": pending,
        "pending_deferred": _with_metadata(pending, admission_deferral_update("scratch full")),
        "pending_deferral_expired": _with_metadata(
            pending, {ADMISSION_DEFERRAL_METADATA_KEY: {"reason": "scratch", "not_before": _LATER}}
        ),
        "running": running,
        "running_cancel_requested": replace(running, cancel_requested=True),
        "completed": replace(running, status=QueueStatus.COMPLETED, finished_at=_FINISHED_AT),
        "failed": replace(
            running, status=QueueStatus.FAILED, finished_at=_FINISHED_AT, error="boom"
        ),
        "cancelled": replace(
            running, status=QueueStatus.CANCELLED, finished_at=_FINISHED_AT, cancel_requested=True
        ),
    }


def _expected_entries(row: QueueEntry) -> dict[str, QueueEntry | None]:
    """Snapshots a caller may hold for ``row``: exact, or one field or key apart."""
    other_lifecycle = replace(
        row,
        status=QueueStatus.RUNNING if row.status == QueueStatus.PENDING else QueueStatus.PENDING,
        started_at="",
        finished_at="",
        error="",
        cancel_requested=False,
    )
    return {
        "none": None,
        "exact": row,
        "other_lifecycle": other_lifecycle,
        "metadata.run_id": _with_metadata(row, {"run_id": "run-other"}),
        "metadata.attempt": _with_metadata(row, {"attempt": 9}),
        f"metadata.{QUEUE_RECORD_SYNC_UPDATED_AT_KEY}": _with_metadata(
            row, {QUEUE_RECORD_SYNC_UPDATED_AT_KEY: _LATER}
        ),
        f"metadata.{_PENDING_KEY}": _with_metadata(row, {_PENDING_KEY: True}),
        f"metadata.{ADMISSION_DEFERRAL_METADATA_KEY}": _with_metadata(
            row, {ADMISSION_DEFERRAL_METADATA_KEY: _DEFERRAL}
        ),
        "enqueued_at": replace(row, enqueued_at=_LATER),
        "priority": replace(row, priority=3),
        "task_id": replace(row, task_id="orca-other"),
    }


_TASK_IDS: dict[str, str | None] = {
    "none": None,
    "equal": _TASK_ID,
    "padded": f" {_TASK_ID} ",
    "different": "orca-other",
}

_Writer = Callable[[Path, QueueEntry | None, str | None], Any]


def _probe(root: Path, expected: QueueEntry | None) -> bool:
    assert expected is not None
    return adapter.cancellation_probe(root, expected)()


# name -> (call, expected_entry use, takes expected_task_id); the use is
# "optional", "required" (never None) or "absent" (not a parameter).
_WRITERS: dict[str, tuple[_Writer, str, bool]] = {
    "mark_completed": (
        lambda root, e, t: adapter.mark_completed(
            root, _QUEUE_ID, expected_entry=e, expected_task_id=t
        ),
        "optional",
        True,
    ),
    "mark_failed": (
        lambda root, e, t: adapter.mark_failed(
            root, _QUEUE_ID, error="boom2", expected_entry=e, expected_task_id=t
        ),
        "optional",
        True,
    ),
    "mark_failed(fence_only)": (
        lambda root, e, t: adapter.mark_failed(
            root,
            _QUEUE_ID,
            error="fenced",
            publish_terminal_side_effects=False,
            expected_entry=e,
            expected_task_id=t,
        ),
        "optional",
        True,
    ),
    "mark_cancelled": (
        lambda root, e, t: adapter.mark_cancelled(
            root, _QUEUE_ID, expected_entry=e, expected_task_id=t
        ),
        "optional",
        True,
    ),
    "requeue_running_entry": (
        lambda root, e, t: adapter.requeue_running_entry(
            root, _QUEUE_ID, expected_entry=e, expected_task_id=t
        ),
        "optional",
        True,
    ),
    "requeue_running_entry(admission_deferral)": (
        lambda root, e, t: adapter.requeue_running_entry(
            root,
            _QUEUE_ID,
            expected_entry=e,
            expected_task_id=t,
            admission_deferral_reason="scratch full",
        ),
        "optional",
        True,
    ),
    "cancel": (
        lambda root, e, _t: adapter.cancel(root, _QUEUE_ID, expected_entry=e),
        "optional",
        False,
    ),
    "update_metadata": (
        lambda root, e, _t: adapter.update_metadata(
            root, _QUEUE_ID, {"pin_marker": 1}, expected_entry=e
        ),
        "optional",
        False,
    ),
    "update_metadata(running_without_cancel)": (
        lambda root, e, _t: adapter.update_metadata(
            root,
            _QUEUE_ID,
            {"pin_marker": 1},
            expected_entry=e,
            require_running_without_cancel_requested=True,
        ),
        "optional",
        False,
    ),
    "update_terminal": (
        lambda root, e, t: adapter.update_terminal(
            root, _QUEUE_ID, "failed", error="corrected", expected_entry=e, expected_task_id=t
        ),
        "optional",
        True,
    ),
    "dequeue_entry_if_pending": (
        lambda root, e, _t: adapter.dequeue_entry_if_pending(root, _QUEUE_ID, expected_entry=e),
        "optional",
        False,
    ),
    "get_cancel_requested": (
        lambda root, e, t: adapter.get_cancel_requested(
            root, _QUEUE_ID, expected_entry=e, expected_task_id=t
        ),
        "optional",
        True,
    ),
    "cancellation_probe": (
        lambda root, e, _t: _probe(root, e),
        "required",
        False,
    ),
    "cancel_requested_ids": (
        lambda root, _e, t: adapter.cancel_requested_ids(root, {_QUEUE_ID: t}),
        "absent",
        True,
    ),
}


def _result(value: Any) -> str:
    if isinstance(value, QueueEntry):
        return f"entry({value.status.value})"
    if isinstance(value, set):
        return repr(sorted(value))
    return repr(value)


def _shown(value: Any) -> str:
    if isinstance(value, QueueStatus):
        return value.value
    if isinstance(value, str) and value[:4].isdigit():
        return "<timestamp>"
    return repr(value)


def _row_change(before: QueueEntry, after: QueueEntry | None) -> str:
    """Changed fields with their new value and changed metadata keys (+ added, - removed, ~ changed)."""
    if after is None:
        return "row missing"
    parts = [
        f"{field.name}={_shown(getattr(after, field.name))}"
        for field in dataclasses.fields(QueueEntry)
        if field.name != "metadata" and getattr(after, field.name) != getattr(before, field.name)
    ]
    for key in sorted(before.metadata.keys() | after.metadata.keys()):
        if key not in after.metadata:
            parts.append(f"-{key}")
        elif key not in before.metadata:
            parts.append(f"+{key}")
        elif after.metadata[key] != before.metadata[key]:
            parts.append(f"~{key}")
    return " ".join(parts) or "unchanged"


def _stored_row(root: Path) -> QueueEntry | None:
    return next(
        (entry for entry in queue_store.list_queue(root) if entry.queue_id == _QUEUE_ID), None
    )


def _cell(root: Path, row: QueueEntry, call: Callable[[], Any]) -> str:
    queue_store.save_entries(root, [row])
    try:
        result = _result(call())
    except Exception as exc:  # noqa: BLE001 - the raised type is the pinned answer
        result = f"raise {type(exc).__name__}: {exc}"
    return f"{result} | {_row_change(row, _stored_row(root))}"


def test_writer_fences(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    job = root / "job"
    job.mkdir(parents=True)
    n = Normalizer({tmp_path: "<tmp>"})
    table: dict[str, str] = {}
    for writer_name, (writer, expected_use, takes_task) in _WRITERS.items():
        for row_name, row in _current_rows(job).items():
            expected_entries = _expected_entries(row)
            combos = [(name, "none") for name in expected_entries]
            combos += [
                (expected_name, task_name)
                for expected_name in ("none", "exact")
                for task_name in _TASK_IDS
                if task_name != "none"
            ]
            for expected_name, task_name in combos:
                if (
                    (expected_name == "none" and expected_use == "required")
                    or (expected_name != "none" and expected_use == "absent")
                    or (task_name != "none" and not takes_task)
                ):
                    continue
                expected = expected_entries[expected_name]
                task_id = _TASK_IDS[task_name]
                table[f"{writer_name}|{row_name}|expected={expected_name}|task={task_name}"] = (
                    n.text(
                        _cell(
                            root,
                            row,
                            partial(writer, root, expected, task_id),
                        )
                    )
                )
    assert_pin("queue_writer_fences.json", table)


# --- requeue fields -------------------------------------------------------------


def test_requeue_fields(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    job = root / "job"
    job.mkdir(parents=True)
    running = replace(
        _row(str(job)), status=QueueStatus.RUNNING, started_at=_STARTED_AT, error="stale error"
    )
    cases: dict[str, tuple[QueueEntry, Callable[[], Any]]] = {
        "orphan_requeue": (
            running,
            lambda: reconcile_orphaned_running_entries(root, ignore_worker_pid=True),
        ),
        "core_requeue": (running, lambda: adapter.requeue_running_entry(root, _QUEUE_ID)),
        "core_requeue_admission_deferral": (
            running,
            lambda: adapter.requeue_running_entry(
                root, _QUEUE_ID, admission_deferral_reason="scratch full"
            ),
        ),
        "orphan_cancel_requested": (
            replace(running, cancel_requested=True),
            lambda: reconcile_orphaned_running_entries(root, ignore_worker_pid=True),
        ),
        "core_cancel_while_running": (
            running,
            lambda: (
                _result(adapter.cancel(root, _QUEUE_ID)),
                adapter.requeue_running_entry(root, _QUEUE_ID),
            ),
        ),
    }
    table: dict[str, Any] = {}
    for name, (row, call) in cases.items():
        queue_store.save_entries(root, [row])
        returned = call()
        after = _stored_row(root)
        assert after is not None
        table[name] = Normalizer({tmp_path: "<tmp>"})(
            {
                "returned": list(returned) if isinstance(returned, tuple) else returned,
                "row_change": _row_change(row, after),
                "row": queue_store.entry_to_dict(after),
            }
        )
    assert_pin("queue_requeue_fields.json", table)


# --- queued notifications -------------------------------------------------------


def test_cancel_with_snapshot_taken_before_queued_notification_claim(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    job = root / "job"
    job.mkdir(parents=True)
    row = _with_metadata(_row(str(job)), {_PENDING_KEY: True})
    queue_store.save_entries(root, [row])
    snapshot = adapter.get_entry_by_id(root, _QUEUE_ID)
    claimed = notifications._claim_queued_notifications(root)
    cancelled = adapter.cancel(root, _QUEUE_ID, expected_entry=snapshot)
    after = _stored_row(root)
    table = {
        "claimed_queue_ids": [event["queue_id"] for event in claimed],
        "cancel_result": _result(cancelled),
        "row_change_after_claim_and_cancel": _row_change(row, after),
    }
    assert_pin("queue_cancel_after_notification_claim.json", table)


def test_repaired_and_claimed_row_gets_its_queued_notification(harness: Harness) -> None:
    job = harness.job("repaired")
    assert harness.cli("run-dir", str(job))[0] == 0
    [row] = harness.rows()
    queue_id = row["queue_id"]
    assert adapter.update_metadata(
        harness.runs, queue_id, {QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_REPAIR_PENDING}
    )
    sends_before = len(harness.channel.sends)
    worker = OrcaQueueWorker(
        load_config(str(harness.config)), str(harness.config), max_concurrent=1
    )
    status, reserved = worker._reserve_next_entry()
    try:
        _join_notification_senders()
        after = adapter.get_entry_by_id(harness.runs, queue_id)
        assert after is not None
        n = harness.n
        table = {
            "sends_before_worker_pass": sends_before,
            "sends": len(harness.channel.sends),
            "reserve_status": status,
            "claimed_queue_id": n.text(reserved.entry.queue_id) if reserved else None,
            "row_status": after.status.value,
            "row_record_sync": after.metadata[QUEUE_RECORD_SYNC_KEY],
            f"row_{_PENDING_KEY}": after.metadata[_PENDING_KEY],
            "notifications": n(
                [
                    dataclasses.asdict(message)
                    for message in harness.channel.sends
                    if isinstance(message, Message)
                ]
            ),
        }
    finally:
        if reserved is not None:
            worker._release_admission_slot(reserved.admission_token)
    assert_pin("queue_repaired_claim_notification.json", table)


# --- worker child argv ----------------------------------------------------------


def test_worker_child_argv() -> None:
    variants: dict[str, dict[str, Any]] = {
        "admission_token": {"admission_token": "slot_pin"},
        "admission_token_none": {"admission_token": None},
        "admission_token_empty": {"admission_token": ""},
        "path_queue_root": {"queue_root": Path("/pins/runs/"), "admission_token": "slot_pin"},
    }
    table: dict[str, list[str]] = {}
    for name, overrides in variants.items():
        kwargs: dict[str, Any] = {
            "config_path": "/pins/orca_auto.yaml",
            "queue_root": "/pins/runs",
            "queue_id": _QUEUE_ID,
            **overrides,
        }
        argv = build_worker_child_command(**kwargs)
        assert argv[0] == sys.executable
        table[name] = ["<sys.executable>", *argv[1:]]
    assert_pin("queue_worker_child_argv.json", table)
