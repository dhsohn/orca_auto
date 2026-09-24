from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca_auto.core import engine_scratch as scratch_mod
from orca_auto.core.engine_scratch import (
    EngineScratchCapacityError,
    EngineScratchError,
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

    with pytest.raises(EngineScratchCapacityError, match="insufficient free space"):
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

    with pytest.raises(EngineScratchCapacityError, match="cannot guarantee RAM headroom"):
        EngineScratchWorkspace.create(policy, selected)
    assert _scratch_attempts(policy.root) == []


def test_scratch_reserve_lost_while_staging_is_a_capacity_refusal_without_leftovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    readings = iter([2**40, 0])
    monkeypatch.setattr(scratch_mod, "_filesystem_free_bytes", lambda _descriptor: next(readings))

    with pytest.raises(EngineScratchCapacityError, match="while staging"):
        EngineScratchWorkspace.create(policy, selected)
    assert _scratch_attempts(policy.root) == []


def test_scratch_capacity_refusal_that_leaves_a_workspace_needs_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    readings = iter([2**40, 0])
    monkeypatch.setattr(scratch_mod, "_filesystem_free_bytes", lambda _descriptor: next(readings))

    def refuse_removal(*_args: object) -> None:
        raise OSError("busy")

    monkeypatch.setattr(scratch_mod, "_remove_owned_workspace", refuse_removal)

    with pytest.raises(EngineScratchError, match="could not be removed") as raised:
        EngineScratchWorkspace.create(policy, selected)
    assert not isinstance(raised.value, EngineScratchCapacityError)


def test_scratch_root_lock_timeout_is_a_capacity_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from contextlib import contextmanager

    from orca_auto.core.utils.lock import FileLockTimeoutError

    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)

    @contextmanager
    def contended(*_args: object, **_kwargs: object):
        raise FileLockTimeoutError("Timed out acquiring lock")
        yield

    monkeypatch.setattr(scratch_mod, "file_lock_at", contended)

    with pytest.raises(EngineScratchCapacityError, match="stayed busy"):
        EngineScratchWorkspace.create(policy, selected)
    assert _scratch_attempts(policy.root) == []


def test_unreadable_available_memory_is_not_a_capacity_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)

    def unreadable() -> int:
        raise EngineScratchError("Cannot determine available host memory for engine scratch")

    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", unreadable)

    with pytest.raises(EngineScratchError) as raised:
        EngineScratchWorkspace.create(policy, selected)
    assert not isinstance(raised.value, EngineScratchCapacityError)


def test_discard_unlaunched_removes_the_workspace_and_touches_nothing_durable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    before = sorted(path.name for path in selected.parent.iterdir())
    workspace = EngineScratchWorkspace.create(policy, selected)

    workspace.discard_unlaunched()

    assert _scratch_attempts(policy.root) == []
    assert sorted(path.name for path in selected.parent.iterdir()) == before
    # The root is reusable immediately: nothing stale was left to block the sweep.
    EngineScratchWorkspace.create(policy, selected).discard_unlaunched()


def test_discard_unlaunched_refuses_a_published_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    workspace = EngineScratchWorkspace.create(policy, selected)
    workspace.publish()

    with pytest.raises(EngineScratchError, match="already published"):
        workspace.discard_unlaunched()
    workspace.cleanup()


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
                "schema_version": 2,
                "owner_pid": 999999,
                "owner_process_start_ticks": 1,
                "owner_boot_id": "old-boot",
                "durable_dir": str(selected.parent),
                "max_task_memory_bytes": 1,
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
                "schema_version": 2,
                "owner_pid": 999999,
                "owner_process_start_ticks": 1,
                "owner_boot_id": "00000000-0000-0000-0000-000000000001",
                "durable_dir": str(unrelated_durable.resolve()),
                "max_task_memory_bytes": 1,
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

    assert (
        scratch_mod._manifest_owner_state(
            {
                "owner_pid": os.getpid(),
                "owner_process_start_ticks": 123,
                "owner_boot_id": "boot",
            }
        )
        == "unknown"
    )


def test_unverifiable_owner_blocks_new_attempt_instead_of_being_counted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    selected = _durable_input(tmp_path)
    monkeypatch.setattr(scratch_mod.process_utils, "linux_boot_id", lambda **_kwargs: "boot")
    monkeypatch.setattr(scratch_mod.process_utils, "is_process_alive", lambda _pid: True)
    monkeypatch.setattr(scratch_mod.process_utils, "process_start_ticks", lambda _pid: None)
    unverifiable = root / "attempt-unverifiable"
    unverifiable.mkdir()
    (unverifiable / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 2,
                "owner_pid": os.getpid(),
                "owner_process_start_ticks": 123,
                "owner_boot_id": "boot",
                "durable_dir": str(selected.parent),
                "max_task_memory_bytes": 1,
            }
        )
    )

    with pytest.raises(EngineScratchError, match="owner cannot be verified"):
        EngineScratchWorkspace.create(policy, selected)

    assert unverifiable.exists()
    assert _scratch_attempts(policy.root) == [unverifiable]


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


def test_concurrent_scratch_attempts_share_the_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    selected = _durable_input(tmp_path)
    first = EngineScratchWorkspace.create(policy, selected)
    second = EngineScratchWorkspace.create(policy, selected)

    assert first.path != second.path
    assert len(_scratch_attempts(policy.root)) == 2
    for workspace in (first, second):
        manifest = json.loads(
            (workspace.path / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).read_text(encoding="utf-8")
        )
        assert manifest["schema_version"] == scratch_mod._WORKSPACE_MANIFEST_SCHEMA_VERSION
        assert manifest["max_task_memory_bytes"] == policy.max_task_memory_bytes

    first.publish()
    first.cleanup()
    assert _scratch_attempts(policy.root) == [second.path]
    second.publish()
    second.cleanup()
    assert _scratch_attempts(policy.root) == []


def test_live_workspace_task_memory_caps_count_toward_headroom_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shm = tmp_path / "shm"
    shm.mkdir()
    monkeypatch.setattr(scratch_mod, "_SCRATCH_ROOT_PARENT", shm)
    monkeypatch.setattr(scratch_mod, "_filesystem_free_bytes", lambda _descriptor: 100)
    policy = OrcaScratchPolicy(root=shm / "orca_auto", min_free_bytes=1, max_task_memory_bytes=10)
    selected = _durable_input(tmp_path)
    # One workspace needs 10 + 100 + 1; a second one additionally carries the
    # live workspace's full cap: 10 + 10 + 100 + 1.
    monkeypatch.setattr(scratch_mod, "_linux_available_memory_bytes", lambda: 115)
    first = EngineScratchWorkspace.create(policy, selected)

    with pytest.raises(EngineScratchError, match="live_task_memory_limits=10"):
        EngineScratchWorkspace.create(policy, selected)
    assert _scratch_attempts(policy.root) == [first.path]

    first.publish()
    first.cleanup()
    second = EngineScratchWorkspace.create(policy, selected)
    second.publish()
    second.cleanup()


def test_workspace_manifest_without_task_memory_cap_blocks_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    selected = _durable_input(tmp_path)
    legacy = root / "attempt-legacy"
    legacy.mkdir()
    (legacy / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner_pid": os.getpid(),
                "owner_process_start_ticks": 1,
                "owner_boot_id": "boot",
                "durable_dir": str(selected.parent),
            }
        )
    )

    with pytest.raises(EngineScratchError, match="without valid ownership"):
        EngineScratchWorkspace.create(policy, selected)

    assert legacy.exists()


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


# --- operator inspection and removal -----------------------------------------------------


def _manifest_payload(durable_dir: Path, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 2,
        "owner_pid": os.getpid(),
        "owner_process_start_ticks": scratch_mod.process_utils.current_process_start_ticks(),
        "owner_boot_id": scratch_mod.process_utils.linux_boot_id(proc_root=Path("/proc")),
        "durable_dir": str(durable_dir.resolve()),
        "max_task_memory_bytes": 1,
    }
    payload.update(overrides)
    return payload


def _write_workspace(root: Path, name: str, manifest: dict[str, object] | str) -> Path:
    workspace = root / name
    workspace.mkdir()
    text = manifest if isinstance(manifest, str) else json.dumps(manifest, sort_keys=True) + "\n"
    (workspace / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).write_text(text, encoding="utf-8")
    return workspace


_UNVERIFIABLE_PID = 2**22 - 7


def _patch_unverifiable_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make one sentinel pid look alive with unreadable start ticks; leave others real."""

    real_alive = scratch_mod.process_utils.is_process_alive
    real_ticks = scratch_mod.process_utils.process_start_ticks
    monkeypatch.setattr(
        scratch_mod.process_utils,
        "is_process_alive",
        lambda pid: True if pid == _UNVERIFIABLE_PID else real_alive(pid),
    )
    monkeypatch.setattr(
        scratch_mod.process_utils,
        "process_start_ticks",
        lambda pid, **kwargs: None if pid == _UNVERIFIABLE_PID else real_ticks(pid, **kwargs),
    )


def test_inspect_scratch_root_classifies_every_workspace_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    selected = _durable_input(tmp_path)
    durable = selected.parent
    _patch_unverifiable_pid(monkeypatch)
    live = _write_workspace(root, "attempt-live", _manifest_payload(durable))
    (live / "sp.out").write_bytes(b"x" * 3000)
    (live / scratch_mod.SCRATCH_RUNTIME_HOME_DIR_NAME).mkdir()
    (live / scratch_mod.SCRATCH_RUNTIME_HOME_DIR_NAME / "nested.bin").write_bytes(b"y" * 1000)
    _write_workspace(
        root,
        "attempt-stale",
        _manifest_payload(durable, owner_pid=999999, owner_boot_id="old-boot"),
    )
    _write_workspace(root, "attempt-invalid", "not json\n")
    _write_workspace(
        root,
        "attempt-unverifiable",
        _manifest_payload(durable, owner_pid=_UNVERIFIABLE_PID, owner_process_start_ticks=5),
    )
    (root / "unrelated.txt").write_text("ignored\n", encoding="utf-8")
    tombstone = root / (".orca_auto_cleanup." + "b" * 32)
    tombstone.mkdir()

    reports = scratch_mod.inspect_scratch_root(policy.root)

    by_name = {report.name: report for report in reports}
    assert set(by_name) == {
        "attempt-live",
        "attempt-stale",
        "attempt-invalid",
        "attempt-unverifiable",
        tombstone.name,
    }
    assert by_name["attempt-live"].state == scratch_mod.SCRATCH_STATE_LIVE
    assert by_name["attempt-live"].blocks_launch is False
    assert by_name["attempt-live"].detail is None
    assert by_name["attempt-live"].owner_pid == os.getpid()
    assert by_name["attempt-live"].durable_dir == str(durable.resolve())
    assert by_name["attempt-live"].max_task_memory_bytes == 1
    manifest_size = (live / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).stat().st_size
    assert by_name["attempt-live"].size_bytes == 4000 + manifest_size
    assert by_name["attempt-live"].size_walk_truncated is False
    assert by_name["attempt-live"].publication_journal is None

    stale = by_name["attempt-stale"]
    assert stale.state == scratch_mod.SCRATCH_STATE_STALE
    assert stale.blocks_launch is True
    assert stale.manifest_valid is True
    assert stale.owner_pid == 999999
    assert stale.owner_boot_id == "old-boot"
    assert stale.detail is not None and "stale workspace" in stale.detail

    invalid = by_name["attempt-invalid"]
    assert invalid.state == scratch_mod.SCRATCH_STATE_INVALID_MANIFEST
    assert invalid.manifest_valid is False
    assert invalid.owner_pid is None
    assert invalid.detail is not None and "without valid ownership" in invalid.detail

    unverifiable = by_name["attempt-unverifiable"]
    assert unverifiable.state == scratch_mod.SCRATCH_STATE_UNVERIFIABLE
    assert unverifiable.blocks_launch is True
    assert unverifiable.detail is not None and "owner cannot be verified" in unverifiable.detail

    assert by_name[tombstone.name].state == scratch_mod.SCRATCH_STATE_TOMBSTONE
    assert by_name[tombstone.name].blocks_launch is False

    # The sweep raises with exactly the detail the report carries.
    with pytest.raises(EngineScratchError) as excinfo:
        EngineScratchWorkspace.create(policy, selected)
    assert str(excinfo.value) in {report.detail for report in reports if report.detail is not None}


def test_inspect_scratch_root_handles_missing_root_and_unsafe_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    assert scratch_mod.inspect_scratch_root(policy.root) == []

    root = scratch_mod._prepare_scratch_root(policy)
    (root / "attempt-file").write_bytes(b"not a directory")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "attempt-link").symlink_to(elsewhere)

    reports = scratch_mod.inspect_scratch_root(policy.root)

    assert {report.name: report.state for report in reports} == {
        "attempt-file": scratch_mod.SCRATCH_STATE_UNSAFE,
        "attempt-link": scratch_mod.SCRATCH_STATE_UNSAFE,
    }
    assert all(report.blocks_launch for report in reports)
    with pytest.raises(EngineScratchError, match="name is unsafe"):
        scratch_mod.remove_scratch_workspace(policy.root, "attempt-link/../attempt-link")
    with pytest.raises(EngineScratchError, match="refusing to remove unsafe"):
        scratch_mod.remove_scratch_workspace(policy.root, "attempt-link")
    assert (root / "attempt-link").is_symlink()
    assert elsewhere.is_dir()


def test_inspect_scratch_root_bounds_the_size_walk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    durable = _durable_input(tmp_path).parent
    workspace = _write_workspace(root, "attempt-big", _manifest_payload(durable))
    for index in range(10):
        (workspace / f"chunk-{index}.bin").write_bytes(b"z" * 100)

    [report] = scratch_mod.inspect_scratch_root(policy.root, max_size_entries=4)

    assert report.size_walk_truncated is True
    assert (
        0
        < report.size_bytes
        < 1000 + (workspace / scratch_mod.SCRATCH_MANIFEST_FILE_NAME).stat().st_size
    )


def test_remove_scratch_workspace_refuses_live_and_unblocks_after_stale_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    selected = _durable_input(tmp_path)
    live = _write_workspace(root, "attempt-live", _manifest_payload(selected.parent))
    stale = _write_workspace(
        root,
        "attempt-stale",
        _manifest_payload(selected.parent, owner_pid=999999, owner_boot_id="old-boot"),
    )
    (stale / "partial.out").write_bytes(b"evidence")

    with pytest.raises(EngineScratchError, match="stale workspace"):
        EngineScratchWorkspace.create(policy, selected)
    with pytest.raises(EngineScratchError, match="refusing to remove live"):
        scratch_mod.remove_scratch_workspace(policy.root, "attempt-live")
    with pytest.raises(ValueError, match="live"):
        scratch_mod.remove_scratch_workspace(policy.root, "attempt-stale", allow_states=("live",))
    with pytest.raises(EngineScratchError, match="does not exist"):
        scratch_mod.remove_scratch_workspace(policy.root, "attempt-missing")
    assert live.is_dir() and stale.is_dir()

    removal = scratch_mod.remove_scratch_workspace(policy.root, "attempt-stale")

    assert removal.report.state == scratch_mod.SCRATCH_STATE_STALE
    assert removal.removed_durable_entries == ()
    assert removal.publication_journal_removed is False
    assert not stale.exists()
    assert live.is_dir()
    assert _scratch_attempts(policy.root) == [live]
    # The live peer still counts toward the guard; the stale one no longer blocks.
    workspace = EngineScratchWorkspace.create(policy, selected)
    assert workspace.path.is_dir()
    workspace.discard_unlaunched()


def test_remove_scratch_workspace_clears_only_unjournaled_publication_temps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    durable = _durable_input(tmp_path).parent
    _write_workspace(
        root,
        "attempt-stale",
        _manifest_payload(durable, owner_pid=999999, owner_boot_id="old-boot"),
    )
    stray = durable / f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'a' * 32}.tmp"
    stray.write_bytes(b"half-copied")
    journaled = durable / f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'b' * 32}.tmp"
    journaled.write_bytes(b"journaled")
    other_dir = tmp_path / "other-generation"
    other_dir.mkdir()
    unrelated = other_dir / f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'c' * 32}.tmp"
    unrelated.write_bytes(b"other generation")
    journal = durable / scratch_mod._PUBLICATION_JOURNAL_FILE_NAME
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "prepared",
                "items": [
                    {
                        "temporary_name": journaled.name,
                        "target_name": "sp.out",
                        "backup_name": None,
                        "sha256": "0" * 64,
                        "size_bytes": 9,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status = scratch_mod.durable_publication_journal_status(durable)
    assert status is not None and (status.phase, status.item_count, status.corrupt) == (
        "prepared",
        1,
        False,
    )
    [report] = scratch_mod.inspect_scratch_root(policy.root)
    assert report.publication_journal is not None
    assert report.publication_journal.phase == "prepared"

    removal = scratch_mod.remove_scratch_workspace(policy.root, "attempt-stale")

    assert removal.removed_durable_entries == (stray.name,)
    assert removal.publication_journal_removed is False
    assert removal.durable_note is not None and "left in place for replay" in removal.durable_note
    assert not stray.exists()
    assert journaled.read_bytes() == b"journaled"
    assert journal.is_file()
    assert unrelated.read_bytes() == b"other generation"


def test_remove_scratch_workspace_drops_a_finished_committed_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    durable = _durable_input(tmp_path).parent
    (durable / "sp.out").write_bytes(b"committed")
    _write_workspace(
        root,
        "attempt-stale",
        _manifest_payload(durable, owner_pid=999999, owner_boot_id="old-boot"),
    )
    journal = durable / scratch_mod._PUBLICATION_JOURNAL_FILE_NAME
    journal.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "committed",
                "items": [
                    {
                        "temporary_name": f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'d' * 32}.tmp",
                        "target_name": "sp.out",
                        "backup_name": None,
                        "sha256": "0" * 64,
                        "size_bytes": 9,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    removal = scratch_mod.remove_scratch_workspace(policy.root, "attempt-stale")

    assert removal.publication_journal_removed is True
    assert not journal.exists()
    assert (durable / "sp.out").read_bytes() == b"committed"


def test_remove_scratch_workspace_leaves_durable_files_of_a_live_peer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = _policy(monkeypatch, tmp_path)
    root = scratch_mod._prepare_scratch_root(policy)
    durable = _durable_input(tmp_path).parent
    _write_workspace(root, "attempt-live", _manifest_payload(durable))
    _write_workspace(
        root,
        "attempt-stale",
        _manifest_payload(durable, owner_pid=999999, owner_boot_id="old-boot"),
    )
    stray = durable / f"{scratch_mod._PUBLICATION_TEMP_PREFIX}{'e' * 32}.tmp"
    stray.write_bytes(b"in flight")

    removal = scratch_mod.remove_scratch_workspace(policy.root, "attempt-stale")

    assert removal.removed_durable_entries == ()
    assert removal.durable_note is not None and "live workspace" in removal.durable_note
    assert stray.read_bytes() == b"in flight"


def test_durable_publication_journal_status_reports_corrupt_and_absent(
    tmp_path: Path,
) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    assert scratch_mod.durable_publication_journal_status(durable) is None
    assert scratch_mod.durable_publication_journal_status(tmp_path / "missing") is None

    (durable / scratch_mod._PUBLICATION_JOURNAL_FILE_NAME).write_text("{broken", encoding="utf-8")
    status = scratch_mod.durable_publication_journal_status(durable)

    assert status is not None
    assert status.corrupt is True
    assert status.phase is None
    assert status.detail is not None and "corrupt" in status.detail
