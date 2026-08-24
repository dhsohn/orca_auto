from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from orca_auto.core.engines import own_engine_accept_entry
from orca_auto.core.queue import (
    dequeue_next,
    enqueue,
    list_queue,
    mark_cancelled,
    mark_completed,
    request_cancel,
)
from orca_auto.core.queue.engine.artifacts import matching_terminal_state_for_entry
from orca_auto.core.queue.generation import queue_entry_generation_token
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
    QUEUE_RECORD_SYNC_REPAIR_PENDING,
    queue_record_sync_metadata,
)
from orca_auto.core.queue.types import QueueEntry
from orca_auto.flow.engines.xtb import queue_runtime as queue_cmd
from orca_auto.flow.engines.xtb import state as state_mod
from tests.engine_artifact_helpers import artifact_payload
from tests.engine_process_helpers import process_one_xtb_for_test
from tests.flow.engines.xtb.factories import (
    make_cfg as _make_cfg,
)
from tests.flow.engines.xtb.factories import (
    make_entry as _make_entry,
)


def _append_and_return(items: Any, value: Any, result: Any) -> Any:
    items.append(value)
    return result


def _dequeued_running_entry(entry: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(**{**vars(entry), "status": SimpleNamespace(value="running")})


def _write_removed_report(job_dir: Path, payload: dict[str, Any]) -> None:
    (job_dir / "job_report.json").write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_worker_adopts_terminal_artifact_with_guarded_index_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "terminal-adoption"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="job-adoption",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "rxn-adoption",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    entry = replace(running, started_at="2026-04-20T00:00:00Z")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id="job-adoption",
            queue_id=pending.queue_id,
            app_name="orca_auto_xtb",
            task_id="job-adoption",
            generation=queue_entry_generation_token(entry),
            job_dir=str(job_dir),
            status="completed",
            reason="completed",
            exit_code=0,
            primary_path=str(selected_xyz),
            selected_xyz_path=str(selected_xyz),
            updated_at="2026-04-20T00:00:01Z",
            engine_payload={
                "job_type": "opt",
                "reaction_key": "rxn-adoption",
                "input_summary": {},
            },
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(
        queue_cmd,
        "upsert_job_record",
        lambda _cfg, **kwargs: upserts.append(kwargs),
    )

    assert queue_cmd._adopt_terminal_artifacts(cfg, queue_root, entry)
    assert upserts == [
        {
            "job_id": "job-adoption",
            "status": "completed",
            "job_dir": job_dir,
            "job_type": "opt",
            "selected_input_xyz": str(selected_xyz),
            "reaction_key": "rxn-adoption",
            "resource_request": {},
            "resource_actual": {},
        }
    ]


def test_terminal_adoption_finalizes_racing_cancel_consistently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "cancel-adoption"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="cancel-adoption-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "cancel-adoption",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-04-20T00:00:00Z")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id=pending.queue_id,
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(current_claim),
            job_dir=str(job_dir),
            status="completed",
            reason="completed",
            exit_code=0,
            primary_path=str(selected_xyz),
            updated_at="2026-04-20T00:00:01Z",
            engine_payload={
                "job_type": "opt",
                "reaction_key": "cancel-adoption",
                "input_summary": {},
            },
        ),
    )
    assert request_cancel(queue_root, pending.queue_id, expected_entry=running) is not None
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)

    [cancelled] = list_queue(queue_root)
    assert cancelled.status.value == "cancelled"
    assert cancelled.cancel_requested is False
    assert [row["status"] for row in upserts] == ["cancelled"]
    persisted_state = state_mod.load_state(job_dir)
    assert persisted_state is not None
    assert persisted_state["status"]["state"] == "cancelled"
    assert not (job_dir / "job_report.json").exists()


def test_worker_repairs_own_publication_and_ignores_foreign_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "repair-publication"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    publication = queue_record_sync_metadata(
        QUEUE_RECORD_SYNC_REPAIR_PENDING,
        token="xtb-repair-token",
        owner_pid=0,
    )
    own = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="xtb-repair-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "repair",
            "resource_request": {},
            **publication,
        },
    )
    foreign = enqueue(
        queue_root,
        app_name="orca_auto_orca",
        task_id="orca-foreign-job",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={"reaction_dir": str(queue_root / "foreign"), **publication},
    )
    recorded: list[str] = []

    def record_queued(_cfg: object, _submission: object, entry: Any) -> bool:
        recorded.append(str(entry.task_id))
        return True

    monkeypatch.setattr(
        queue_cmd,
        "_record_queued_submission",
        record_queued,
    )

    assert queue_cmd._repair_xtb_queue_publications(SimpleNamespace(cfg=cfg))

    rows = {entry.queue_id: entry for entry in list_queue(queue_root)}
    assert recorded == [own.task_id]
    assert rows[own.queue_id].metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_COMPLETE
    assert (
        rows[foreign.queue_id].metadata[QUEUE_RECORD_SYNC_KEY] == QUEUE_RECORD_SYNC_REPAIR_PENDING
    )
    claimed = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert claimed is not None and claimed.queue_id == own.queue_id


def test_terminal_adoption_rejects_foreign_artifact_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "foreign-artifact"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(
        job_dir,
        selected_xyz,
        queue_id="queue-current",
        job_id="job-current",
        job_type="opt",
    )
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="crest",
            job_id="job-current",
            queue_id="queue-current",
            app_name="orca_auto_crest",
            task_id="job-current",
            job_dir=str(job_dir),
            status="completed",
            primary_path=str(selected_xyz),
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, entry)
    assert upserts == []


def test_terminal_adoption_uses_exact_state_and_ignores_removed_report(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "stale-report"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="job-current",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "current",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-07-13T01:00:00+00:00")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id=pending.queue_id,
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(current_claim),
            job_dir=str(job_dir),
            status="completed",
            reason="completed",
            exit_code=0,
            primary_path=str(selected_xyz),
            updated_at="2026-07-13T02:00:00+00:00",
            engine_payload={"job_type": "opt", "reaction_key": "current", "input_summary": {}},
        ),
    )
    _write_removed_report(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id="stale-queue",
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(current_claim),
            status="failed",
            reason="stale_failed",
            primary_path=str(selected_xyz),
            engine_payload={"job_type": "opt", "reaction_key": "stale"},
        ),
    )

    assert queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)
    [terminal] = list_queue(queue_root)
    assert terminal.status.value == "completed"
    assert terminal.error == ""


@pytest.mark.parametrize(
    "report_updated_at",
    [
        "",
        "not-a-timestamp",
        "2026-07-13T01:59:59+00:00",
        "2026-07-13T02:00:00+00:00",
        "2026-07-13T02:00:01+00:00",
    ],
)
def test_report_only_terminal_is_not_supported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_updated_at: str,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "report-only"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="report-only-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "report-only",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-07-13T02:00:00+00:00")
    _write_removed_report(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id=pending.queue_id,
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(current_claim),
            job_dir=str(job_dir),
            status="completed",
            reason="report_only_completed",
            primary_path=str(selected_xyz),
            updated_at=report_updated_at,
            engine_payload={
                "job_type": "opt",
                "reaction_key": "report-only",
                "input_summary": {},
            },
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)
    [persisted] = list_queue(queue_root)
    assert persisted.status.value == "running"
    assert upserts == []


@pytest.mark.parametrize(
    "report_updated_at",
    ["2026-07-13T01:00:00+00:00", "2026-07-13T02:00:00+00:00", ""],
)
def test_terminal_adoption_ignores_removed_report_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_updated_at: str,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "same-status-stale-report"
    job_dir.mkdir()
    current_xyz = job_dir / "current.xyz"
    stale_xyz = job_dir / "stale.xyz"
    for path in (current_xyz, stale_xyz):
        path.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="same-status-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(current_xyz),
            "job_type": "opt",
            "reaction_key": "current",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-07-13T01:00:00+00:00")
    common_fields: dict[str, Any] = {
        "engine": "xtb",
        "job_id": pending.task_id,
        "queue_id": pending.queue_id,
        "app_name": pending.app_name,
        "task_id": pending.task_id,
        "generation": queue_entry_generation_token(current_claim),
        "status": "completed",
    }
    state_mod.write_state(
        job_dir,
        artifact_payload(
            **common_fields,
            job_dir=str(job_dir),
            reason="completed",
            exit_code=0,
            primary_path=str(current_xyz),
            updated_at="2026-07-13T02:00:00+00:00",
            engine_payload={"job_type": "opt", "reaction_key": "current", "input_summary": {}},
        ),
    )
    _write_removed_report(
        job_dir,
        artifact_payload(
            **common_fields,
            reason="stale_completed",
            primary_path=str(stale_xyz),
            updated_at=report_updated_at,
            engine_payload={"job_type": "sp", "reaction_key": "stale", "input_summary": {}},
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)
    assert upserts[0]["selected_input_xyz"] == str(current_xyz)
    assert upserts[0]["job_type"] == "opt"
    assert upserts[0]["reaction_key"] == "current"
    repaired_state = state_mod.load_state(job_dir)
    assert repaired_state is not None
    assert repaired_state["input"]["primary_path"] == str(current_xyz)
    removed_report = json.loads((job_dir / "job_report.json").read_text(encoding="utf-8"))
    assert removed_report["input"]["primary_path"] == str(stale_xyz)


def test_newer_removed_report_cannot_override_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "newer-report"
    job_dir.mkdir()
    stale_xyz = job_dir / "stale.xyz"
    current_xyz = job_dir / "current.xyz"
    for path in (stale_xyz, current_xyz):
        path.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="newer-report-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(current_xyz),
            "job_type": "opt",
            "reaction_key": "current",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-07-13T01:00:00+00:00")
    common_fields: dict[str, Any] = {
        "engine": "xtb",
        "job_id": pending.task_id,
        "queue_id": pending.queue_id,
        "app_name": pending.app_name,
        "task_id": pending.task_id,
        "generation": queue_entry_generation_token(current_claim),
        "status": "completed",
    }
    state_mod.write_state(
        job_dir,
        artifact_payload(
            **common_fields,
            job_dir=str(job_dir),
            reason="completed",
            exit_code=0,
            primary_path=str(stale_xyz),
            updated_at="2026-07-13T01:00:00+00:00",
            engine_payload={"job_type": "sp", "reaction_key": "stale", "input_summary": {}},
        ),
    )
    _write_removed_report(
        job_dir,
        artifact_payload(
            **common_fields,
            reason="current_completed",
            primary_path=str(current_xyz),
            updated_at="2026-07-13T02:00:00+00:00",
            engine_payload={"job_type": "opt", "reaction_key": "current", "input_summary": {}},
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)
    assert upserts == []


def test_exact_removed_report_does_not_rebuild_foreign_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "foreign-state-current-report"
    job_dir.mkdir()
    selected_xyz = job_dir / "current.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="current-report-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "current",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id="foreign-job",
            queue_id="foreign-queue",
            app_name=pending.app_name,
            task_id="foreign-job",
            job_dir=str(job_dir),
            status="failed",
            primary_path=str(selected_xyz),
            updated_at="2999-01-01T00:00:00+00:00",
            engine_payload={"job_type": "opt", "reaction_key": "foreign"},
        ),
    )
    _write_removed_report(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id=pending.queue_id,
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(running),
            status="completed",
            reason="current_completed",
            primary_path=str(selected_xyz),
            updated_at="2999-01-01T00:00:01+00:00",
            engine_payload={"job_type": "opt", "reaction_key": "current", "input_summary": {}},
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, running)
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["job"]["id"] == "foreign-job"
    assert state["job"]["queue_id"] == "foreign-queue"
    assert upserts == []


def test_terminal_state_repair_preserves_command(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "same-timestamp-command"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="same-timestamp-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "same-timestamp",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-07-13T01:00:00+00:00")
    common: dict[str, Any] = {
        "engine": "xtb",
        "job_id": pending.task_id,
        "queue_id": pending.queue_id,
        "app_name": pending.app_name,
        "task_id": pending.task_id,
        "generation": queue_entry_generation_token(current_claim),
        "status": "completed",
        "reason": "completed",
        "exit_code": 0,
        "primary_path": str(selected_xyz),
        "updated_at": "2026-07-13T02:00:00+00:00",
    }
    engine_payload = {
        "job_type": "opt",
        "reaction_key": "same-timestamp",
        "input_summary": {},
    }
    state_mod.write_state(
        job_dir,
        artifact_payload(
            **common,
            job_dir=str(job_dir),
            engine_payload={**engine_payload, "command": ["xtb", str(selected_xyz), "--opt"]},
        ),
    )

    assert queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["engine_payload"]["command"] == ["xtb", str(selected_xyz), "--opt"]
    assert not (job_dir / "job_report.json").exists()


def test_failed_terminal_repair_preserves_zero_exit_code(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "failed-exit-zero"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="failed-exit-zero-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "failed-exit-zero",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    current_claim = replace(running, started_at="2026-07-13T01:00:00+00:00")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id=pending.queue_id,
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(current_claim),
            job_dir=str(job_dir),
            status="failed",
            reason="scientific_validation_failed",
            exit_code=0,
            primary_path=str(selected_xyz),
            updated_at="2026-07-13T02:00:00+00:00",
            engine_payload={
                "job_type": "opt",
                "reaction_key": "failed-exit-zero",
                "input_summary": {},
            },
        ),
    )

    assert queue_cmd._adopt_terminal_artifacts(cfg, queue_root, current_claim)
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"] == {
        "state": "failed",
        "reason": "scientific_validation_failed",
        "exit_code": 0,
    }
    assert not (job_dir / "job_report.json").exists()


def test_terminal_without_exact_artifacts_records_repair_blocker(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "missing-terminal-artifacts"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="missing-artifacts-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "missing-artifacts",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    terminal = mark_completed(queue_root, running.queue_id, expected_entry=running)
    assert terminal is not None

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, terminal)
    [persisted] = list_queue(queue_root)
    assert persisted.status.value == "completed"
    assert persisted.metadata["terminal_repair_blocked_reason"] == "terminal_state_unrecoverable"


def test_terminal_state_older_than_current_claim_is_rejected(tmp_path: Path) -> None:
    job_dir = tmp_path / "older-terminal-state"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(
        job_dir,
        selected_xyz,
        queue_id="older-state-queue",
        job_id="older-state-job",
        status="running",
    )
    entry.started_at = "2026-07-13T03:00:00+00:00"
    common_fields: dict[str, Any] = {
        "engine": "xtb",
        "job_id": entry.task_id,
        "queue_id": entry.queue_id,
        "app_name": entry.app_name,
        "task_id": entry.task_id,
        "generation": queue_entry_generation_token(cast(QueueEntry, entry)),
        "job_dir": str(job_dir),
        "status": "completed",
        "primary_path": str(selected_xyz),
    }

    matched = matching_terminal_state_for_entry(
        state=artifact_payload(
            **common_fields,
            updated_at="2026-07-13T01:00:00+00:00",
        ),
        entry=cast(QueueEntry, entry),
        engine="xtb",
        job_dir=job_dir,
    )

    assert matched is None


def test_completed_state_with_nonzero_exit_is_not_terminal_evidence(tmp_path: Path) -> None:
    job_dir = tmp_path / "invalid-completed-state"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(
        job_dir,
        selected_xyz,
        queue_id="invalid-completed-queue",
        job_id="invalid-completed-job",
        status="running",
    )
    entry.started_at = "2026-07-13T01:00:00+00:00"
    state = artifact_payload(
        engine="xtb",
        job_id=entry.task_id,
        queue_id=entry.queue_id,
        app_name=entry.app_name,
        task_id=entry.task_id,
        generation=queue_entry_generation_token(cast(QueueEntry, entry)),
        job_dir=str(job_dir),
        status="completed",
        reason="completed",
        exit_code=1,
        primary_path=str(selected_xyz),
        updated_at="2026-07-13T02:00:00+00:00",
    )

    assert (
        matching_terminal_state_for_entry(
            state=state,
            entry=cast(QueueEntry, entry),
            engine="xtb",
            job_dir=job_dir,
        )
        is None
    )


def test_running_state_is_not_completed_by_removed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "running-state-stale-report"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="job-current",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "current",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    common_fields: dict[str, Any] = {
        "engine": "xtb",
        "job_id": pending.task_id,
        "queue_id": pending.queue_id,
        "app_name": pending.app_name,
        "task_id": pending.task_id,
        "primary_path": str(selected_xyz),
        "engine_payload": {"job_type": "opt", "reaction_key": "current"},
    }
    state_mod.write_state(
        job_dir,
        artifact_payload(
            **common_fields,
            job_dir=str(job_dir),
            status="running",
            reason="current_attempt",
        ),
    )
    _write_removed_report(
        job_dir,
        artifact_payload(
            **common_fields,
            status="completed",
            reason="stale_attempt",
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))

    assert not queue_cmd._adopt_terminal_artifacts(cfg, queue_root, running)
    [unchanged] = list_queue(queue_root)
    assert unchanged.status.value == "running"
    assert upserts == []


def test_queue_worker_parser_has_no_organize_flags() -> None:
    args = queue_cmd.build_parser().parse_args(["--config", "/tmp/orca_auto.yaml"])

    assert args.config == "/tmp/orca_auto.yaml"
    assert not hasattr(args, "auto_organize")
    assert not hasattr(args, "no_auto_organize")

    with pytest.raises(SystemExit):
        queue_cmd.build_parser().parse_args(["--config", "/tmp/orca_auto.yaml", "--auto-organize"])


def test_process_one_returns_blocked_when_no_admission_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: None)

    assert process_one_xtb_for_test(queue_cmd, cfg) == "blocked"


def test_process_one_returns_idle_and_releases_reserved_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    released: list[tuple[object, object]] = []

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: "slot-1")
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((str(root), token))
    )

    assert process_one_xtb_for_test(queue_cmd, cfg) == "idle"
    assert released == [(cfg.runtime.admission_root, "slot-1")]


def test_queue_worker_starts_up_to_max_concurrent_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    entries = []
    for index in range(2):
        job_dir = queue_root / f"job-{index}"
        job_dir.mkdir()
        selected_xyz = job_dir / f"input-{index}.xyz"
        selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
        entries.append(
            _make_entry(
                job_dir,
                selected_xyz,
                queue_id=f"queue-{index}",
                job_id=f"job-{index}",
                status="pending",
            )
        )

    slots = iter(["slot-1", "slot-2"])
    dequeued = iter(entries)
    started: list[tuple[str, str, str]] = []

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: next(slots))
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: entries)
    monkeypatch.setattr(queue_cmd, "activate_reserved_slot", lambda *args, **kwargs: object())
    real_deps = queue_cmd._queue_worker_deps()
    monkeypatch.setattr(
        queue_cmd,
        "_queue_worker_deps",
        lambda: replace(
            real_deps,
            dequeue_next_entry=lambda _cfg: (queue_root, _dequeued_running_entry(next(dequeued))),
        ),
    )

    def fake_start_background_job_process(
        *,
        config_path: str,
        queue_root: Path,
        entry: object,
        admission_token: str,
    ) -> _Process:
        started.append((config_path, str(queue_root), admission_token))
        return _Process(len(started) + 100)

    monkeypatch.setattr(
        queue_cmd,
        "_start_background_job_process",
        fake_start_background_job_process,
    )

    worker = queue_cmd.QueueWorker(
        cfg,
        config_path="/tmp/orca_auto.yaml",
        max_concurrent=2,
    )

    assert worker._fill_slots() == "processed"
    assert sorted(worker._running) == ["queue-0", "queue-1"]
    assert started == [
        ("/tmp/orca_auto.yaml", str(queue_root), "slot-1"),
        ("/tmp/orca_auto.yaml", str(queue_root), "slot-2"),
    ]


def test_queue_worker_check_cancel_requests_is_child_side_noop(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, status="pending")

    signals: list[int] = []

    class _Process:
        pid = 1234

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

        def send_signal(self, signum: int) -> None:
            signals.append(signum)

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._running[entry.queue_id] = queue_cmd._RunningJob(
        queue_root=queue_root,
        entry=entry,
        process=_Process(),
        admission_token="slot-1",
    )

    worker._check_cancel_requests()
    worker._check_cancel_requests()

    assert signals == []
    assert worker._running[entry.queue_id].cancel_requested is False


def test_queue_worker_shutdown_requeues_running_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)

    graceful_terminated: list[int] = []
    hard_terminated: list[int] = []
    requeued: list[tuple[Path, str]] = []
    released: list[tuple[str, str]] = []

    class _Process:
        pid = 9001

        def __init__(self) -> None:
            self._terminated = False

        def poll(self) -> int | None:
            return 0 if self._terminated else None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            graceful_terminated.append(self.pid)
            self._terminated = True

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        queue_cmd,
        "_terminate_process",
        lambda proc: hard_terminated.append(proc.pid),
    )
    monkeypatch.setattr(
        queue_cmd,
        "requeue_running_entry",
        lambda root, queue_id, **_kwargs: _append_and_return(
            requeued,
            (root, queue_id),
            entry,
        ),
    )
    monkeypatch.setattr(queue_cmd, "_queue_entry_by_id", lambda _root, _queue_id: entry)
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((str(root), token))
    )

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._shutdown_requested = True
    worker._running[entry.queue_id] = queue_cmd._RunningJob(
        queue_root=queue_root,
        entry=entry,
        process=_Process(),
        admission_token="slot-1",
    )

    worker._shutdown_all()

    assert graceful_terminated == [9001]
    assert hard_terminated == []
    assert requeued == [(queue_root, "queue-1")]
    assert released == [(cfg.runtime.admission_root, "slot-1")]
    assert worker._running == {}
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"]["state"] == "queued"
    assert state["status"]["reason"] == "worker_shutdown"
    assert state["recovery"]["pending"] is True


def test_queue_worker_run_once_waits_for_child_completion_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz)
    sleep_calls: list[float] = []
    released: list[tuple[str, str]] = []

    class _Process:
        def __init__(self) -> None:
            self.pid = 4444
            self._poll_values = iter([None, 0])

        def poll(self) -> int | None:
            return next(self._poll_values, 0)

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda _root: 0)
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: [entry])
    monkeypatch.setattr(queue_cmd, "_try_reserve_admission_slot", lambda _cfg: "slot-1")
    monkeypatch.setattr(queue_cmd, "activate_reserved_slot", lambda *args, **kwargs: object())
    real_deps = queue_cmd._queue_worker_deps()
    monkeypatch.setattr(
        queue_cmd,
        "_queue_worker_deps",
        lambda: replace(
            real_deps,
            dequeue_next_entry=lambda _cfg: (queue_root, _dequeued_running_entry(entry)),
        ),
    )
    monkeypatch.setattr(
        queue_cmd,
        "_start_background_job_process",
        lambda **kwargs: _Process(),
    )
    monkeypatch.setattr(
        queue_cmd,
        "_load_terminal_summary",
        lambda queue_root, entry, rc=None: queue_cmd._TerminalSummary(
            queue_id=entry.queue_id,
            job_id=entry.task_id,
            status="completed",
            reason="xtb_ok",
        ),
    )
    monkeypatch.setattr(
        queue_cmd, "_ensure_terminal_queue_status", lambda queue_root, entry, summary: None
    )
    monkeypatch.setattr(
        queue_cmd, "release_slot", lambda root, token: released.append((str(root), token))
    )
    monkeypatch.setattr(queue_cmd.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    exit_code = worker.run_once()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status: completed" in output
    assert "reason: xtb_ok" in output
    assert sleep_calls == [queue_cmd.POLL_INTERVAL_SECONDS]
    assert released == [(cfg.runtime.admission_root, "slot-1")]


def test_queue_worker_reconcile_worker_state_requeues_stale_running_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-1"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, status="running")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id="job-1",
            job_dir=str(job_dir),
            status="running",
            primary_path=str(selected_xyz),
            selected_xyz_path=str(selected_xyz),
            engine_payload={
                "job_type": "path_search",
                "reaction_key": "rxn-1",
            },
        )
        | {"process": {"worker_pid": 999_999}},
    )
    requeued: list[tuple[Path, str]] = []

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda _root: 0)
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: [entry])
    monkeypatch.setattr(queue_cmd, "_pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        queue_cmd,
        "requeue_running_entry",
        lambda root, queue_id, **_kwargs: _append_and_return(
            requeued,
            (root, queue_id),
            entry,
        ),
    )

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._reconcile_worker_state()

    assert requeued == [(queue_root, "queue-1")]
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"]["state"] == "queued"
    assert state["status"]["reason"] == "crashed_recovery"
    assert state["recovery"]["pending"] is True


def test_queue_worker_reconcile_preserves_running_entry_with_live_child_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "job-live"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("3\ncandidate\nH 0 0 0\n", encoding="utf-8")
    entry = _make_entry(job_dir, selected_xyz, status="running")
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id="job-1",
            job_dir=str(job_dir),
            status="running",
            primary_path=str(selected_xyz),
            selected_xyz_path=str(selected_xyz),
            engine_payload={
                "job_type": "path_search",
                "reaction_key": "rxn-1",
            },
        )
        | {"process": {"worker_pid": 4242}},
    )
    requeued: list[tuple[Path, str]] = []

    monkeypatch.setattr(queue_cmd, "reconcile_stale_slots", lambda _root: 0)
    monkeypatch.setattr(queue_cmd, "list_queue", lambda _root: [entry])
    monkeypatch.setattr(queue_cmd, "_pid_is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(
        queue_cmd,
        "requeue_running_entry",
        lambda root, queue_id, **_kwargs: _append_and_return(
            requeued,
            (root, queue_id),
            entry,
        ),
    )

    worker = queue_cmd.QueueWorker(cfg, config_path="/tmp/cfg.yaml")
    worker._reconcile_worker_state()

    assert requeued == []
    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"]["state"] == "running"


def test_cmd_queue_worker_constructs_xtb_worker_without_organize_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    seen: list[tuple[str, str]] = []

    class _FakeWorker:
        def __init__(
            self,
            cfg_obj: object,
            *,
            config_path: str,
            max_concurrent: int | None = None,
        ) -> None:
            seen.append(("init", config_path))

        def run_once(self) -> int:
            seen.append(("run_once", ""))
            return 17

        def run(self) -> int:
            seen.append(("run", ""))
            return 23

    monkeypatch.setattr(queue_cmd, "load_config", lambda _path=None: cfg)
    monkeypatch.setattr(queue_cmd, "QueueWorker", _FakeWorker)
    monkeypatch.setattr(queue_cmd, "default_config_path", lambda: "/tmp/default-orca_auto.yaml")

    exit_code = queue_cmd.cmd_queue_worker(
        SimpleNamespace(
            config=None,
        )
    )

    assert seen[0] == ("init", "/tmp/default-orca_auto.yaml")
    assert exit_code == 23
    assert seen[-1] == ("run", "")


def test_terminal_reconcile_repairs_partial_cancelled_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "partial-terminal-repair"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="partial-terminal-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "partial",
        },
    )
    running = dequeue_next(queue_root, accept_entry_fn=own_engine_accept_entry("xtb"))
    assert running is not None
    cancelled = mark_cancelled(
        queue_root,
        pending.queue_id,
        error="cancel_requested",
        expected_entry=running,
    )
    assert cancelled is not None
    common_fields: dict[str, Any] = {
        "engine": "xtb",
        "job_id": pending.task_id,
        "queue_id": pending.queue_id,
        "app_name": pending.app_name,
        "task_id": pending.task_id,
        "generation": queue_entry_generation_token(cancelled),
        "primary_path": str(selected_xyz),
        "updated_at": "2999-01-01T00:00:00+00:00",
        "engine_payload": {"job_type": "sp", "reaction_key": "stale"},
    }
    state_mod.write_state(
        job_dir,
        artifact_payload(
            **common_fields,
            job_dir=str(job_dir),
            status="cancelled",
            reason="cancel_requested",
        ),
    )
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(
        queue_cmd,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_root, list_queue(queue_root)[0])],
    )
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))
    monkeypatch.setattr(
        queue_cmd,
        "resolve_job_location_for_cfg",
        lambda _cfg, _job_id: (
            queue_root,
            SimpleNamespace(status="cancelled", original_run_dir=str(job_dir)),
        ),
    )

    worker = SimpleNamespace(cfg=cfg)
    queue_cmd._sync_terminal_running_entries(worker)

    state = state_mod.load_state(job_dir)
    assert state is not None
    assert state["status"]["state"] == "cancelled"
    assert state["engine_payload"]["job_type"] == "opt"
    assert state["engine_payload"]["reaction_key"] == "partial"
    assert list_queue(queue_root)[0].metadata["reaction_key"] == "partial"
    assert upserts[-1]["status"] == "cancelled"

    queue_cmd._sync_terminal_running_entries(worker)

    assert len(upserts) == 1
    assert not (job_dir / "job_report.json").exists()


def test_terminal_reconcile_finalizes_direct_pending_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_cfg(tmp_path)
    queue_root = Path(cfg.runtime.allowed_root)
    job_dir = queue_root / "pending-cancel-repair"
    job_dir.mkdir()
    selected_xyz = job_dir / "input.xyz"
    selected_xyz.write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    pending = enqueue(
        queue_root,
        app_name="orca_auto_xtb",
        task_id="pending-cancel-job",
        task_kind="xtb_opt",
        engine="xtb",
        metadata={
            "job_dir": str(job_dir),
            "selected_input_xyz": str(selected_xyz),
            "job_type": "opt",
            "reaction_key": "pending-cancel",
        },
    )
    state_mod.write_state(
        job_dir,
        artifact_payload(
            engine="xtb",
            job_id=pending.task_id,
            queue_id=pending.queue_id,
            app_name=pending.app_name,
            task_id=pending.task_id,
            generation=queue_entry_generation_token(pending),
            job_dir=str(job_dir),
            status="queued",
            reason="queued",
            primary_path=str(selected_xyz),
            updated_at="2999-01-01T00:00:00+00:00",
            engine_payload={"job_type": "opt", "reaction_key": "pending-cancel"},
        ),
    )
    cancelled = request_cancel(queue_root, pending.queue_id, expected_entry=pending)
    assert cancelled is not None and cancelled.status.value == "cancelled"
    upserts: list[dict[str, object]] = []
    monkeypatch.setattr(
        queue_cmd,
        "queue_entries_with_roots",
        lambda _cfg: [(queue_root, list_queue(queue_root)[0])],
    )
    monkeypatch.setattr(queue_cmd, "upsert_job_record", lambda _cfg, **kw: upserts.append(kw))
    monkeypatch.setattr(
        queue_cmd,
        "resolve_job_location_for_cfg",
        lambda _cfg, _job_id: (
            queue_root,
            SimpleNamespace(status="cancelled", original_run_dir=str(job_dir)),
        ),
    )

    worker = SimpleNamespace(cfg=cfg)
    queue_cmd._sync_terminal_running_entries(worker)

    [persisted] = list_queue(queue_root)
    state = state_mod.load_state(job_dir)
    assert persisted.status.value == "cancelled"
    assert persisted.cancel_requested is False
    assert persisted.error == "cancel_requested"
    assert state is not None
    assert state["status"]["state"] == "queued"
    assert persisted.metadata["terminal_repair_blocked_reason"] == "terminal_state_unrecoverable"
    assert upserts == []
    assert not (job_dir / "job_report.json").exists()

    queue_cmd._sync_terminal_running_entries(worker)

    assert upserts == []
