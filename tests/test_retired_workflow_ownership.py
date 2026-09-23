from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

from orca_auto.cli import main as cli_main
from orca_auto.core.queue import store
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca import worker_execution
from orca_auto.orca.config import AppConfig, OrcaRuntimeConfig, PathsConfig
from orca_auto.orca.queue import adapter
from orca_auto.orca.submission import create_queued_submission


def _fixture(tmp_path: Path, *, ownership: str = "marker") -> tuple[AppConfig, Path, QueueEntry]:
    job = tmp_path / "retired" / "generation" / "job"
    job.mkdir(parents=True)
    (job / "h2.inp").write_text(
        "! HF STO-3G SP\n* xyz 0 1\nH 0 0 0\nH 0 0 .74\n*\n", encoding="utf-8"
    )
    if ownership == "marker":
        (job.parent / "workflow.json").write_text("preserve legacy owner", encoding="utf-8")
    cfg = AppConfig(
        runtime=OrcaRuntimeConfig(allowed_root=str(tmp_path)),
        paths=PathsConfig(orca_executable="/usr/bin/false"),
    )
    entry = QueueEntry(
        queue_id="retired-row",
        task_id="retired-task",
        app_name="orca_auto_orca",
        engine="orca",
        task_kind="orca_run_inp",
        metadata={
            "reaction_dir": str(job),
            "_orca_auto_queued_record_sync": "complete",
            **({"workflow_id": "retired-owner"} if ownership == "metadata" else {}),
        },
        status=QueueStatus.RUNNING,
        started_at="2026-09-23T00:00:00Z",
    )
    return cfg, job, entry


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".lock" and ".activity" not in p.name
    }


@pytest.mark.parametrize("ownership", ["marker", "metadata"])
@pytest.mark.parametrize("force", [False, True])
def test_retired_submission_does_not_create_snapshot_or_queue(
    tmp_path: Path, ownership: str, force: bool
) -> None:
    cfg, job, entry = _fixture(tmp_path, ownership=ownership)
    if ownership == "metadata":
        store.save_entries(tmp_path, [replace(entry, status=QueueStatus.COMPLETED)])
    before = _files(tmp_path)
    with pytest.raises(ValueError, match="retired"):
        create_queued_submission(
            cfg, Namespace(force=force, priority=10), job, selected_inp=job / "h2.inp"
        )
    assert _files(tmp_path) == before


@pytest.mark.parametrize("ownership", ["marker", "metadata"])
@pytest.mark.parametrize("operation", ["prepare", "rebind"])
def test_retired_worker_cannot_prepare_or_rebind_generation(
    tmp_path: Path, operation: str, ownership: str
) -> None:
    cfg, _job, entry = _fixture(tmp_path, ownership=ownership)
    store.save_entries(tmp_path, [entry])
    before = _files(tmp_path)
    with pytest.raises(ValueError, match="retired"):
        if operation == "prepare":
            worker_execution._build_execution_context(cfg, entry, admission_token=None)
        else:
            worker_execution._maybe_rebind_recovery_generation(
                entry, queue_root=tmp_path, cfg_factory=lambda: cfg
            )
    assert _files(tmp_path) == before


@pytest.mark.parametrize("ownership", ["marker", "metadata"])
@pytest.mark.parametrize(
    "status", [QueueStatus.PENDING, QueueStatus.RUNNING, QueueStatus.COMPLETED]
)
def test_retired_cancel_and_clear_preserve_queue_and_artifacts(
    tmp_path: Path, status: QueueStatus, ownership: str
) -> None:
    _cfg, _job, entry = _fixture(tmp_path, ownership=ownership)
    entry = replace(entry, status=status)
    store.save_entries(tmp_path, [entry])
    before = _files(tmp_path)
    assert adapter.cancel(tmp_path, entry.queue_id, expected_entry=entry) is None
    assert adapter.clear_terminal(tmp_path) == 0
    assert _files(tmp_path) == before


@pytest.mark.parametrize("ownership", ["marker", "metadata"])
def test_retired_pending_row_does_not_block_next_standalone_job(
    tmp_path: Path, ownership: str
) -> None:
    _cfg, _job, entry = _fixture(tmp_path, ownership=ownership)
    retired = replace(entry, status=QueueStatus.PENDING, started_at="")
    ordinary_dir = tmp_path / "ordinary"
    ordinary_dir.mkdir()
    ordinary = replace(
        retired,
        queue_id="ordinary-row",
        task_id="ordinary-task",
        metadata={"reaction_dir": str(ordinary_dir), "_orca_auto_queued_record_sync": "complete"},
    )
    store.save_entries(tmp_path, [retired, ordinary])
    selected = adapter.dequeue_next(tmp_path)
    assert selected is not None and selected.queue_id == ordinary.queue_id
    remaining = {row.queue_id: row for row in store.list_queue(tmp_path)}
    assert remaining[retired.queue_id] == retired
    assert remaining[ordinary.queue_id].status == QueueStatus.RUNNING


@pytest.mark.parametrize("ownership", ["marker", "metadata"])
def test_retired_terminal_state_is_not_removed_by_run_cleanup(
    tmp_path: Path, ownership: str
) -> None:
    from orca_auto.orca.run_cleanup import clear_terminal_entries
    from orca_auto.orca.state import save_state
    from orca_auto.orca.state_reading import state_path

    _cfg, job, entry = _fixture(tmp_path, ownership=ownership)
    retired = replace(entry, status=QueueStatus.COMPLETED)
    store.save_entries(tmp_path, [retired])
    save_state(
        job,
        {
            "run_id": "retired-run",
            "reaction_dir": str(job),
            "selected_inp": str(job / "h2.inp"),
            "status": "completed",
            "started_at": "2026-09-23T00:00:00Z",
            "updated_at": "2026-09-23T00:01:00Z",
            "attempts": [{"index": 1}],
            "final_result": {"status": "completed"},
        },
    )
    original_state = state_path(job).read_bytes()
    original_queue = (tmp_path / "queue.json").read_bytes()
    assert clear_terminal_entries(tmp_path) == (0, 0)
    assert state_path(job).read_bytes() == original_state
    assert (tmp_path / "queue.json").read_bytes() == original_queue


@pytest.mark.parametrize("force", [False, True])
def test_public_run_dir_preserves_metadata_owned_job_and_allows_standalone(
    tmp_path: Path,
    force: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orca_auto.orca import submission

    _cfg, job, entry = _fixture(tmp_path, ownership="metadata")
    retired = replace(entry, status=QueueStatus.COMPLETED)
    store.save_entries(tmp_path, [retired])
    config = tmp_path / "orca_auto.yaml"
    config.write_text(
        json.dumps(
            {"runs_root": str(tmp_path), "orca": {"paths": {"orca_executable": "/usr/bin/false"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(submission, "notify_queued_submission", lambda _cfg, _result: True)
    before = _files(tmp_path)
    original_job_dirs = {path.relative_to(job) for path in job.rglob("*") if path.is_dir()}
    argv = ["run-dir", str(job), "--config", str(config), "--json"]
    if force:
        argv.append("--force")

    assert cli_main(argv) == 1
    assert _files(tmp_path) == before
    assert {path.relative_to(job) for path in job.rglob("*") if path.is_dir()} == original_job_dirs
    capsys.readouterr()

    ordinary = tmp_path / "standalone"
    ordinary.mkdir()
    (ordinary / "h2.inp").write_bytes((job / "h2.inp").read_bytes())
    assert cli_main(["run-dir", str(ordinary), "--config", str(config), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "queued"
    rows = store.list_queue(tmp_path)
    assert len(rows) == 2
    assert next(row for row in rows if row.queue_id == retired.queue_id) == retired
    current = next(row for row in rows if row.queue_id != retired.queue_id)
    assert current.status == QueueStatus.PENDING
    assert current.metadata["reaction_dir"] == str(ordinary)
    for relative_path, data in before.items():
        if relative_path.startswith(str(job.relative_to(tmp_path)) + "/"):
            assert (tmp_path / relative_path).read_bytes() == data
