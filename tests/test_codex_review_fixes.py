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


@pytest.mark.parametrize("event_type", ["worker_cycle_started", "worker_cycle_finished"])
def test_worker_cycle_events_keep_their_session(tmp_path: Path, event_type: str) -> None:
    event = {
        "event_type": event_type,
        "worker_session_id": "wf_worker_20260717_074850_deadbeef",
        "metadata": {"cycle_started_at": "2026-07-17T10:00:00+00:00"},
    }

    rendered = render_telegram(journal_event_message(event, tmp_path))

    assert "wf_worker_20260717_074850_deadbeef" in rendered
