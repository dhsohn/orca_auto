from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from orca_auto.smoke import manifest as smoke_manifest
from orca_auto.smoke.catalog import SmokeScenario
from orca_auto.smoke.manifest import (
    build_case_manifest,
    create_batch_directory,
    observe_terminal,
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


def test_source_identity_changes_when_untracked_content_changes(tmp_path: Path) -> None:
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

    assert first["untracked_digest_complete"] is True
    assert second["untracked_digest_complete"] is True
    assert first["untracked_file_count"] == second["untracked_file_count"] == 1
    assert first["status_digest"] == second["status_digest"]
    assert first["working_tree_digest"] == second["working_tree_digest"]
    assert first["untracked_digest"] != second["untracked_digest"]


def test_source_identity_changes_when_untracked_mode_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Smoke Test")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    (repo / "pyproject.toml").write_text("[project]\nname='tiny'\nversion='1'\n", encoding="utf-8")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    untracked = repo / "smoke_runner.sh"
    untracked.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    untracked.chmod(0o644)

    first = source_identity(repo)
    untracked.chmod(0o755)
    second = source_identity(repo)

    assert first["untracked_digest_complete"] is True
    assert second["untracked_digest_complete"] is True
    assert first["untracked_file_count"] == second["untracked_file_count"] == 1
    assert first["status_digest"] == second["status_digest"]
    assert first["working_tree_digest"] == second["working_tree_digest"]
    assert first["untracked_digest"] != second["untracked_digest"]


def test_source_identity_keeps_later_metadata_after_content_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Smoke Test")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    (repo / "pyproject.toml").write_text(
        "[project]\nname='tiny'\nversion='1'\n",
        encoding="utf-8",
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "initial")
    (repo / "a_over_budget.py").write_text("oversized\n", encoding="utf-8")
    later = repo / "z_later.py"
    later.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(smoke_manifest, "MAX_UNTRACKED_DIGEST_BYTES", 4)

    first = source_identity(repo)
    later.write_text("later metadata changed size\n", encoding="utf-8")
    second = source_identity(repo)

    assert first["untracked_file_count"] == second["untracked_file_count"] == 2
    assert first["untracked_digest_complete"] is False
    assert second["untracked_digest_complete"] is False
    assert first["untracked_digest"] != second["untracked_digest"]


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
        create_batch_directory(smoke_root, profile="fake", repo_root=repo)

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

    batch_dir, manifest = create_batch_directory(
        smoke_root,
        profile="fake",
        repo_root=repo,
    )

    assert re.fullmatch(r"\d{8}-\d{6}-f-[0-9a-f]{6}", batch_dir.name)
    assert len(batch_dir.name) == 24
    assert manifest["batch_id"] == batch_dir.name
    assert manifest["source"]["git_short"]
    assert str(manifest["source"]["git_short"]) not in batch_dir.name


def test_atomic_manifest_publish_rolls_back_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = os.open(tmp_path, smoke_manifest._directory_open_flags())
    try:
        smoke_manifest._atomic_write_json_at(
            directory_fd,
            "batch.json",
            {"status": "finalizing"},
        )
        original_fsync = smoke_manifest.os.fsync
        directory_fsync_calls = 0

        def fail_new_target_fsync(descriptor: int) -> None:
            nonlocal directory_fsync_calls
            if descriptor == directory_fd:
                directory_fsync_calls += 1
                # The first directory fsync makes the old-inode backup durable;
                # the second is the new terminal target's commit attempt.
                if directory_fsync_calls == 2:
                    raise OSError("injected terminal directory fsync failure")
            original_fsync(descriptor)

        monkeypatch.setattr(smoke_manifest.os, "fsync", fail_new_target_fsync)

        with pytest.raises(OSError, match="terminal directory fsync failure"):
            smoke_manifest._atomic_write_json_at(
                directory_fd,
                "batch.json",
                {"status": "passed"},
            )
    finally:
        os.close(directory_fd)

    assert json.loads((tmp_path / "batch.json").read_text(encoding="utf-8")) == {
        "status": "finalizing"
    }


def test_atomic_manifest_cleanup_failure_does_not_reverse_verified_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = os.open(tmp_path, smoke_manifest._directory_open_flags())
    try:
        smoke_manifest._atomic_write_json_at(
            directory_fd,
            "batch.json",
            {"status": "finalizing"},
        )
        original_unlink = smoke_manifest.os.unlink

        def fail_backup_cleanup(path: str, *args: object, **kwargs: object) -> None:
            if str(path).endswith(".bak"):
                raise PermissionError("injected backup cleanup failure")
            original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(smoke_manifest.os, "unlink", fail_backup_cleanup)

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

    manifest = build_case_manifest(
        scenario=scenario,
        batch_dir=batch_dir,
        case_dir=case_dir,
        runtime_dir=runtime_dir,
        pytest_result={"passed": True, "skipped": 0},
        started_at="2026-07-14T00:00:00+00:00",
        finished_at="2026-07-14T00:00:01+00:00",
    )

    assert manifest["observed_terminal"]["status"] == "mixed"
    assert {
        record["path"]: record["status"] for record in manifest["observed_terminal"]["records"]
    } == {
        "completed_job/job_state.json": "completed",
        "queued_job/job_state.json": "queued",
    }
    assert manifest["verdict"] == "failed"
    assert manifest["failure_reasons"] == ["terminal_status_mismatch"]


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

    observed = observe_terminal(tmp_path, surface="orca-standalone")

    assert observed["status"] == expected_status
    assert sorted(record["status"] for record in observed["records"]) == sorted(statuses)
    assert len(observed["records"]) == len(statuses)


def test_observe_terminal_preserves_invalid_and_missing_status_records(tmp_path: Path) -> None:
    invalid_path = tmp_path / "invalid" / "job_state.json"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("not-json\n", encoding="utf-8")
    _write_job_state(tmp_path, "missing_status", {"reason": "not terminalized"})
    _write_job_state(tmp_path, "completed", {"status": "completed"})

    observed = observe_terminal(tmp_path, surface="orca-standalone")

    assert observed["status"] == "mixed"
    assert {record["path"]: record["status"] for record in observed["records"]} == {
        "completed/job_state.json": "completed",
        "invalid/job_state.json": "invalid",
        "missing_status/job_state.json": "missing",
    }
    assert observed["reasons"] == ["state_record_invalid", "state_status_missing"]


def test_observe_terminal_reports_missing_when_no_state_records_exist(tmp_path: Path) -> None:
    observed = observe_terminal(tmp_path, surface="orca-standalone")

    assert observed == {"status": "missing", "reasons": [], "records": []}
