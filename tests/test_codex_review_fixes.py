"""Regressions for the accepted post-merge review findings on PRs 112-120."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca_auto.core.messaging import render_telegram
from orca_auto.core.paths.workflow import (
    WORKFLOW_FILE_NAME,
    WORKFLOW_SCAFFOLD_MANIFEST_NAME,
    workflow_workspace_internal_engine_paths_from_path,
)
from orca_auto.core.queue.generation import visible_generation_children
from orca_auto.flow.registry._notifications import journal_event_message


def test_same_second_generations_order_by_recency(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    # Same second-resolution timestamp; the hex suffixes reverse-sort against
    # the actual creation order.
    older = job_dir / "20260717-120000-ffffffff"
    newer = job_dir / "20260717-120000-00000000"
    older.mkdir()
    newer.mkdir()
    base_ns = 1_700_000_000_000_000_000
    os.utime(older, ns=(base_ns, base_ns))
    os.utime(newer, ns=(base_ns + 2_000_000_000, base_ns + 2_000_000_000))

    children = visible_generation_children(job_dir)

    assert [entry.name for entry in children] == [newer.name, older.name]


def test_distinct_second_generations_still_order_by_name(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    older = job_dir / "20260717-120000-aaaaaaaa"
    newer = job_dir / "20260717-120001-aaaaaaaa"
    older.mkdir()
    newer.mkdir()
    # Even with a misleading mtime, a later timestamp prefix wins.
    base_ns = 1_700_000_000_000_000_000
    os.utime(older, ns=(base_ns + 9_000_000_000, base_ns + 9_000_000_000))
    os.utime(newer, ns=(base_ns, base_ns))

    children = visible_generation_children(job_dir)

    assert [entry.name for entry in children] == [newer.name, older.name]


def _workflow_paths(root: Path, target: Path):
    return workflow_workspace_internal_engine_paths_from_path(
        target,
        workflow_root=root,
        engine="orca",
    )


def test_generation_shape_alone_does_not_grant_workflow_paths(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    job = root / "plain_orca_job"
    generation = job / "20260717-120000-aabbccdd"
    stage = generation / "03_orca"
    stage.mkdir(parents=True)

    assert _workflow_paths(root, stage) is None


def test_scaffold_parent_grants_workflow_paths(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    scaffold = root / "reaction_scaffold"
    generation = scaffold / "20260717-120000-aabbccdd"
    stage = generation / "03_orca"
    stage.mkdir(parents=True)
    (scaffold / WORKFLOW_SCAFFOLD_MANIFEST_NAME).write_text("template: x\n", encoding="utf-8")

    assert _workflow_paths(root, stage) is not None


def test_committed_manifest_grants_workflow_paths_without_scaffold(tmp_path: Path) -> None:
    # The scaffold's mutable flow.yaml was removed after submission; the
    # committed workspace manifest still authorizes the workspace.
    root = tmp_path / "runs"
    scaffold = root / "reaction_scaffold"
    generation = scaffold / "20260717-120000-aabbccdd"
    stage = generation / "03_orca"
    stage.mkdir(parents=True)
    (generation / WORKFLOW_FILE_NAME).write_text("{}", encoding="utf-8")

    assert _workflow_paths(root, stage) is not None


def test_xtb_md_artifacts_revalidate_before_each_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import orca_auto.xtb_md.records as records

    job_dir = tmp_path / "xtb_job"
    job_dir.mkdir()
    validations: list[int] = []
    writes: list[str] = []

    def fake_validate(_root: object, _snapshot: object) -> Path:
        validations.append(len(writes))
        return job_dir

    monkeypatch.setattr(records, "validate_execution_snapshot_job_dir", fake_validate)
    monkeypatch.setattr(records, "job_dir_from_entry", lambda _entry: job_dir)
    monkeypatch.setattr(records, "write_state", lambda _d, _p: writes.append("state"))
    monkeypatch.setattr(records, "write_report_json", lambda _d, _p: writes.append("json"))
    monkeypatch.setattr(records, "write_report_md_lines", lambda _d, _l: writes.append("md"))
    monkeypatch.setattr(
        "orca_auto.core.engines.artifacts.build_engine_report_markdown",
        lambda _payload: ["# report"],
    )
    monkeypatch.setattr(
        records.job_locations,
        "upsert_job_record",
        lambda _cfg, **_kwargs: None,
    )
    entry = SimpleNamespace(
        task_id="task-xtb",
        metadata={
            "execution_snapshot": {"job_dir": str(job_dir)},
            "resource_request": {"max_cores": 1, "max_memory_gb": 1},
            "ensemble": "nvt",
            "selected_input_xyz": str(job_dir / "input.xyz"),
            "molecule_key": "h2",
        },
    )
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(tmp_path)))

    records.persist_job_artifact(cfg, entry, {"status": {"state": "completed"}})

    assert writes == ["state", "json", "md"]
    # One validation per artifact write (plus the trailing index refresh):
    # a directory swapped in after any single write is caught before the next.
    assert len(validations) >= len(writes) + 1
    assert validations[:3] == [0, 1, 2]


@pytest.mark.parametrize("event_type", ["worker_cycle_started", "worker_cycle_finished"])
def test_worker_cycle_events_keep_their_session(tmp_path: Path, event_type: str) -> None:
    event = {
        "event_type": event_type,
        "worker_session_id": "wf_worker_20260717_074850_deadbeef",
        "metadata": {"cycle_started_at": "2026-07-17T10:00:00+00:00"},
    }

    rendered = render_telegram(journal_event_message(event, tmp_path))

    assert "wf_worker_20260717_074850_deadbeef" in rendered
