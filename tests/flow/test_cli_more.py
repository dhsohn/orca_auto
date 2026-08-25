from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.flow.cli import workflow as cli_workflow


def test_emit_worker_payload_formats_text_and_json(capsys) -> None:
    payload = {
        "cycle_started_at": "2026-04-19T17:00:00+00:00",
        "worker_session_id": "worker_1",
        "discovered_count": 3,
        "advanced_count": 2,
        "skipped_count": 1,
        "failed_count": 0,
        "workflow_results": [
            {
                "workflow_id": "wf_1",
                "template_name": "reaction_ts_search",
                "previous_status": "planned",
                "status": "running",
                "advanced": True,
                "reason": "submitted",
            }
        ],
    }

    cli_workflow._emit_worker_payload(payload, json_mode=False, single_cycle=False)
    stdout = capsys.readouterr().out
    assert "cycle_started_at: 2026-04-19T17:00:00+00:00 worker_session_id=worker_1" in stdout
    assert (
        "- wf_1 template=reaction_ts_search previous=planned status=running advanced=yes" in stdout
    )
    assert "reason=submitted" in stdout

    cli_workflow._emit_worker_payload(payload, json_mode=True, single_cycle=True)
    pretty = json.loads(capsys.readouterr().out)
    assert pretty["worker_session_id"] == "worker_1"

    cli_workflow._emit_worker_payload(payload, json_mode=True, single_cycle=False)
    compact = capsys.readouterr().out.strip()
    assert '"workflow_results"' in compact
    assert "\n" not in compact


def test_cmd_workflow_worker_handles_negative_cycles_and_lock_timeout(monkeypatch, capsys) -> None:
    args = SimpleNamespace(
        once=False,
        max_cycles=-1,
        interval_seconds=1.0,
        lock_timeout_seconds=5.0,
        refresh_registry=False,
        refresh_each_cycle=False,
        json=False,
        workflow_root="/tmp/wf",
        no_submit=False,
        crest_config=None,
        xtb_config=None,
        orca_config=None,
    )
    assert cli_workflow.cmd_workflow_worker(args) == 1
    assert "--max-cycles must be >= 0" in capsys.readouterr().err

    writes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    @contextmanager
    def raising_lock(*args: Any, **kwargs: Any):
        raise TimeoutError("already running")
        yield

    monkeypatch.setattr(cli_workflow, "file_lock", raising_lock)
    monkeypatch.setattr(
        cli_workflow, "workflow_worker_lock_path", lambda workflow_root: Path("/tmp/worker.lock")
    )
    monkeypatch.setattr(cli_workflow, "now_utc_iso", lambda: "2026-04-19T17:20:00+00:00")
    monkeypatch.setattr(cli_workflow, "timestamped_token", lambda prefix: "wf_worker_01")
    monkeypatch.setattr(
        cli_workflow,
        "write_workflow_worker_state",
        lambda workflow_root, **kwargs: writes.append(kwargs),
    )
    monkeypatch.setattr(
        cli_workflow,
        "append_workflow_journal_event",
        lambda workflow_root, **kwargs: events.append(kwargs),
    )

    args.max_cycles = 0
    result = cli_workflow.cmd_workflow_worker(args)

    assert result == 1
    assert "worker_lock_error: already running" in capsys.readouterr().err
    assert writes == []
    assert events[-1]["event_type"] == "worker_lock_error"


def test_cmd_workflow_worker_single_cycle_and_keyboard_interrupt(monkeypatch, capsys) -> None:
    writes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    @contextmanager
    def fake_lock(*args: Any, **kwargs: Any):
        yield

    monkeypatch.setattr(cli_workflow, "file_lock", fake_lock)
    monkeypatch.setattr(
        cli_workflow, "workflow_worker_lock_path", lambda workflow_root: Path("/tmp/worker.lock")
    )
    monkeypatch.setattr(cli_workflow, "now_utc_iso", lambda: "2026-04-19T17:30:00+00:00")
    monkeypatch.setattr(cli_workflow, "timestamped_token", lambda prefix: "wf_worker_02")
    monkeypatch.setattr(
        cli_workflow,
        "write_workflow_worker_state",
        lambda workflow_root, **kwargs: writes.append(kwargs),
    )
    monkeypatch.setattr(
        cli_workflow,
        "append_workflow_journal_event",
        lambda workflow_root, **kwargs: events.append(kwargs),
    )
    monkeypatch.setattr(
        cli_workflow,
        "advance_workflow_registry_once",
        lambda **kwargs: {
            "cycle_started_at": "2026-04-19T17:30:00+00:00",
            "worker_session_id": kwargs["worker_session_id"],
            "discovered_count": 1,
            "advanced_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "workflow_results": [],
        },
    )
    monkeypatch.setattr(cli_workflow.time, "sleep", lambda seconds: None)

    args = SimpleNamespace(
        once=True,
        max_cycles=0,
        interval_seconds=1.0,
        lock_timeout_seconds=5.0,
        refresh_registry=True,
        refresh_each_cycle=False,
        json=False,
        workflow_root="/tmp/wf",
        no_submit=True,
        crest_config=None,
        xtb_config=None,
        orca_config=None,
    )

    assert cli_workflow.cmd_workflow_worker(args) == 0
    stdout = capsys.readouterr().out
    assert "worker_session_id=wf_worker_02" in stdout
    assert [item["status"] for item in writes] == ["running", "running", "stopped"]
    assert writes[1]["minimum_heartbeat_interval_seconds"] == 30.0
    assert writes[1]["last_cycle_started_at"] == "2026-04-19T17:30:00+00:00"
    assert writes[-1]["status"] == "stopped"
    assert writes[-1]["metadata"]["stop_reason"] == "max_cycles_reached"
    assert events[0]["event_type"] == "worker_started"
    assert events[-1]["event_type"] == "worker_stopped"

    def raise_keyboard_interrupt(**kwargs: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_workflow, "advance_workflow_registry_once", raise_keyboard_interrupt)
    writes.clear()
    events.clear()
    args.once = False
    args.max_cycles = 0
    assert cli_workflow.cmd_workflow_worker(args) == 130
    assert writes[-1]["status"] == "interrupted"
    assert events[-1]["event_type"] == "worker_interrupted"
