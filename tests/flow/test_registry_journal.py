from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.core.messaging import SendResult, render_discord_embed
from orca_auto.flow import registry
from orca_auto.flow.registry import _notifications as registry_notifications
from orca_auto.flow.registry import journal as workflow_journal
from tests.flow.registry_test_helpers import (
    patch_file_locks as _patch_file_locks,
)
from tests.flow.registry_test_helpers import (
    patch_now_utc_iso as _patch_now_utc_iso,
)


def test_maybe_notify_journal_event_sends_message_and_swallows_channel_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sent_messages: list[Any] = []

    class FakeChannel:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail

        @property
        def enabled(self) -> bool:
            return True

        def send(self, message: Any, *, silent: bool = False) -> SendResult:
            if self.fail:
                raise RuntimeError("channel failed")
            sent_messages.append(message)
            return SendResult(sent=True)

    event = {
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_notify",
        "template_name": "reaction_ts_search",
        "status": "running",
        "previous_status": "planned",
        "worker_session_id": "session-notify",
    }

    monkeypatch.setattr(
        registry_notifications, "journal_notification_enabled", lambda event_type: True
    )
    monkeypatch.setattr(
        registry_notifications, "messenger_channel_from_env", lambda: FakeChannel(fail=False)
    )
    workflow_journal._maybe_notify_journal_event(event, tmp_path)
    workflow_journal._maybe_notify_journal_event(
        {
            "event_type": "workflow_stage_submitted",
            "workflow_id": "wf_notify",
            "template_name": "reaction_ts_search",
            "stage_id": "xtb_path_search_01",
            "engine": "xtb",
            "task_kind": "path_search",
            "metadata": {"engine": "xtb"},
        },
        tmp_path,
    )
    workflow_journal._maybe_notify_journal_event(
        {
            "event_type": "workflow_stage_submitted",
            "workflow_id": "wf_notify",
            "template_name": "reaction_ts_search",
            "stage_id": "orca_ts",
            "engine": "orca",
            "task_kind": "dft",
            "metadata": {"engine": "orca"},
        },
        tmp_path,
    )
    workflow_journal._maybe_notify_journal_event(
        {
            "event_type": "workflow_phase_finished",
            "workflow_id": "wf_notify",
            "template_name": "reaction_ts_search",
            "worker_session_id": "session-notify",
            "metadata": {
                "phase": "xtb",
                "phase_label": "xTB",
                "phase_outcome": "completed",
                "stage_count": 2,
                "stage_status_counts": {"completed": 2},
            },
        },
        tmp_path,
    )

    # The opt-in above was widened to every event type, so every event sends —
    # including the two stage events. Nothing overrides an explicit opt-in.
    assert len(sent_messages) == 4
    status_fields = {
        item["name"]: item["value"]
        for item in render_discord_embed(sent_messages[0]).get("fields", [])
    }
    phase_fields = {
        item["name"]: item["value"]
        for item in render_discord_embed(sent_messages[3]).get("fields", [])
    }
    assert status_fields["Workflow"] == "`wf_notify`"
    assert phase_fields["Phase"] == "`xTB`"

    monkeypatch.setattr(
        registry_notifications, "messenger_channel_from_env", lambda: FakeChannel(fail=True)
    )
    workflow_journal._maybe_notify_journal_event(event, tmp_path)
    assert len(sent_messages) == 4


def test_append_workflow_journal_event_writes_jsonl_and_returns_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    notifications: list[dict[str, Any]] = []
    token_values = iter(["wf_evt_1", "wf_evt_2", "wf_evt_3"])
    time_values = iter(
        [
            "2026-04-19T01:00:00+00:00",
            "2026-04-19T01:05:00+00:00",
            "2026-04-19T01:10:00+00:00",
        ]
    )

    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(workflow_journal, "timestamped_token", lambda prefix: next(token_values))
    _patch_now_utc_iso(monkeypatch, lambda: next(time_values))
    monkeypatch.setattr(
        workflow_journal,
        "_maybe_notify_journal_event",
        lambda event, workflow_root: notifications.append(
            {"event": dict(event), "workflow_root": str(Path(workflow_root).resolve())}
        ),
    )

    first = registry.append_workflow_journal_event(
        tmp_path,
        event_type="workflow_status_changed",
        workflow_id="wf_1",
        template_name="reaction_ts_search",
        status="running",
        previous_status="planned",
        reason="advanced",
        worker_session_id="session-1",
        metadata={"attempt": 1},
    )
    second = registry.append_workflow_journal_event(
        tmp_path,
        event_type="workflow_stage_submitted",
        workflow_id="wf_2",
        template_name="reaction_ts_search",
        stage_id="xtb_path_search_01",
        engine="xtb",
        task_kind="path_search",
        stage_status="queued",
        previous_stage_status="planned",
        worker_session_id="session-stage",
    )
    third = registry.append_workflow_journal_event(
        tmp_path,
        event_type="workflow_stage_handoff_ready",
        workflow_id="wf_2",
        template_name="reaction_ts_search",
        stage_id="xtb_path_search_01",
        engine="xtb",
        task_kind="path_search",
        stage_status="completed",
        reaction_handoff_status="ready",
        previous_reaction_handoff_status="queued",
        reason="xtb_ts_guess_ready",
    )

    journal_path = registry.workflow_journal_path(tmp_path)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    second_raw = json.loads(lines[1])
    third_raw = json.loads(lines[2])

    assert first["event_id"] == "wf_evt_1"
    assert first["occurred_at"] == "2026-04-19T01:00:00+00:00"
    assert first["metadata"] == {"attempt": 1}
    assert second["event_id"] == "wf_evt_2"
    assert third["event_id"] == "wf_evt_3"
    assert len(lines) == 3
    assert json.loads(lines[0])["workflow_id"] == "wf_1"
    assert second_raw["workflow_id"] == "wf_2"
    assert second_raw["stage_id"] == "xtb_path_search_01"
    assert second_raw["engine"] == "xtb"
    assert second_raw["task_kind"] == "path_search"
    assert second_raw["previous_stage_status"] == "planned"
    assert second_raw["stage_status"] == "queued"
    assert third_raw["reaction_handoff_status"] == "ready"
    assert third_raw["previous_reaction_handoff_status"] == "queued"
    assert notifications[0]["workflow_root"] == str(tmp_path.resolve())
    assert notifications[1]["event"]["stage_status"] == "queued"
    assert notifications[2]["event"]["reason"] == "xtb_ts_guess_ready"


def test_append_workflow_journal_event_is_idempotent_for_caller_owned_event_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    notifications: list[str] = []
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(
        workflow_journal,
        "_maybe_notify_journal_event",
        lambda event, workflow_root: notifications.append(str(event["event_id"])),
    )
    kwargs: dict[str, Any] = {
        "event_id": "wf_evt_cancel_stable",
        "occurred_at": "2026-08-12T03:58:00+00:00",
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_cancel_stable",
        "previous_status": "running",
        "status": "cancelled",
        "reason": "cancel_requested",
        "worker_session_id": "workflow_cancel",
    }

    first = registry.append_workflow_journal_event(tmp_path, **kwargs)
    second = registry.append_workflow_journal_event(tmp_path, **kwargs)

    assert second == first
    assert notifications == ["wf_evt_cancel_stable"]
    assert len(registry.workflow_journal_path(tmp_path).read_text().splitlines()) == 1

    with pytest.raises(ValueError, match="event id already exists with different content"):
        registry.append_workflow_journal_event(tmp_path, **{**kwargs, "status": "failed"})


def test_append_workflow_journal_event_reestablishes_durability_for_existing_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    kwargs: dict[str, Any] = {
        "event_id": "wf_evt_retry_durability",
        "occurred_at": "2026-08-12T04:00:30+00:00",
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_retry_durability",
        "previous_status": "running",
        "status": "cancelled",
    }
    real_fsync = workflow_journal.os.fsync
    fsync_attempts = 0
    fsynced_directories: list[Path] = []

    def fail_first_fsync(descriptor: int) -> None:
        nonlocal fsync_attempts
        fsync_attempts += 1
        if fsync_attempts == 1:
            raise OSError("injected journal fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(workflow_journal.os, "fsync", fail_first_fsync)
    monkeypatch.setattr(
        workflow_journal,
        "fsync_directory",
        lambda path: fsynced_directories.append(Path(path)),
    )

    with pytest.raises(OSError, match="injected journal fsync failure"):
        registry.append_workflow_journal_event(tmp_path, **kwargs)

    event = registry.append_workflow_journal_event(tmp_path, **kwargs)

    assert event["event_id"] == "wf_evt_retry_durability"
    assert fsync_attempts == 2
    assert fsynced_directories == [tmp_path.resolve()]
    assert len(registry.workflow_journal_path(tmp_path).read_text().splitlines()) == 1


def test_append_workflow_journal_event_bounds_existing_event_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    kwargs: dict[str, Any] = {
        "event_id": "wf_evt_bounded_lookup",
        "occurred_at": "2026-08-12T04:00:45+00:00",
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_bounded_lookup",
        "previous_status": "running",
        "status": "cancelled",
    }
    first = registry.append_workflow_journal_event(tmp_path, **kwargs)
    real_read = workflow_journal.read_confined_tail_lines
    observed_limits: list[int | None] = []

    def bounded_read(*args: Any, **kwargs: Any) -> list[str]:
        observed_limits.append(kwargs.get("max_bytes"))
        return real_read(*args, **kwargs)

    monkeypatch.setattr(workflow_journal, "read_confined_tail_lines", bounded_read)

    assert registry.append_workflow_journal_event(tmp_path, **kwargs) == first
    assert observed_limits and all(
        isinstance(limit, int) and limit > 0 for limit in observed_limits
    )


def test_append_workflow_journal_event_fails_closed_when_lookup_exceeds_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    kwargs: dict[str, Any] = {
        "event_id": "wf_evt_outside_bounded_tail",
        "occurred_at": "2026-08-12T04:00:50+00:00",
        "event_type": "workflow_status_changed",
        "workflow_id": "wf_outside_bounded_tail",
        "previous_status": "running",
        "status": "cancelled",
    }
    path = registry.workflow_journal_path(tmp_path)
    original = json.dumps({"event_id": "other"}) + "\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        workflow_journal,
        "read_confined_tail_lines",
        lambda *args, **kwargs: [],
    )

    def reject_oversized_lookup(*args: Any, **kwargs: Any) -> str:
        assert kwargs["max_bytes"] == workflow_journal.CALLER_EVENT_LOOKUP_MAX_BYTES
        raise ValueError("workflow journal exceeds its read limit")

    monkeypatch.setattr(workflow_journal, "read_confined_text", reject_oversized_lookup)

    with pytest.raises(ValueError, match="exceeds its read limit"):
        registry.append_workflow_journal_event(tmp_path, **kwargs)

    assert path.read_text(encoding="utf-8") == original


def test_append_workflow_journal_event_separates_a_truncated_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    path = registry.workflow_journal_path(tmp_path)
    path.write_text('{"event_id":"truncated"', encoding="utf-8")

    event = registry.append_workflow_journal_event(
        tmp_path,
        event_id="wf_evt_after_truncated_tail",
        occurred_at="2026-08-12T04:00:00+00:00",
        event_type="workflow_status_changed",
        workflow_id="wf_after_truncated_tail",
        previous_status="running",
        status="cancelled",
    )

    assert registry.list_workflow_journal(tmp_path) == [event]
    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"event_id":"truncated"',
        json.dumps(event, ensure_ascii=True),
    ]


def test_append_workflow_journal_event_fsyncs_before_notification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_file_locks(monkeypatch)
    fsynced_descriptors: list[int] = []
    fsynced_directories: list[Path] = []
    monkeypatch.setattr(
        workflow_journal.os,
        "fsync",
        lambda descriptor: fsynced_descriptors.append(descriptor),
    )
    monkeypatch.setattr(
        workflow_journal,
        "fsync_directory",
        lambda path: fsynced_directories.append(Path(path)),
        raising=False,
    )

    def assert_durable_before_notification(event: dict[str, Any], workflow_root: Path) -> None:
        assert event["event_id"] == "wf_evt_durable"
        assert fsynced_descriptors
        assert fsynced_directories == [tmp_path.resolve()]

    monkeypatch.setattr(
        workflow_journal,
        "_maybe_notify_journal_event",
        assert_durable_before_notification,
    )

    registry.append_workflow_journal_event(
        tmp_path,
        event_id="wf_evt_durable",
        occurred_at="2026-08-12T04:01:00+00:00",
        event_type="workflow_status_changed",
        workflow_id="wf_durable",
        previous_status="running",
        status="cancelled",
    )


def test_list_workflow_journal_uses_append_commit_order_and_applies_bounded_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    journal_path = registry.workflow_journal_path(tmp_path)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_id": "evt_1",
                        "occurred_at": "2026-04-19T00:01:00+00:00",
                        "event_type": "a",
                    }
                ),
                "not-json",
                "",
                json.dumps(
                    {
                        "event_id": "evt_3",
                        "occurred_at": "2026-04-19T00:03:00+00:00",
                        "event_type": "c",
                    }
                ),
                json.dumps(
                    {
                        "event_id": "evt_2",
                        "occurred_at": "2026-04-19T00:02:00+00:00",
                        "event_type": "b",
                    }
                ),
                "{truncated",
            ]
        ),
        encoding="utf-8",
    )
    _patch_file_locks(monkeypatch)
    monkeypatch.setattr(
        workflow_journal,
        "read_confined_text",
        lambda *args, **kwargs: pytest.fail("bounded journal reads must not load the full file"),
    )

    result = registry.list_workflow_journal(tmp_path, limit=2)

    assert [item["event_id"] for item in result] == ["evt_2", "evt_3"]
