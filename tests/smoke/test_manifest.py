from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from orca_auto.smoke import manifest as smoke_manifest
from orca_auto.smoke.catalog import SmokeScenario
from orca_auto.smoke.manifest import (
    build_case_manifest,
    create_pinned_batch_directory,
    prepare_smoke_root,
    source_identity,
)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_source_identity_tracks_untracked_names_but_not_content(tmp_path: Path) -> None:
    # Untracked file NAMES reach the identity through the status digest; their
    # content is deliberately outside the provenance scope, so smoke I/O never
    # scales with untracked workstation files.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Smoke Test")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    (repo / "pyproject.toml").write_text("[project]\nname='tiny'\nversion='1'\n", encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    untracked = repo / "new_test.py"
    untracked.write_text("VALUE = 'first'\n", encoding="utf-8")

    first = source_identity(repo)
    untracked.write_text("VALUE = 'second'\n", encoding="utf-8")
    second = source_identity(repo)
    (repo / "another_untracked.py").write_text("x\n", encoding="utf-8")
    third = source_identity(repo)

    assert "untracked_digest" not in first
    assert first["dirty"] is True
    assert first == second
    assert third["status_digest"] != first["status_digest"]


def test_prepare_smoke_root_refuses_unowned_or_symlink_directory(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    unowned = runs_root / ".orca_auto_smoke"
    unowned.mkdir(parents=True)
    with pytest.raises(ValueError, match="unowned"):
        prepare_smoke_root(runs_root)

    unowned.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    unowned.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_smoke_root(runs_root)


def test_prepare_smoke_root_serializes_owner_marker_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    first_marker_started = threading.Event()
    release_first_marker = threading.Event()
    original_write = smoke_manifest._atomic_write_json_at

    def pause_first_owner_write(
        directory_fd: int,
        name: str,
        payload: dict[str, object],
        *,
        mode: int = 0o600,
    ) -> None:
        if name == smoke_manifest.SMOKE_OWNER_FILENAME and not first_marker_started.is_set():
            first_marker_started.set()
            assert release_first_marker.wait(timeout=10)
        original_write(directory_fd, name, payload, mode=mode)

    monkeypatch.setattr(smoke_manifest, "_atomic_write_json_at", pause_first_owner_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(prepare_smoke_root, runs_root)
        assert first_marker_started.wait(timeout=10)
        second = executor.submit(prepare_smoke_root, runs_root)
        release_first_marker.set()
        first_root = first.result(timeout=10)
        second_root = second.result(timeout=10)

    assert first_root == second_root == runs_root / ".orca_auto_smoke"
    assert not (runs_root / smoke_manifest._SMOKE_INIT_LOCK_FILENAME).exists()


def test_rebuild_smoke_index_uses_pinned_tmpfs_lock_without_disk_leaf(tmp_path: Path) -> None:
    smoke_root = prepare_smoke_root(tmp_path / "runs")
    batches_root = smoke_root / "batches"
    batches_root.mkdir()
    smoke_root_fd = os.open(smoke_root, smoke_manifest.directory_open_flags())
    batches_fd = os.open(batches_root, smoke_manifest.directory_open_flags())
    try:
        index_path = smoke_manifest.rebuild_smoke_index(
            smoke_root,
            smoke_root_fd=smoke_root_fd,
            batches_fd=batches_fd,
        )
    finally:
        os.close(batches_fd)
        os.close(smoke_root_fd)

    assert index_path == smoke_root / smoke_manifest.SMOKE_INDEX_FILENAME
    assert index_path.is_file()
    assert not (smoke_root / "index.lock").exists()


def test_prepare_smoke_root_rejects_writable_forged_owner_directory(tmp_path: Path) -> None:
    smoke_root = tmp_path / "runs" / ".orca_auto_smoke"
    smoke_root.mkdir(parents=True, mode=0o700)
    smoke_root.chmod(0o777)
    marker = smoke_root / smoke_manifest.SMOKE_OWNER_FILENAME
    marker.write_text(
        json.dumps(
            {
                "schema_version": smoke_manifest.SMOKE_SCHEMA_VERSION,
                "kind": "orca_auto_smoke_results",
            }
        ),
        encoding="utf-8",
    )
    marker.chmod(0o600)

    with pytest.raises(ValueError, match="group- or world-writable"):
        prepare_smoke_root(tmp_path / "runs")


def test_batch_creation_does_not_follow_replaced_batches_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Smoke Test")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    (repo / "pyproject.toml").write_text("[project]\nname='tiny'\n", encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    smoke_root = prepare_smoke_root(tmp_path / "runs")
    batches_root = smoke_root / "batches"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_identity = smoke_manifest.source_identity

    def replace_batches(repo_root: Path) -> dict[str, object]:
        identity = original_identity(repo_root)
        held = smoke_root / "batches-held"
        batches_root.rename(held)
        batches_root.symlink_to(outside, target_is_directory=True)
        return identity

    monkeypatch.setattr(smoke_manifest, "source_identity", replace_batches)

    with pytest.raises(ValueError, match="Batches root identity changed"):
        create_pinned_batch_directory(smoke_root, profile="fake", repo_root=repo)

    assert list(outside.iterdir()) == []


def test_batch_directory_name_is_short_and_source_identity_stays_in_manifest(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Smoke Test")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    (repo / "pyproject.toml").write_text("[project]\nname='tiny'\n", encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    smoke_root = prepare_smoke_root(tmp_path / "runs")

    pinned = create_pinned_batch_directory(
        smoke_root,
        profile="fake",
        repo_root=repo,
    )
    try:
        batch_dir, manifest = pinned.batch_dir, pinned.manifest
    finally:
        pinned.close()

    assert re.fullmatch(r"\d{8}-\d{6}-f-[0-9a-f]{6}", batch_dir.name)
    assert len(batch_dir.name) == 24
    assert manifest["batch_id"] == batch_dir.name
    assert manifest["source"]["git_short"]
    assert str(manifest["source"]["git_short"]) not in batch_dir.name


def test_atomic_manifest_publish_failure_is_reported_not_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Smoke manifests are regenerable, so a durability failure after the
    # rename surfaces as an error instead of restoring the previous surface.
    directory_fd = os.open(tmp_path, smoke_manifest.directory_open_flags())
    try:
        smoke_manifest._atomic_write_json_at(
            directory_fd,
            "batch.json",
            {"status": "finalizing"},
        )
        original_fsync = smoke_manifest.os.fsync

        def fail_directory_fsync(descriptor: int) -> None:
            if descriptor == directory_fd:
                raise OSError("injected terminal directory fsync failure")
            original_fsync(descriptor)

        monkeypatch.setattr(smoke_manifest.os, "fsync", fail_directory_fsync)

        with pytest.raises(OSError, match="terminal directory fsync failure"):
            smoke_manifest._atomic_write_json_at(
                directory_fd,
                "batch.json",
                {"status": "passed"},
            )
    finally:
        os.close(directory_fd)

    assert json.loads((tmp_path / "batch.json").read_text(encoding="utf-8")) == {"status": "passed"}


def test_atomic_manifest_cleanup_failure_does_not_reverse_verified_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = os.open(tmp_path, smoke_manifest.directory_open_flags())
    try:
        smoke_manifest._atomic_write_json_at(
            directory_fd,
            "batch.json",
            {"status": "finalizing"},
        )
        original_unlink = smoke_manifest.os.unlink

        def fail_staging_cleanup(path: str, *args: object, **kwargs: object) -> None:
            if str(path).endswith(".tmp"):
                raise PermissionError("injected staging cleanup failure")
            original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(smoke_manifest.os, "unlink", fail_staging_cleanup)

        smoke_manifest._atomic_write_json_at(
            directory_fd,
            "batch.json",
            {"status": "passed"},
        )
    finally:
        os.close(directory_fd)

    assert json.loads((tmp_path / "batch.json").read_text(encoding="utf-8")) == {"status": "passed"}


def _write_job_state(runtime_dir: Path, child_name: str, payload: object) -> None:
    state_path = runtime_dir / child_name / "job_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")


def test_case_manifest_fails_when_completed_and_queued_records_are_both_present(
    tmp_path: Path,
) -> None:
    batch_dir = tmp_path / "batch"
    case_dir = batch_dir / "cases" / "multi_job"
    runtime_dir = case_dir / "runtime"
    _write_job_state(runtime_dir, "completed_job", {"status": "completed"})
    _write_job_state(runtime_dir, "queued_job", {"status": "queued"})
    scenario = SmokeScenario(
        scenario_id="multi_job",
        surface="orca-standalone",
        description="completed and queued children must not pass",
        pytest_selector="tests/test_case.py::test_case",
        expected_status="completed",
        required_artifacts=("job_state.json",),
    )

    runtime_fd = os.open(runtime_dir, smoke_manifest.directory_open_flags())
    try:
        manifest = build_case_manifest(
            scenario=scenario,
            batch_dir=batch_dir,
            case_dir=case_dir,
            runtime_dir=runtime_dir,
            runtime_fd=runtime_fd,
            pytest_result={"passed": True, "skipped": 0},
            started_at="2026-07-14T00:00:00+00:00",
            finished_at="2026-07-14T00:00:01+00:00",
        )
    finally:
        os.close(runtime_fd)

    assert manifest["observed_terminal"]["status"] == "mixed"
    assert {
        record["path"]: record["status"] for record in manifest["observed_terminal"]["records"]
    } == {
        "completed_job/job_state.json": "completed",
        "queued_job/job_state.json": "queued",
    }
    assert manifest["verdict"] == "failed"
    assert manifest["failure_reasons"] == ["terminal_status_mismatch"]


def _observe(runtime_dir: Path, *, surface: str) -> dict[str, Any]:
    runtime_fd = os.open(runtime_dir, smoke_manifest.directory_open_flags())
    try:
        target_name = "workflow.json" if surface == "workflow" else "job_state.json"
        files = smoke_manifest._scan_runtime(
            runtime_fd,
            payload_names=frozenset({target_name}),
        )
    finally:
        os.close(runtime_fd)
    return smoke_manifest._observe_terminal_from_scan(files, surface=surface)


@pytest.mark.parametrize(
    ("statuses", "expected_status"),
    [
        (("completed", "completed"), "completed"),
        (("failed", "failed"), "failed"),
        (("cancelled", "cancelled"), "cancelled"),
        (("completed", "failed"), "mixed"),
    ],
)
def test_observe_terminal_requires_consistent_terminal_records(
    tmp_path: Path,
    statuses: tuple[str, ...],
    expected_status: str,
) -> None:
    for index, status in enumerate(statuses):
        _write_job_state(tmp_path, f"job_{index}", {"status": status})

    observed = _observe(tmp_path, surface="orca-standalone")

    assert observed["status"] == expected_status
    assert sorted(record["status"] for record in observed["records"]) == sorted(statuses)
    assert len(observed["records"]) == len(statuses)


def test_observe_terminal_preserves_invalid_and_missing_status_records(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid" / "job_state.json"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("not-json\n", encoding="utf-8")
    _write_job_state(tmp_path, "missing_status", {"reason": "not terminalized"})
    _write_job_state(tmp_path, "completed", {"status": "completed"})

    observed = _observe(tmp_path, surface="orca-standalone")

    assert observed["status"] == "mixed"
    assert {record["path"]: record["status"] for record in observed["records"]} == {
        "completed/job_state.json": "completed",
        "invalid/job_state.json": "invalid",
        "missing_status/job_state.json": "missing",
    }
    assert observed["reasons"] == ["state_record_invalid", "state_status_missing"]


def test_observe_terminal_reports_missing_when_no_state_records_exist(tmp_path: Path) -> None:
    observed = _observe(tmp_path, surface="orca-standalone")

    assert observed == {"status": "missing", "reasons": [], "records": []}
