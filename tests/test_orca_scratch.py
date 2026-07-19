from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.orca.scratch import (
    OrcaScratchError,
    OrcaScratchPolicy,
    OrcaScratchWorkspace,
    is_transient_orca_scratch_file,
)


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


def test_scratch_publishes_surviving_results_once_and_omits_tmp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    original_input = selected.read_bytes()
    original_geometry = selected.with_name("input.xyz").read_bytes()

    workspace = OrcaScratchWorkspace.create(policy, selected)
    assert workspace.scratch_input.read_bytes() == original_input
    assert (workspace.path / "input.xyz").read_bytes() == original_geometry
    (workspace.path / "sp.out").write_text("FINAL SINGLE POINT ENERGY -1.0\n")
    (workspace.path / "sp.gbw").write_bytes(b"checkpoint")
    (workspace.path / "sp.property.txt").write_text("properties\n")
    (workspace.path / "sp.EIJ.tmp").write_bytes(b"x" * 4096)
    (workspace.path / "sp.cpscfdata.tmp.7").write_bytes(b"y" * 2048)
    (workspace.path / "orca.process.json").write_text("{}\n", encoding="utf-8")
    (workspace.path / ".orca.process.lock").write_text("", encoding="utf-8")

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
    assert not selected.with_name("orca.process.json").exists()
    assert not selected.with_name(".orca.process.lock").exists()

    workspace.cleanup()
    assert not workspace.path.exists()


def test_scratch_rejects_changed_staged_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = OrcaScratchWorkspace.create(policy, selected)
    (workspace.path / "input.xyz").write_text("1\noptimized\nH 0 0 1\n")

    with pytest.raises(OrcaScratchError, match="modified a staged immutable input"):
        workspace.publish()
    assert "input" in selected.with_name("input.xyz").read_text()
    assert workspace.path.exists()


def test_scratch_refuses_to_publish_when_durable_input_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = OrcaScratchWorkspace.create(policy, selected)
    selected.write_text("! changed while running\n")

    with pytest.raises(OrcaScratchError, match="changed during scratch run"):
        workspace.publish()
    with pytest.raises(OrcaScratchError, match="unpublished"):
        workspace.cleanup()
    assert workspace.path.exists()


def test_scratch_refuses_symlink_result_and_retains_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = OrcaScratchWorkspace.create(policy, selected)
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (workspace.path / "sp.out").symlink_to(outside)

    with pytest.raises(OrcaScratchError, match="unsupported entry"):
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

    with pytest.raises(OrcaScratchError, match="insufficient free space"):
        OrcaScratchWorkspace.create(policy, selected)
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

    with pytest.raises(OrcaScratchError, match="cannot guarantee RAM headroom"):
        OrcaScratchWorkspace.create(policy, selected)
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

    with pytest.raises(OrcaScratchError, match="stale workspace"):
        OrcaScratchWorkspace.create(policy, selected)

    assert stale.exists()


def test_alive_owner_with_unreadable_start_ticks_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scratch_mod.process_utils, "linux_boot_id", lambda **_kwargs: "boot")
    monkeypatch.setattr(scratch_mod.process_lock, "is_process_alive", lambda _pid: True)
    monkeypatch.setattr(scratch_mod.process_lock, "process_start_ticks", lambda _pid: None)

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

    with pytest.raises(OrcaScratchError, match="without valid ownership"):
        OrcaScratchWorkspace.create(policy, selected)

    assert unresolved.exists()


def test_orphaned_durable_publication_entry_is_preserved_and_blocks_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    orphan = selected.parent / f"{scratch_mod._PUBLICATION_BACKUP_PREFIX}orphan"
    orphan.write_bytes(b"unknown prior artifact")

    with pytest.raises(OrcaScratchError, match="unresolved scratch publication entry"):
        OrcaScratchWorkspace.create(policy, selected)

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

    with pytest.raises(OrcaScratchError, match="invalid item"):
        OrcaScratchWorkspace.create(policy, selected)

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

    with pytest.raises(OrcaScratchError, match="target changed"):
        OrcaScratchWorkspace.create(policy, selected)

    assert selected.read_bytes() == original


def test_only_one_scratch_attempt_can_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    first = OrcaScratchWorkspace.create(policy, selected)

    with pytest.raises(OrcaScratchError, match="attempt is active"):
        OrcaScratchWorkspace.create(policy, selected)

    first.publish()
    first.cleanup()
    second = OrcaScratchWorkspace.create(policy, selected)
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

    workspace = OrcaScratchWorkspace.create(policy, selected)

    assert workspace.scratch_input.read_bytes() == original
    with pytest.raises(OrcaScratchError, match="changed during scratch run"):
        workspace.publish()


def test_workspace_path_replacement_cannot_publish_forged_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = OrcaScratchWorkspace.create(policy, selected)
    moved = workspace.path.with_name(f"{workspace.path.name}-moved")
    workspace.path.rename(moved)
    workspace.path.mkdir()
    for name in ("sp.inp", "input.xyz"):
        (workspace.path / name).write_bytes((moved / name).read_bytes())
    (workspace.path / "sp.out").write_text("forged\n", encoding="utf-8")

    with pytest.raises(OrcaScratchError, match="workspace pathname identity changed"):
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
    workspace = OrcaScratchWorkspace.create(policy, selected)
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

    with pytest.raises(OrcaScratchError, match="pathname identity changed"):
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
    workspace = OrcaScratchWorkspace.create(policy, selected)
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
    workspace = OrcaScratchWorkspace.create(policy, selected)
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
    workspace = OrcaScratchWorkspace.create(policy, selected)
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
    workspace = OrcaScratchWorkspace.create(policy, selected)
    (workspace.path / "job_state.json").write_text("scratch state\n", encoding="utf-8")

    with pytest.raises(OrcaScratchError, match="collides with runtime state"):
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
    workspace = OrcaScratchWorkspace.create(policy, renamed)
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
    assert is_transient_orca_scratch_file(name) is expected
