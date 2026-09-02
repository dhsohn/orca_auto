from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.core.engine_scratch import (
    EngineScratchError,
    EngineScratchPolicy,
    EngineScratchWorkspace,
    is_transient_scratch_file,
)
from orca_auto.orca.scratch import OrcaScratchPolicy


def _policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> OrcaScratchPolicy:
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: 2**63)
    return OrcaScratchPolicy(
        root=shm / "orca_auto",
        min_free_bytes=1,
        max_task_memory_bytes=1,
    )


def _durable_input(tmp_path: Path) -> Path:
    durable = tmp_path / "durable"
    durable.mkdir()
    (durable / "input.xyz").write_text("1\ninput\nH 0 0 0\n", encoding="utf-8")
    selected = durable / "sp.inp"
    selected.write_text("! HF STO-3G SP\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    return selected


def _scratch_attempts(root: Path) -> list[Path]:
    return [path for path in root.iterdir() if path.name.startswith("attempt-")]


def test_publish_name_omitted_directory_contributes_no_dirent_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Engine allowlist policies (CREST/xTB publish_name) can omit whole scratch
    # directories; only regular-file bytes may count toward the omitted volume.
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: 2**63)
    policy = EngineScratchPolicy(
        root=shm / "engine",
        min_free_bytes=1,
        max_task_memory_bytes=1,
        publish_name={"sp.out"}.__contains__,
    )
    selected = _durable_input(tmp_path)

    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "sp.out").write_text("done\n", encoding="utf-8")
    (workspace.path / "work.bin").write_bytes(b"x" * 1024)
    work_dir = workspace.path / "workdir"
    work_dir.mkdir()
    (work_dir / "inner.bin").write_bytes(b"z" * 512)

    publication = workspace.publish()

    assert {path.name for path in publication.paths} == {"sp.out"}
    assert set(publication.omitted_transient_files) == {"work.bin", "workdir"}
    assert publication.omitted_transient_bytes == 1024


def test_scratch_create_sweeps_interrupted_cleanup_tombstones(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    policy.root.mkdir(parents=True)
    policy.root.chmod(0o700)
    tombstone = policy.root / (".orca_auto_cleanup." + "a" * 32)
    tombstone.mkdir()
    (tombstone / "leftover.bin").write_bytes(b"x" * 4096)

    # A tombstone is an interrupted cleanup's rename-for-deletion; the next
    # scratch run completes the removal instead of silently pinning tmpfs RAM.
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "sp.out").write_text("done\n", encoding="utf-8")
    publication = workspace.publish()
    workspace.cleanup()

    assert not tombstone.exists()
    assert {path.name for path in publication.paths} == {"sp.out"}


def test_scratch_publishes_surviving_results_once_and_omits_tmp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    original_input = selected.read_bytes()
    original_geometry = selected.with_name("input.xyz").read_bytes()

    workspace = EngineScratchWorkspace.create(policy, selected)
    assert workspace.scratch_input.read_bytes() == original_input
    assert (workspace.path / "input.xyz").read_bytes() == original_geometry
    (workspace.path / "sp.out").write_text("FINAL SINGLE POINT ENERGY -1.0\n")
    (workspace.path / "sp.gbw").write_bytes(b"checkpoint")
    (workspace.path / "sp.property.txt").write_text("properties\n")
    (workspace.path / "sp.EIJ.tmp").write_bytes(b"x" * 4096)
    (workspace.path / "sp.cpscfdata.tmp.7").write_bytes(b"y" * 2048)

    publication = workspace.publish()

    assert {path.name for path in publication.paths} == {
        "sp.gbw",
        "sp.out",
        "sp.property.txt",
    }
    assert publication.omitted_transient_bytes == 6144
    assert set(publication.omitted_transient_files) == {
        "sp.EIJ.tmp",
        "sp.cpscfdata.tmp.7",
    }
    assert selected.read_bytes() == original_input
    assert selected.with_name("input.xyz").read_bytes() == original_geometry
    assert selected.with_suffix(".gbw").read_bytes() == b"checkpoint"
    assert not selected.with_name("sp.EIJ.tmp").exists()

    workspace.cleanup()
    assert not workspace.path.exists()


def test_scratch_can_pin_immutable_input_separately_from_publication_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    durable = tmp_path / "durable"
    snapshots = durable / ".snapshots"
    snapshots.mkdir(parents=True)
    manifest_snapshot = snapshots / "manifest.json"
    manifest_snapshot.write_text('{"job_type":"opt"}\n', encoding="utf-8")
    mutable_manifest = durable / "xtb_job.yaml"
    mutable_manifest.write_text("job_type: opt\n", encoding="utf-8")

    workspace = EngineScratchWorkspace.create(
        policy,
        manifest_snapshot,
        durable_output_dir=durable,
    )
    mutable_manifest.unlink()
    (workspace.path / "xtbopt.xyz").write_text("1\nresult\nH 0 0 0\n", encoding="utf-8")

    publication = workspace.publish()

    assert [path.name for path in publication.paths] == ["xtbopt.xyz"]
    assert (durable / "xtbopt.xyz").is_file()
    assert manifest_snapshot.is_file()
    workspace.cleanup()


def test_scratch_rejects_input_outside_separate_publication_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    snapshot_dir = tmp_path / "snapshots"
    output_dir = tmp_path / "durable"
    snapshot_dir.mkdir()
    output_dir.mkdir()
    manifest_snapshot = snapshot_dir / "manifest.json"
    manifest_snapshot.write_text("{}\n", encoding="utf-8")

    with pytest.raises(EngineScratchError, match="inside its publication directory"):
        EngineScratchWorkspace.create(
            policy,
            manifest_snapshot,
            durable_output_dir=output_dir,
        )


def test_scratch_rejects_changed_staged_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "input.xyz").write_text("1\noptimized\nH 0 0 1\n")

    with pytest.raises(EngineScratchError, match="modified a staged immutable input"):
        workspace.publish()
    assert "input" in selected.with_name("input.xyz").read_text()
    assert workspace.path.exists()


def test_scratch_refuses_to_publish_when_durable_input_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = EngineScratchWorkspace.create(policy, selected)
    selected.write_text("! changed while running\n")

    with pytest.raises(EngineScratchError, match="changed during scratch run"):
        workspace.publish()
    with pytest.raises(EngineScratchError, match="unpublished"):
        workspace.cleanup()
    assert workspace.path.exists()


def test_scratch_refuses_symlink_result_and_retains_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = EngineScratchWorkspace.create(policy, selected)
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (workspace.path / "sp.out").symlink_to(outside)

    with pytest.raises(EngineScratchError, match="unsupported entry"):
        workspace.publish()
    assert workspace.path.exists()
    assert not selected.with_suffix(".out").exists()


def test_scratch_capacity_guard_removes_unowned_new_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    monkeypatch.setattr(
        scratch_mod,
        "_filesystem_free_bytes",
        lambda _descriptor: 0,
    )

    with pytest.raises(EngineScratchError, match="insufficient free space"):
        EngineScratchWorkspace.create(policy, selected)
    assert _scratch_attempts(policy.root) == []


def test_scratch_memory_headroom_guard_removes_unowned_new_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    monkeypatch.setattr(
        scratch_mod,
        "_linux_available_memory_bytes",
        lambda: 1,
    )

    with pytest.raises(EngineScratchError, match="cannot guarantee RAM headroom"):
        EngineScratchWorkspace.create(policy, selected)
    assert _scratch_attempts(policy.root) == []


def test_stale_workspace_is_preserved_and_blocks_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    selected = _durable_input(tmp_path)
    stale = root / "attempt-stale"
    stale.mkdir()
    (stale / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_pid": 999999,
                "owner_process_start_ticks": 1,
                "owner_boot_id": "old-boot",
                "durable_dir": str(selected.parent),
            }
        )
    )

    with pytest.raises(EngineScratchError, match="stale workspace"):
        EngineScratchWorkspace.create(policy, selected)

    assert stale.exists()


def test_unrelated_stale_workspace_is_preserved_and_blocks_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    selected = _durable_input(tmp_path)
    unrelated_durable = tmp_path / "unrelated-durable"
    unrelated_durable.mkdir()
    stale = root / "attempt-unrelated-stale"
    stale.mkdir()
    manifest = stale / scratch_mod.SCRATCH_MANIFEST_FILE_NAME
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_pid": 999999,
                "owner_process_start_ticks": 1,
                "owner_boot_id": "00000000-0000-0000-0000-000000000001",
                "durable_dir": str(unrelated_durable.resolve()),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sentinel = stale / "partial.out"
    sentinel.write_bytes(b"preserve-unrelated-stale-evidence\n")
    manifest_before = manifest.read_bytes()
    sentinel_before = sentinel.read_bytes()

    with pytest.raises(EngineScratchError, match="stale workspace"):
        EngineScratchWorkspace.create(policy, selected)

    assert manifest.read_bytes() == manifest_before
    assert sentinel.read_bytes() == sentinel_before


def test_alive_owner_with_unreadable_start_ticks_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scratch_mod.process_utils, "linux_boot_id", lambda **_kwargs: "boot")
    monkeypatch.setattr(scratch_mod.process_utils, "is_process_alive", lambda _pid: True)
    monkeypatch.setattr(scratch_mod.process_utils, "process_start_ticks", lambda _pid: None)

    assert scratch_mod._manifest_owner_is_live(
        {
            "owner_pid": os.getpid(),
            "owner_process_start_ticks": 123,
            "owner_boot_id": "boot",
        }
    )


def test_invalid_workspace_manifest_is_preserved_and_blocks_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    unresolved = root / "attempt-unresolved"
    unresolved.mkdir()
    (unresolved / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).write_text("not json\n")

    with pytest.raises(EngineScratchError, match="without valid ownership"):
        EngineScratchWorkspace.create(policy, selected)

    assert unresolved.exists()


def test_orphaned_durable_publication_entry_is_preserved_and_blocks_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    orphan = selected.parent / f"{scratch_mod._PUBLICATION_BACKUP_PREFIX}orphan"
    orphan.write_bytes(b"unknown prior artifact")

    with pytest.raises(EngineScratchError, match="unresolved scratch publication entry"):
        EngineScratchWorkspace.create(policy, selected)

    assert orphan.read_bytes() == b"unknown prior artifact"


def test_journal_names_cannot_escape_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("keep\n", encoding="utf-8")
    (selected.parent / scratch_mod._PUBLICATION_JOURNAL_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "prepared",
                "items": [
                    {
                        "temporary_name": ".orca_auto_publish.route/../../victim",
                        "target_name": "sp.out",
                        "backup_name": None,
                        "sha256": "0" * 64,
                        "size_bytes": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EngineScratchError, match="invalid item"):
        EngineScratchWorkspace.create(policy, selected)

    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_prepared_journal_never_deletes_unverified_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    original = selected.read_bytes()
    (selected.parent / scratch_mod._PUBLICATION_JOURNAL_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "prepared",
                "items": [
                    {
                        "temporary_name": f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'0' * 32}.tmp",
                        "target_name": selected.name,
                        "backup_name": None,
                        "sha256": "0" * 64,
                        "size_bytes": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EngineScratchError, match="target changed"):
        EngineScratchWorkspace.create(policy, selected)

    assert selected.read_bytes() == original


def test_only_one_scratch_attempt_can_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    first = EngineScratchWorkspace.create(policy, selected)

    with pytest.raises(EngineScratchError, match="attempt is active"):
        EngineScratchWorkspace.create(policy, selected)

    first.publish()
    first.cleanup()
    second = EngineScratchWorkspace.create(policy, selected)
    second.publish()
    second.cleanup()


def test_input_capture_is_not_rebound_between_preflight_and_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    original = selected.read_bytes()
    original_size = scratch_mod._input_closure_size_bytes

    def replace_after_capture(selected_name, captured_inputs, **kwargs):
        size = original_size(selected_name, captured_inputs, **kwargs)
        selected.write_text("! replaced after capture\n", encoding="utf-8")
        return size

    monkeypatch.setattr(
        scratch_mod,
        "_input_closure_size_bytes",
        replace_after_capture,
    )

    workspace = EngineScratchWorkspace.create(policy, selected)

    assert workspace.scratch_input.read_bytes() == original
    with pytest.raises(EngineScratchError, match="changed during scratch run"):
        workspace.publish()


def test_workspace_path_replacement_cannot_publish_forged_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = EngineScratchWorkspace.create(policy, selected)
    moved = workspace.path.with_name(f"{workspace.path.name}-moved")
    workspace.path.rename(moved)
    workspace.path.mkdir()
    for name in ("sp.inp", "input.xyz"):
        (workspace.path / name).write_bytes((moved / name).read_bytes())
    (workspace.path / "sp.out").write_text("forged\n", encoding="utf-8")

    with pytest.raises(EngineScratchError, match="workspace pathname identity changed"):
        workspace.publish()

    assert not selected.with_suffix(".out").exists()
    assert (workspace.path / "sp.out").read_text(encoding="utf-8") == "forged\n"


def test_generation_path_swap_during_publication_rolls_back_original_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    original_dir = selected.parent
    moved_dir = tmp_path / "moved-generation"
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "sp.out").write_text("new output\n", encoding="utf-8")

    original_identity_check = scratch_mod._require_directory_path_identity
    calls = 0

    def swap_before_commit_check(*args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            original_dir.rename(moved_dir)
            original_dir.mkdir()
            (original_dir / "sp.inp").write_bytes((moved_dir / "sp.inp").read_bytes())
            (original_dir / "input.xyz").write_bytes((moved_dir / "input.xyz").read_bytes())
        original_identity_check(*args, **kwargs)

    monkeypatch.setattr(
        scratch_mod,
        "_require_directory_path_identity",
        swap_before_commit_check,
    )

    with pytest.raises(EngineScratchError, match="pathname identity changed"):
        workspace.publish()

    assert not (original_dir / "sp.out").exists()
    assert not (moved_dir / "sp.out").exists()
    assert workspace.path.exists()
    assert not any(path.name.startswith(".orca_auto_publish") for path in moved_dir.iterdir())


def test_multi_file_publication_failure_restores_previous_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    selected.with_suffix(".gbw").write_bytes(b"old checkpoint")
    selected.with_suffix(".out").write_bytes(b"old output")
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "sp.gbw").write_bytes(b"new checkpoint")
    (workspace.path / "sp.out").write_bytes(b"new output")

    original_replace = scratch_mod.os.replace

    def fail_second_artifact(source, target, *args, **kwargs):
        if str(source).startswith(scratch_mod._PUBLICATION_TEMP_PREFIX) and target == "sp.out":
            raise OSError("injected second artifact failure")
        return original_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(scratch_mod.os, "replace", fail_second_artifact)

    with pytest.raises(OSError, match="second artifact"):
        workspace.publish()

    assert selected.with_suffix(".gbw").read_bytes() == b"old checkpoint"
    assert selected.with_suffix(".out").read_bytes() == b"old output"
    assert not any(path.name.startswith(".orca_auto_") for path in selected.parent.iterdir())


def test_committed_publication_cleanup_is_retried_without_invalidating_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    selected.with_suffix(".out").write_bytes(b"old output")
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "sp.out").write_bytes(b"new output")

    original_unlink = scratch_mod._unlink_at_if_present
    failed = False

    def fail_cleanup_once(directory_fd: int, name: str) -> None:
        nonlocal failed
        if not failed and name.startswith(scratch_mod._PUBLICATION_BACKUP_PREFIX):
            failed = True
            raise OSError("injected cleanup failure")
        original_unlink(directory_fd, name)

    monkeypatch.setattr(scratch_mod, "_unlink_at_if_present", fail_cleanup_once)

    publication = workspace.publish()

    assert [path.name for path in publication.paths] == ["sp.out"]
    assert selected.with_suffix(".out").read_bytes() == b"new output"
    workspace.cleanup()


def test_committed_journal_outcome_unknown_is_recovered_as_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "sp.out").write_bytes(b"new output")

    original_write = scratch_mod._atomic_write_json_at
    injected = False

    def raise_after_committed_write(directory_fd: int, name: str, payload: dict) -> None:
        nonlocal injected
        original_write(directory_fd, name, payload)
        if payload.get("phase") == "committed" and not injected:
            injected = True
            raise OSError("injected post-commit fsync outcome")

    monkeypatch.setattr(scratch_mod, "_atomic_write_json_at", raise_after_committed_write)

    publication = workspace.publish()

    assert [path.name for path in publication.paths] == ["sp.out"]
    assert selected.with_suffix(".out").read_bytes() == b"new output"
    workspace.cleanup()


def test_reserved_runtime_artifact_is_never_published(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    state_path = selected.parent / "job_state.json"
    state_path.write_text("durable state\n", encoding="utf-8")
    workspace = EngineScratchWorkspace.create(policy, selected)
    (workspace.path / "job_state.json").write_text("scratch state\n", encoding="utf-8")

    with pytest.raises(EngineScratchError, match="collides with runtime state"):
        workspace.publish()

    assert state_path.read_text(encoding="utf-8") == "durable state\n"


def test_attempt_stem_containing_tmp_does_not_hide_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    renamed = selected.with_name("molecule.tmp.inp")
    selected.rename(renamed)
    workspace = EngineScratchWorkspace.create(policy, renamed)
    (workspace.path / "molecule.tmp.out").write_text("output\n", encoding="utf-8")
    (workspace.path / "molecule.tmp.gbw").write_bytes(b"checkpoint")
    (workspace.path / "molecule.tmp.EIJ.tmp").write_bytes(b"transient")

    publication = workspace.publish()

    assert {path.name for path in publication.paths} == {
        "molecule.tmp.out",
        "molecule.tmp.gbw",
    }
    assert publication.omitted_transient_files == ("molecule.tmp.EIJ.tmp",)
    workspace.cleanup()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sp.EIJ.tmp", True),
        ("sp.cpscfdata.tmp.7", True),
        ("sp.tmp.proc0.0", True),
        ("sp.property.txt", False),
        ("sp.gbw", False),
    ],
)
def test_transient_orca_scratch_file_classification(name: str, expected: bool) -> None:
    assert is_transient_scratch_file(name) is expected
