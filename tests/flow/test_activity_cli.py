from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.app_ids import (
    ORCA_AUTO_CONFIG_ENV_VAR,
    ORCA_AUTO_ORCA_SOURCE,
    ORCA_AUTO_REPO_ROOT_ENV_VAR,
)
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.flow import activity
from orca_auto.flow.activity import _cancel as _activity_cancel
from orca_auto.flow.activity import _list as _activity_list
from orca_auto.flow.activity import _model as _activity_model
from orca_auto.flow.activity import _orca as _activity_orca
from orca_auto.flow.activity import _sources as _activity_sources


def _shared_config(tmp_path: Path) -> tuple[Path, Path]:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    config_path = tmp_path / "orca_auto.yaml"
    config_path.write_text(f"runs_root: {runs_root}\n", encoding="utf-8")
    return config_path, runs_root


def test_list_activities_merges_workflows_and_standalone_sources(monkeypatch) -> None:
    workflow_record = SimpleNamespace(
        workflow_id="wf-2",
        template_name="reaction_ts_search",
        status="running",
        source_job_id="",
        source_job_type="",
        reaction_key="rxn-2",
        requested_at="2026-04-20T10:00:00+00:00",
        workspace_dir="/tmp/wf/wf-2",
        workflow_file="/tmp/wf/wf-2/workflow.json",
        stage_count=2,
        updated_at="2026-04-20T10:06:00+00:00",
        metadata={"last_restarted_at": "2026-04-20T10:05:00+00:00"},
    )

    monkeypatch.setattr(activity, "list_workflow_registry", lambda workflow_root: [workflow_record])
    monkeypatch.setattr(
        activity,
        "list_workflow_summaries",
        lambda workflow_root: [
            {
                "workflow_id": "wf-2",
                "stage_summaries": [
                    {
                        "stage_id": "crest.reactant",
                        "status": "completed",
                        "task_status": "completed",
                        "engine": "crest",
                        "reaction_dir": "",
                    },
                    {
                        "stage_id": "xtb.path",
                        "status": "running",
                        "task_status": "running",
                        "engine": "xtb",
                        "reaction_dir": "/tmp/xtb_jobs/rxn-2",
                    },
                ],
            }
        ],
    )
    monkeypatch.setattr(
        activity,
        "engine_runtime_paths",
        lambda config_path, *, engine: {
            "allowed_root": Path("/tmp/crest_root" if engine == "crest" else "/tmp/xtb_root"),
        },
    )

    crest_entry = QueueEntry(
        queue_id="crest-q-1",
        app_name="orca_auto_crest",
        task_id="crest-job-1",
        task_kind="crest_conformer_search",
        engine="crest",
        status=QueueStatus.PENDING,
        priority=10,
        enqueued_at="2026-04-20T10:01:00+00:00",
        metadata={"job_dir": "/tmp/crest_root/jobs/mol-a"},
    )
    xtb_entry = QueueEntry(
        queue_id="xtb-q-1",
        app_name="orca_auto_xtb",
        task_id="xtb-job-1",
        task_kind="xtb_path_search",
        engine="xtb",
        status=QueueStatus.RUNNING,
        priority=5,
        enqueued_at="2026-04-20T10:02:00+00:00",
        started_at="2026-04-20T10:03:00+00:00",
        metadata={
            "job_dir": "/tmp/xtb_root/jobs/rxn-a",
            "job_type": "path_search",
            "reaction_key": "rxn-a",
            "terminal_repair_blocked_reason": "terminal_artifacts_unrecoverable",
        },
    )
    monkeypatch.setattr(
        activity,
        "list_queue",
        # Shared or malformed roots can expose foreign rows. Each catalog
        # collector must retain only its complete canonical identity.
        lambda _root: [crest_entry, xtb_entry],
    )
    monkeypatch.setattr(
        _activity_orca,
        "orca_records",
        lambda **kwargs: [
            activity.ActivityRecord(
                activity_id="orca-q-1",
                kind="job",
                engine="orca",
                status="running",
                label="ts-run-1",
                source="orca_auto_orca",
                submitted_at="2026-04-20T10:04:00+00:00",
                updated_at="2026-04-20T10:07:00+00:00",
                cancel_target="orca-q-1",
                aliases=("orca-q-1",),
                metadata={},
            )
        ],
    )

    payload = activity.list_activities(
        workflow_root="/tmp/wf",
        crest_config="/tmp/crest.yaml",
        xtb_config="/tmp/xtb.yaml",
        orca_config="/tmp/orca.yaml",
    )

    assert payload["count"] == 4
    assert [item["activity_id"] for item in payload["activities"]] == [
        "orca-q-1",
        "wf-2",
        "xtb-q-1",
        "crest-q-1",
    ]
    workflow_item = next(item for item in payload["activities"] if item["activity_id"] == "wf-2")
    assert workflow_item["engine"] == "workflow"
    assert workflow_item["label"] == "/tmp/xtb_jobs/rxn-2"
    assert workflow_item["metadata"]["current_engine"] == "xtb"
    assert workflow_item["metadata"]["elapsed_started_at"] == "2026-04-20T10:05:00+00:00"
    xtb_item = next(item for item in payload["activities"] if item["activity_id"] == "xtb-q-1")
    assert xtb_item["status"] == "repair_blocked"
    assert xtb_item["metadata"]["repair_blocked_reason"] == "terminal_artifacts_unrecoverable"
    assert xtb_item["metadata"]["elapsed_started_at"] == "2026-04-20T10:03:00+00:00"


def test_list_activities_treats_submission_failed_stage_as_terminal_for_current_stage(
    monkeypatch,
) -> None:
    workflow_record = SimpleNamespace(
        workflow_id="wf-3",
        template_name="reaction_ts_search",
        status="running",
        source_job_id="",
        source_job_type="",
        reaction_key="rxn-3",
        requested_at="2026-04-20T10:00:00+00:00",
        workspace_dir="/tmp/wf/wf-3",
        workflow_file="/tmp/wf/wf-3/workflow.json",
        stage_count=2,
        updated_at="2026-04-20T10:06:00+00:00",
    )

    monkeypatch.setattr(activity, "list_workflow_registry", lambda workflow_root: [workflow_record])
    monkeypatch.setattr(
        activity,
        "list_workflow_summaries",
        lambda workflow_root: [
            {
                "workflow_id": "wf-3",
                "stage_summaries": [
                    {
                        "stage_id": "orca.submit",
                        "status": "submission_failed",
                        "task_status": "submission_failed",
                        "engine": "orca",
                        "reaction_dir": "/tmp/orca_jobs/rxn-3/failed-submit",
                    },
                    {
                        "stage_id": "xtb.path",
                        "status": "running",
                        "task_status": "running",
                        "engine": "xtb",
                        "reaction_dir": "/tmp/xtb_jobs/rxn-3",
                    },
                ],
            }
        ],
    )
    monkeypatch.setattr(
        _activity_sources,
        "resolve_activity_source_request",
        lambda request: _activity_model.ResolvedActivitySources("/tmp/wf", None, None, None),
    )
    monkeypatch.setattr(activity, "list_queue", lambda root: [])
    monkeypatch.setattr(_activity_orca, "orca_records", lambda **kwargs: [])

    payload = activity.list_activities(workflow_root="/tmp/wf")

    workflow_item = payload["activities"][0]
    assert workflow_item["activity_id"] == "wf-3"
    assert workflow_item["label"] == "/tmp/xtb_jobs/rxn-3"
    assert workflow_item["metadata"]["current_engine"] == "xtb"
    assert workflow_item["metadata"]["current_stage_id"] == "xtb.path"
    assert workflow_item["metadata"]["current_stage_status"] == "running"


def test_cancel_activity_routes_workflow_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        _activity_list,
        "collect_activity_records",
        lambda **kwargs: [
            activity.ActivityRecord(
                activity_id="wf-9",
                kind="workflow",
                engine="xtb",
                status="running",
                label="wf-9",
                source="orca_auto_flow",
                submitted_at="2026-04-20T10:00:00+00:00",
                updated_at="2026-04-20T10:00:00+00:00",
                cancel_target="wf-9",
                aliases=("wf-9", "/tmp/wf/wf-9"),
                metadata={},
            )
        ],
    )
    monkeypatch.setattr(
        _activity_cancel,
        "cancel_materialized_workflow",
        lambda **kwargs: {"workflow_id": "wf-9", "status": "cancelled", "cancelled": []},
    )

    payload = activity.cancel_activity(target="wf-9", workflow_root="/tmp/wf")

    assert payload["activity_id"] == "wf-9"
    assert payload["status"] == "cancelled"
    assert payload["source"] == "orca_auto_flow"


def test_cancel_activity_routes_xtb_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        _activity_list,
        "collect_activity_records",
        lambda **kwargs: [
            activity.ActivityRecord(
                activity_id="xtb-q-1",
                kind="job",
                engine="xtb",
                status="running",
                label="rxn-a",
                source="orca_auto_xtb",
                submitted_at="2026-04-20T10:00:00+00:00",
                updated_at="2026-04-20T10:01:00+00:00",
                cancel_target="xtb-q-1",
                aliases=("xtb-q-1", "xtb-job-1"),
                metadata={},
            )
        ],
    )
    monkeypatch.setattr(
        activity,
        "cancel_xtb_target",
        lambda **kwargs: {"status": "cancel_requested", "queue_id": kwargs["target"]},
    )

    payload = activity.cancel_activity(target="xtb-job-1", xtb_config="/tmp/xtb.yaml")

    assert payload["activity_id"] == "xtb-q-1"
    assert payload["status"] == "cancel_requested"
    assert payload["source"] == "orca_auto_xtb"


def test_activity_helper_edges_and_discovery_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _activity_sources.coerce_mapping({"a": 1}) == {"a": 1}
    assert _activity_sources.coerce_mapping(["not", "mapping"]) == {}

    existing = tmp_path / "config.yaml"
    existing.write_text("workflow:\n  root: /tmp/wf\n", encoding="utf-8")

    assert _activity_sources.discover_workflow_root(tmp_path / "wf") == str(
        (tmp_path / "wf").resolve()
    )
    monkeypatch.setenv(ORCA_AUTO_CONFIG_ENV_VAR, str(existing))
    assert _activity_sources.discover_shared_config(None) == str(existing.resolve())
    assert _activity_sources.discover_shared_config(str(existing)) == str(existing.resolve())
    resolved = _activity_sources.resolve_activity_source_request(
        _activity_model.ActivitySourceRequest(
            workflow_root=tmp_path / "wf",
            shared_config=str(existing),
        )
    )
    assert resolved.crest_config == str(existing.resolve())
    assert resolved.xtb_config == str(existing.resolve())
    assert resolved.orca_config == str(existing.resolve())

    monkeypatch.setenv(ORCA_AUTO_REPO_ROOT_ENV_VAR, str(tmp_path / "repo"))
    assert _activity_sources.discover_orca_repo_root(None) == str((tmp_path / "repo").resolve())
    assert _activity_sources.discover_orca_repo_root(str(tmp_path / "explicit")) == str(
        (tmp_path / "explicit").resolve()
    )

    assert (
        _activity_sources.shared_config_hint("", None, " /tmp/shared.yaml ") == "/tmp/shared.yaml"
    )
    assert _activity_model.parse_iso("") < _activity_model.parse_iso("2026-04-26T00:00:00Z")
    assert _activity_model.parse_iso("bad") < _activity_model.parse_iso("2026-04-26T00:00:00+09:00")
    assert _activity_model.parse_iso("2026-04-26T00:00:00").tzinfo is not None
    assert _activity_model.unique_texts([" a ", "", "a", "b"]) == ("a", "b")
    assert _activity_model.mapping_text({"key": " value "}, "key") == "value"
    assert _activity_model.path_aliases("", root=tmp_path) == ()


def test_runtime_path_and_engine_root_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"

    def fake_engine_runtime_paths(
        config_path: str, *, engine: str | None = None
    ) -> dict[str, Path]:
        del config_path
        return {"allowed_root": allowed}

    monkeypatch.setattr(activity, "engine_runtime_paths", fake_engine_runtime_paths)
    assert _activity_list.engine_queue_roots(
        "/tmp/cfg.yaml",
        engine="orca",
        deps=activity._activity_list_deps(),
    ) == (allowed,)
    monkeypatch.setattr(activity, "shared_workflow_root_from_config", lambda config_path: "")
    assert _activity_list.engine_queue_roots(
        "/tmp/cfg.yaml",
        engine="xtb",
        deps=activity._activity_list_deps(),
    ) == (allowed,)

    workspace_a = tmp_path / "wf" / "a"
    workspace_b = tmp_path / "wf" / "b"
    runtime_a = tmp_path / "runtime-a"
    runtime_b = tmp_path / "runtime-b"
    runtime_a.mkdir()
    runtime_b.mkdir()
    monkeypatch.setattr(
        activity, "shared_workflow_root_from_config", lambda config_path: str(tmp_path / "wf")
    )
    monkeypatch.setattr(
        activity,
        "iter_workflow_runtime_workspaces",
        lambda workflow_root, *, engine: [workspace_a, workspace_b, workspace_a],
    )
    monkeypatch.setattr(
        activity,
        "workflow_workspace_internal_engine_paths",
        lambda workspace_dir, *, engine, stage_dirname=None: {
            "allowed_root": runtime_a if workspace_dir == workspace_a else runtime_b,
        },
    )
    assert _activity_list.engine_queue_roots(
        "/tmp/cfg.yaml",
        engine="crest",
        deps=activity._activity_list_deps(),
    ) == (runtime_a, runtime_b)


def test_orca_records_merge_queue_entries_and_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "rxn-1"
    reaction_dir.mkdir()
    orphan_dir = allowed / "orphan"
    orphan_dir.mkdir()
    entries = [
        QueueEntry(
            queue_id="q-1",
            app_name="orca_auto_orca",
            task_id="task-1",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.RUNNING,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            metadata={
                "run_id": "run-1",
                "reaction_dir": str(reaction_dir),
                "workflow_id": "wf-1",
                "job_type": "opt",
                "selected_inp": "rxn.inp",
            },
        ),
        QueueEntry(
            queue_id="q-2",
            app_name="orca_auto_orca",
            task_id="task-2",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.RUNNING,
            priority=4,
            enqueued_at="2026-04-26T00:02:00+00:00",
            cancel_requested=True,
            metadata={"reaction_dir": str(tmp_path / "missing")},
        ),
        QueueEntry(
            queue_id="xtb-foreign",
            app_name="orca_auto_xtb",
            task_id="xtb-task",
            task_kind="xtb_sp",
            engine="xtb",
            status=QueueStatus.PENDING,
            priority=1,
            enqueued_at="2026-04-26T00:03:00+00:00",
            metadata={"job_type": "sp", "job_dir": str(tmp_path / "xtb")},
        ),
    ]
    snapshots = [
        SimpleNamespace(
            key="snap-1",
            run_id="run-1",
            reaction_dir=reaction_dir,
            status="completed",
            name="tracked-name",
            completed_at="2026-04-26T01:00:00+00:00",
            updated_at="",
            started_at="2026-04-26T00:00:00+00:00",
            attempts=2,
            selected_inp_name="rxn.inp",
            job_type="opt",
        ),
        SimpleNamespace(
            key="snap-2",
            run_id="run-2",
            reaction_dir=orphan_dir,
            status="failed",
            name="",
            completed_at="",
            updated_at="2026-04-26T02:00:00+00:00",
            started_at="2026-04-26T01:30:00+00:00",
            attempts=1,
            selected_inp_name="orphan.inp",
            job_type="sp",
        ),
    ]
    reconciled: list[Path] = []

    from orca_auto.orca import run_snapshot
    from orca_auto.orca.queue import adapter as queue_adapter

    monkeypatch.setattr(
        activity, "engine_runtime_paths", lambda config_path, *, engine: {"allowed_root": allowed}
    )
    monkeypatch.setattr(
        queue_adapter, "reconcile_orphaned_running_entries", lambda root: reconciled.append(root)
    )
    monkeypatch.setattr(queue_adapter, "list_queue", lambda root: entries)
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root: snapshots)

    rows = _activity_orca.orca_records(
        config_path="/tmp/cfg.yaml",
        deps=activity._orca_activity_deps(),
    )

    assert reconciled == [allowed]
    by_id = {row.activity_id: row for row in rows}
    assert "xtb-foreign" not in by_id
    assert by_id["q-1"].status == "completed"
    assert by_id["q-1"].label == "tracked-name"
    assert by_id["q-1"].metadata["elapsed_started_at"] == "2026-04-26T00:01:00+00:00"
    assert by_id["q-2"].status == "cancel_requested"
    assert by_id["q-2"].metadata["elapsed_started_at"] == "2026-04-26T00:02:00+00:00"
    assert by_id["run-2"].status == "failed"
    assert by_id["run-2"].metadata["elapsed_started_at"] == "2026-04-26T01:30:00+00:00"
    assert by_id["run-2"].metadata["selected_inp_name"] == "orphan.inp"


def test_orca_records_suppress_stale_snapshot_for_terminal_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cancelled queue entry whose run state still reads "running" must not keep
    # showing the job as in progress: the stale snapshot is superseded by the
    # terminal queue outcome and should not be listed as a separate active row.
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "ts3"
    reaction_dir.mkdir()
    entries = [
        QueueEntry(
            queue_id="q-cancel",
            app_name="orca_auto_orca",
            task_id="task-ts3",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.CANCELLED,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            finished_at="2026-04-26T00:05:00+00:00",
            metadata={"reaction_dir": str(reaction_dir)},
        ),
    ]
    snapshots = [
        SimpleNamespace(
            key="snap-stale",
            run_id="",
            reaction_dir=reaction_dir,
            status="running",
            name="ts3",
            completed_at="",
            updated_at="2026-04-26T00:04:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            attempts=1,
            selected_inp_name="ts3.inp",
            job_type="optts",
        ),
    ]

    from orca_auto.orca import run_snapshot
    from orca_auto.orca.queue import adapter as queue_adapter

    monkeypatch.setattr(
        activity, "engine_runtime_paths", lambda config_path, *, engine: {"allowed_root": allowed}
    )
    monkeypatch.setattr(queue_adapter, "reconcile_orphaned_running_entries", lambda root: None)
    monkeypatch.setattr(queue_adapter, "list_queue", lambda root: entries)
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root: snapshots)

    rows = _activity_orca.orca_records(
        config_path="/tmp/cfg.yaml",
        deps=activity._orca_activity_deps(),
    )

    # Only the cancelled queue record remains; the stale running snapshot is gone.
    assert {row.activity_id: row.status for row in rows} == {"q-cancel": "cancelled"}


def test_orca_records_keep_live_snapshot_despite_terminal_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A genuinely live re-run that shares a reaction dir with an older terminal
    # queue entry must NOT be suppressed: the live run lock means it is in progress,
    # so the snapshot row is kept alongside the terminal queue row.
    allowed = tmp_path / "orca"
    allowed.mkdir()
    reaction_dir = allowed / "ts4"
    reaction_dir.mkdir()
    entries = [
        QueueEntry(
            queue_id="q-done",
            app_name="orca_auto_orca",
            task_id="task-ts4",
            task_kind="orca_run_inp",
            engine="orca",
            status=QueueStatus.COMPLETED,
            priority=3,
            enqueued_at="2026-04-26T00:00:00+00:00",
            started_at="2026-04-26T00:01:00+00:00",
            finished_at="2026-04-26T00:05:00+00:00",
            metadata={"reaction_dir": str(reaction_dir)},
        ),
    ]
    snapshots = [
        SimpleNamespace(
            key="snap-live",
            run_id="",
            reaction_dir=reaction_dir,
            status="running",
            name="ts4",
            completed_at="",
            updated_at="2026-04-26T00:10:00+00:00",
            started_at="2026-04-26T00:09:00+00:00",
            attempts=1,
            selected_inp_name="ts4.inp",
            job_type="optts",
        ),
    ]

    from orca_auto.orca import run_snapshot
    from orca_auto.orca.queue import adapter as queue_adapter

    monkeypatch.setattr(
        activity, "engine_runtime_paths", lambda config_path, *, engine: {"allowed_root": allowed}
    )
    monkeypatch.setattr(queue_adapter, "reconcile_orphaned_running_entries", lambda root: None)
    monkeypatch.setattr(queue_adapter, "list_queue", lambda root: entries)
    monkeypatch.setattr(run_snapshot, "collect_run_snapshots", lambda root: snapshots)
    # A live run lock holds the dir -> the snapshot is a genuine in-progress re-run.
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: True)

    rows = _activity_orca.orca_records(
        config_path="/tmp/cfg.yaml",
        deps=activity._orca_activity_deps(),
    )

    # Both the terminal queue row and the live running snapshot row are present.
    assert {row.activity_id: row.status for row in rows} == {
        "q-done": "completed",
        "ts4": "running",
    }


def test_snapshot_display_status_marks_dead_running_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orca_auto.orca.run_snapshot import RunSnapshot

    def _snap(status: str) -> RunSnapshot:
        return RunSnapshot(
            key="k",
            name="rxn",
            reaction_dir=Path("/tmp/rxn"),
            run_id="r",
            status=status,
            started_at="",
            updated_at="",
            completed_at="",
            selected_inp_name="",
            attempts=0,
            latest_out_path=None,
            final_reason="",
            elapsed=0.0,
            elapsed_text="",
        )

    running = _snap("running")

    # No live run lock -> the run is gone; show it as failed, not in progress.
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: False)
    assert _activity_orca._snapshot_display_status(running) == "failed"

    # A live run lock -> genuinely running, leave it as running.
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: True)
    assert _activity_orca._snapshot_display_status(running) == "running"

    # Terminal statuses are never reinterpreted, regardless of the lock.
    done = _snap("completed")
    monkeypatch.setattr(_activity_orca, "run_lock_is_held", lambda *a, **k: False)
    assert _activity_orca._snapshot_display_status(done) == "completed"


def test_match_activity_record_and_cancel_error_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        activity.ActivityRecord(
            "a", "job", "xtb", "running", "A", "orca_auto_xtb", "", "", "target", aliases=("same",)
        ),
        activity.ActivityRecord(
            "b", "job", "xtb", "running", "B", "orca_auto_xtb", "", "", "target", aliases=("same",)
        ),
    ]
    with pytest.raises(ValueError, match="empty"):
        _activity_cancel.match_activity_record(records, "")
    with pytest.raises(ValueError, match="Ambiguous activity target"):
        _activity_cancel.match_activity_record(records, "target")
    with pytest.raises(ValueError, match="Ambiguous activity target"):
        _activity_cancel.match_activity_record(records, "same")
    with pytest.raises(LookupError, match="not found"):
        _activity_cancel.match_activity_record(records, "missing")

    def collect_one(record: activity.ActivityRecord) -> None:
        monkeypatch.setattr(_activity_list, "collect_activity_records", lambda **kwargs: [record])

    monkeypatch.setattr(
        _activity_sources,
        "resolve_activity_source_request",
        lambda request: _activity_model.ResolvedActivitySources(None, None, None, None),
    )

    collect_one(
        activity.ActivityRecord(
            "crest", "job", "crest", "running", "C", "orca_auto_crest", "", "", "crest-q"
        )
    )
    with pytest.raises(ValueError, match="crest_config"):
        activity.cancel_activity(target="crest-q")

    collect_one(
        activity.ActivityRecord(
            "xtb", "job", "xtb", "running", "X", "orca_auto_xtb", "", "", "xtb-q"
        )
    )
    with pytest.raises(ValueError, match="xtb_config"):
        activity.cancel_activity(target="xtb-q")

    collect_one(
        activity.ActivityRecord(
            "orca", "job", "orca", "running", "O", ORCA_AUTO_ORCA_SOURCE, "", "", "orca-q"
        )
    )
    with pytest.raises(ValueError, match="orca_auto_config"):
        activity.cancel_activity(target="orca-q")

    collect_one(
        activity.ActivityRecord("bad", "job", "x", "running", "B", "unknown", "", "", "bad-q")
    )
    with pytest.raises(ValueError, match="Unsupported activity source"):
        activity.cancel_activity(target="bad-q")


def test_cancel_activity_routes_crest_and_orca_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    records = {
        "crest-q": activity.ActivityRecord(
            "crest-q", "job", "crest", "running", "C", "orca_auto_crest", "", "", "crest-q"
        ),
        "orca-q": activity.ActivityRecord(
            "orca-q", "job", "orca", "running", "O", ORCA_AUTO_ORCA_SOURCE, "", "", "orca-q"
        ),
    }
    monkeypatch.setattr(
        _activity_list, "collect_activity_records", lambda **kwargs: list(records.values())
    )
    monkeypatch.setattr(
        activity, "cancel_crest_target", lambda **kwargs: {"status": "cancel_requested", **kwargs}
    )
    monkeypatch.setattr(activity, "cancel_orca_target", lambda **kwargs: {"status": "", **kwargs})
    monkeypatch.setattr(
        _activity_sources, "discover_orca_repo_root", lambda explicit: "/tmp/orca-repo"
    )

    crest_payload = activity.cancel_activity(target="crest-q", crest_config="/tmp/crest.yaml")
    orca_payload = activity.cancel_activity(target="orca-q", orca_config="/tmp/orca.yaml")

    assert crest_payload["status"] == "cancel_requested"
    assert crest_payload["result"]["config_path"] == str(Path("/tmp/crest.yaml").resolve())
    assert orca_payload["status"] == "failed"
    assert orca_payload["result"]["repo_root"] == "/tmp/orca-repo"


def test_clear_activities_clears_workflow_and_engine_terminal_sources(monkeypatch) -> None:
    import orca_auto.orca.run_cleanup as orca_run_cleanup

    captured_statuses: tuple[str, ...] = ()

    def fake_clear_terminal_workflow_registry(workflow_root, statuses=None):
        nonlocal captured_statuses
        captured_statuses = tuple(sorted(statuses or ()))
        return 2

    monkeypatch.setattr(
        activity, "clear_terminal_workflow_registry", fake_clear_terminal_workflow_registry
    )
    monkeypatch.setattr(
        _activity_list,
        "engine_queue_roots",
        lambda config_path, *, engine, **kwargs: (
            (Path("/tmp/xtb_root_a"), Path("/tmp/xtb_root_b"))
            if engine == "xtb"
            else (Path("/tmp/crest_root"),)
        ),
    )

    cleared_roots: list[str] = []

    def fake_clear_queue_terminal(root: Path, **kwargs: Any) -> int:
        assert callable(kwargs.get("select_entry_fn"))
        cleared_roots.append(str(root))
        if "xtb_root_a" in str(root):
            return 1
        if "xtb_root_b" in str(root):
            return 2
        return 3

    monkeypatch.setattr(activity, "clear_queue_terminal", fake_clear_queue_terminal)
    monkeypatch.setattr(
        activity,
        "engine_runtime_paths",
        lambda config_path, *, engine="orca": {"allowed_root": Path("/tmp/orca_root")},
    )
    monkeypatch.setattr(orca_run_cleanup, "clear_terminal_entries", lambda allowed_root: (4, 5))

    payload = activity.clear_activities(
        workflow_root="/tmp/workflows",
        crest_config="/tmp/orca_auto.yaml",
        xtb_config="/tmp/orca_auto.yaml",
        orca_config="/tmp/orca_auto.yaml",
    )

    assert payload["total_cleared"] == 17
    assert payload["cleared"] == {
        "workflows": 2,
        "xtb_queue_entries": 3,
        "crest_queue_entries": 3,
        "orca_queue_entries": 4,
        "orca_run_states": 5,
    }
    assert "submission_failed" in captured_statuses
    assert cleared_roots == [
        "/tmp/xtb_root_a",
        "/tmp/xtb_root_b",
        "/tmp/crest_root",
    ]


def test_clear_activities_keeps_cleared_terminal_workflows_hidden_from_listing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        _activity_sources,
        "resolve_activity_source_request",
        lambda request: _activity_model.ResolvedActivitySources(str(tmp_path), None, None, None),
    )

    workspace = tmp_path / "wf_completed"
    workspace.mkdir(parents=True)
    (workspace / "workflow.json").write_text(
        json.dumps(
            {
                "workflow_id": "wf_completed",
                "template_name": "reaction_ts_search",
                "status": "completed",
                "source_job_id": "job-1",
                "source_job_type": "reaction_ts_search",
                "reaction_key": "rxn-1",
                "requested_at": "2026-04-20T10:00:00+00:00",
                "stages": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )

    initial = activity.list_activities(workflow_root=tmp_path)
    assert [item["activity_id"] for item in initial["activities"]] == ["wf_completed"]

    cleared = activity.clear_activities(workflow_root=tmp_path)
    assert cleared["cleared"]["workflows"] == 1

    after_clear = activity.list_activities(workflow_root=tmp_path)
    assert after_clear["activities"] == []


def test_list_activities_autodiscovers_defaults_when_no_args(monkeypatch) -> None:
    monkeypatch.setattr(
        _activity_sources, "discover_workflow_root", lambda workflow_root: "/tmp/workflow_root"
    )
    monkeypatch.setattr(
        _activity_sources,
        "discover_shared_config",
        lambda explicit: "/tmp/orca_auto.yaml",
    )
    captured: dict[str, Any] = {}

    def fake_collect(**kwargs: Any) -> list[activity.ActivityRecord]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(_activity_list, "collect_activity_records", fake_collect)

    payload = activity.list_activities()

    assert payload["count"] == 0
    assert payload["sources"] == {
        "workflow_root": str(Path("/tmp/workflow_root").resolve()),
        "crest_config": "/tmp/orca_auto.yaml",
        "xtb_config": "/tmp/orca_auto.yaml",
        "orca_config": "/tmp/orca_auto.yaml",
    }
    assert captured["workflow_root"] == "/tmp/workflow_root"
    assert captured["crest_config"] == "/tmp/orca_auto.yaml"
    assert captured["xtb_config"] == "/tmp/orca_auto.yaml"
    assert captured["orca_config"] == "/tmp/orca_auto.yaml"


def test_workspace_display_name_prefers_scaffold_for_generation_workspaces() -> None:
    from orca_auto.flow.activity._workflow_records import _workspace_display_name

    root = Path("/tmp/orca_runs")
    assert (
        _workspace_display_name(
            "/tmp/orca_runs/TS8_wf/20260717-153816-966c453a", workflow_root=root
        )
        == "TS8_wf"
    )
    # Direct-API workspaces sit right under the workflow root: no scaffold to show.
    assert (
        _workspace_display_name("/tmp/orca_runs/20260717-153816-966c453a", workflow_root=root)
        == "20260717-153816-966c453a"
    )
    # Non-generation workspace names display as themselves.
    assert (
        _workspace_display_name("/tmp/orca_runs/wf_reaction_ts8", workflow_root=root)
        == "wf_reaction_ts8"
    )
    assert _workspace_display_name("", workflow_root=root) == ""


def test_cancel_activity_autodiscovers_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        _activity_sources, "discover_workflow_root", lambda workflow_root: "/tmp/workflow_root"
    )
    monkeypatch.setattr(
        _activity_sources,
        "discover_shared_config",
        lambda explicit: "/tmp/orca_auto.yaml",
    )
    monkeypatch.setattr(
        _activity_list,
        "collect_activity_records",
        lambda **kwargs: [
            activity.ActivityRecord(
                activity_id="wf-77",
                kind="workflow",
                engine="workflow",
                status="running",
                label="wf-77",
                source="orca_auto_flow",
                submitted_at="2026-04-20T10:00:00+00:00",
                updated_at="2026-04-20T10:00:00+00:00",
                cancel_target="wf-77",
                aliases=("wf-77",),
                metadata={},
            )
        ],
    )
    captured: dict[str, Any] = {}

    def fake_cancel_workflow(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"workflow_id": "wf-77", "status": "cancelled"}

    monkeypatch.setattr(_activity_cancel, "cancel_materialized_workflow", fake_cancel_workflow)

    payload = activity.cancel_activity(target="wf-77")

    assert payload["status"] == "cancelled"
    assert captured["workflow_root"] == "/tmp/workflow_root"
    assert captured["crest_config"] == "/tmp/orca_auto.yaml"
    assert captured["xtb_config"] == "/tmp/orca_auto.yaml"
    assert captured["orca_config"] == "/tmp/orca_auto.yaml"
