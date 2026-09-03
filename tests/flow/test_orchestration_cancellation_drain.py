"""The workflow worker journals cancel transitions a crashed cancel left behind."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.flow.orchestration.workflow_cancellation import (
    _CANCELLATION_TRANSITIONS_KEY,
    _stored_cancellation_transitions,
    drain_cancellation_transitions,
)
from orca_auto.flow.registry.journal import (
    append_workflow_journal_event,
    list_workflow_journal,
)
from orca_auto.flow.runtime import _workflow_advance_deps
from orca_auto.flow.runtime import advance as runtime_advance
from orca_auto.flow.runtime.models import _WorkflowCycle
from orca_auto.flow.state import (
    acquire_workflow_lock,
    load_workflow_payload,
    resolve_workflow_workspace,
    write_workflow_payload,
)

_TRANSITION = {
    "event_id": "wf_evt_cancel_1",
    "occurred_at": "2026-08-11T05:20:00+00:00",
    "previous_status": "running",
    "status": "cancel_requested",
}


def _workspace(tmp_path: Path, transitions: list[dict[str, Any]]) -> tuple[Path, Path]:
    root = tmp_path / "runs"
    workspace = root / "workflows" / "wf-1"
    workspace.mkdir(parents=True)
    write_workflow_payload(
        workspace,
        {
            "workflow_id": "wf-1",
            "template_name": "ts_pipeline",
            "status": "cancel_requested",
            "stages": [],
            "metadata": {_CANCELLATION_TRANSITIONS_KEY: transitions},
        },
    )
    return root, workspace


def _real_drain(root: Path, workspace: Path, append: Any = append_workflow_journal_event) -> int:
    return drain_cancellation_transitions(
        root,
        workspace,
        resolve_workflow_workspace_fn=resolve_workflow_workspace,
        acquire_workflow_lock_fn=acquire_workflow_lock,
        load_workflow_payload_fn=load_workflow_payload,
        write_workflow_payload_fn=write_workflow_payload,
        append_workflow_journal_event_fn=append,
    )


def test_drain_appends_the_cancel_rows_and_dedupes_one_the_command_did_write(
    tmp_path: Path,
) -> None:
    # The cancel command appended the first transition and crashed before it
    # rewrote the payload: the journal has the row, the payload still stores
    # it. The drain must append the same bytes (the journal refuses a same-id
    # row with different content) and then clear the payload.
    second = {**_TRANSITION, "event_id": "wf_evt_cancel_2", "status": "cancelled"}
    second["previous_status"] = "cancel_requested"
    root, workspace = _workspace(tmp_path, [_TRANSITION, second])
    append_workflow_journal_event(
        root,
        event_id=_TRANSITION["event_id"],
        occurred_at=_TRANSITION["occurred_at"],
        event_type="workflow_status_changed",
        workflow_id="wf-1",
        template_name="ts_pipeline",
        previous_status="running",
        status="cancel_requested",
        reason="cancel_requested",
        worker_session_id="workflow_cancel",
    )

    assert _real_drain(root, workspace) == 2

    rows = [
        row for row in list_workflow_journal(root) if row["event_type"] == "workflow_status_changed"
    ]
    assert sorted(row["event_id"] for row in rows) == ["wf_evt_cancel_1", "wf_evt_cancel_2"]
    assert {row["worker_session_id"] for row in rows} == {"workflow_cancel"}
    assert {row["reason"] for row in rows} == {"cancel_requested"}
    assert load_workflow_payload(workspace)["metadata"][_CANCELLATION_TRANSITIONS_KEY] == []
    # A second drain finds nothing and appends nothing.
    assert _real_drain(root, workspace) == 0
    assert len(list_workflow_journal(root)) == len(rows)


def test_drain_writes_nothing_when_no_transition_is_stored(tmp_path: Path) -> None:
    root, workspace = _workspace(tmp_path, [])
    payload_path = workspace / "workflow.json"
    before = payload_path.stat().st_mtime_ns

    assert _real_drain(root, workspace) == 0

    assert payload_path.stat().st_mtime_ns == before
    assert list_workflow_journal(root) == []


def test_drain_keeps_the_transitions_it_could_not_append(tmp_path: Path) -> None:
    second = {**_TRANSITION, "event_id": "wf_evt_cancel_2", "status": "cancelled"}
    second["previous_status"] = "cancel_requested"
    root, workspace = _workspace(tmp_path, [_TRANSITION, second])
    appended: list[str] = []

    def append(_root: Path, **event: Any) -> dict[str, Any]:
        if event["event_id"] == "wf_evt_cancel_2":
            raise ValueError("journal event id already exists with different content")
        appended.append(event["event_id"])
        return event

    assert _real_drain(root, workspace, append) == 1

    assert appended == ["wf_evt_cancel_1"]
    stored = load_workflow_payload(workspace)["metadata"][_CANCELLATION_TRANSITIONS_KEY]
    assert [entry["event_id"] for entry in stored] == ["wf_evt_cancel_2"]


def test_drain_holds_the_workspace_lock_around_load_and_write(tmp_path: Path) -> None:
    root, workspace = _workspace(tmp_path, [_TRANSITION])
    order: list[str] = []

    class _Lock:
        def __init__(self, path: Path, *, timeout_seconds: float) -> None:
            assert Path(path) == workspace
            assert timeout_seconds == 5.0

        def __enter__(self) -> None:
            order.append("lock")

        def __exit__(self, *_exc: Any) -> None:
            order.append("unlock")

    def load(path: Path) -> dict[str, Any]:
        order.append("load")
        return load_workflow_payload(path)

    def write(path: Path, payload: dict[str, Any]) -> Path:
        order.append("write")
        return write_workflow_payload(path, payload)

    drained = drain_cancellation_transitions(
        root,
        workspace,
        resolve_workflow_workspace_fn=resolve_workflow_workspace,
        acquire_workflow_lock_fn=_Lock,
        load_workflow_payload_fn=load,
        write_workflow_payload_fn=write,
        append_workflow_journal_event_fn=lambda *_a, **_k: order.append("append"),
    )

    assert drained == 1
    # The first load is the lockless pre-read; the locked one is authoritative.
    assert order == ["load", "lock", "load", "append", "write", "unlock"]


def test_drain_refuses_a_workspace_outside_the_workflow_root(tmp_path: Path) -> None:
    # A registry row can carry a raw workspace string that resolves nowhere
    # under this root; the drain must not lock, read or write it, nor drain
    # an in-root workspace that merely shares its basename.
    root, _ = _workspace(tmp_path, [_TRANSITION])
    twin = root / "wf-1"
    twin.mkdir()
    write_workflow_payload(
        twin,
        {"workflow_id": "wf-1", "metadata": {_CANCELLATION_TRANSITIONS_KEY: [_TRANSITION]}},
    )
    twin_before = (twin / "workflow.json").read_bytes()
    outside = tmp_path / "elsewhere" / "wf-1"
    outside.mkdir(parents=True)
    write_workflow_payload(
        outside,
        {"workflow_id": "wf-1", "metadata": {_CANCELLATION_TRANSITIONS_KEY: [_TRANSITION]}},
    )
    before = (outside / "workflow.json").read_bytes()

    assert _real_drain(root, outside) == 0

    assert (outside / "workflow.json").read_bytes() == before
    assert not (outside / "workflow.lock").exists()
    assert (twin / "workflow.json").read_bytes() == twin_before
    assert list_workflow_journal(root) == []


def test_drain_empties_a_stored_list_with_no_valid_transition(tmp_path: Path) -> None:
    # A list that holds only invalid entries journals nothing, but leaving it
    # in place would hold the record out of every terminal clear forever.
    root, workspace = _workspace(tmp_path, [{"event_id": "", "status": "cancelled"}])

    assert _real_drain(root, workspace) == 0

    assert load_workflow_payload(workspace)["metadata"][_CANCELLATION_TRANSITIONS_KEY] == []
    assert list_workflow_journal(root) == []


def test_drain_does_not_recreate_a_removed_workspace(tmp_path: Path) -> None:
    # A cancelled record whose workspace an operator deleted stays in the
    # registry; the drain must not leave a lock file behind in a recreated
    # directory on every cycle.
    root = tmp_path / "runs"
    workspace = root / "workflows" / "wf-gone"
    root.mkdir()

    assert _real_drain(root, workspace) == 0

    assert not workspace.exists()
    assert list_workflow_journal(root) == []


def test_stored_transitions_keep_only_valid_deduplicated_entries() -> None:
    metadata = {
        _CANCELLATION_TRANSITIONS_KEY: [
            _TRANSITION,
            dict(_TRANSITION),
            {**_TRANSITION, "previous_status": "cancel_requested"},
            {**_TRANSITION, "status": "completed"},
            "not-a-dict",
        ]
    }

    assert _stored_cancellation_transitions(metadata) == [_TRANSITION]


def _cycle(root: Path) -> _WorkflowCycle:
    return _WorkflowCycle(
        root=root,
        cycle_started_at="2026-09-03T12:00:00+00:00",
        session_id="worker-session",
        requested_submit_ready=False,
        cycle_submit_ready=False,
        admission_blocked=False,
    )


def _recording_drain(order: list[str]) -> Any:
    def drain(*_args: Any, **_kwargs: Any) -> int:
        order.append("drain")
        return 1

    return drain


def _deps(
    order: list[str], workspace: Path, **overrides: Any
) -> runtime_advance.WorkflowAdvanceDeps:
    fields: dict[str, Any] = {
        "advance_workflow_fn": lambda **_k: pytest.fail("terminal records are not advanced"),
        "resolve_workflow_workspace_fn": lambda *, target, workflow_root: workspace,
        "load_workflow_payload_fn": lambda _path: {"workflow_id": "wf-1"},
        "safe_workflow_summary_fn": lambda *_a, **_k: {},
        "workflow_is_terminal_status_fn": lambda status: (
            status in {"cancelled", "failed", "completed"}
        ),
        "workflow_needs_terminal_child_sync_fn": lambda *_a, **_k: False,
        "append_workflow_advance_failed_event_fn": lambda *_a, **_k: None,
        "append_workflow_advanced_events_fn": lambda *_a, **_k: order.append("advanced_events"),
        "append_workflow_journal_event_fn": lambda *_a, **_k: None,
        "workflow_skipped_terminal_result_fn": lambda record, *, previous_status: (
            "skipped",
            previous_status,
        ),
        "workflow_advance_failed_result_fn": lambda *_a, **_k: None,
        "workflow_advanced_result_fn": lambda *_a, **_k: "advanced",
        "drain_cancellation_transitions_fn": _recording_drain(order),
        "acquire_workflow_lock_fn": lambda *_a, **_k: None,
        "write_workflow_payload_fn": lambda *_a, **_k: None,
    }
    fields.update(overrides)
    return runtime_advance.WorkflowAdvanceDeps(**fields)


def test_advanced_outcome_drains_before_journaling_its_own_events(tmp_path: Path) -> None:
    order: list[str] = []
    workspace = tmp_path / "wf-1"
    record = SimpleNamespace(workflow_id="wf-1", workspace_dir=str(workspace), status="running")

    outcome = runtime_advance.advanced_workflow_outcome(
        cycle=_cycle(tmp_path),
        record=record,
        payload={"workflow_id": "wf-1", "status": "cancel_requested"},
        previous_status="running",
        previous_summary={},
        workspace_dir=str(workspace),
        terminal_sync=False,
        deps=_deps(order, workspace),
    )

    assert outcome.outcome == "advanced"
    assert order == ["drain", "advanced_events"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("cancelled", ["drain"]), ("failed", ["drain"]), ("completed", [])],
)
def test_terminal_record_is_drained_only_for_a_cancel_outcome(
    tmp_path: Path, status: str, expected: list[str]
) -> None:
    # A cancel that resolved straight to `cancelled` is never advanced again;
    # its stored transitions are drained from the skip path instead.
    order: list[str] = []
    workspace = tmp_path / "wf-1"
    record = SimpleNamespace(workflow_id="wf-1", workspace_dir=str(workspace), status=status)

    outcome = runtime_advance.advance_workflow_record_outcome(
        cycle=_cycle(tmp_path),
        record=record,
        options=None,  # type: ignore[arg-type]
        deps=_deps(order, workspace),
    )

    assert outcome.outcome == "skipped"
    assert order == expected


def test_worker_advance_deps_wire_the_drain() -> None:
    deps = _workflow_advance_deps()

    assert deps.drain_cancellation_transitions_fn is drain_cancellation_transitions
    assert deps.acquire_workflow_lock_fn is acquire_workflow_lock
    assert deps.write_workflow_payload_fn is write_workflow_payload
