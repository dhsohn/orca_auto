from __future__ import annotations

import fcntl
import os
import stat
import threading
from multiprocessing import get_context
from pathlib import Path

import pytest

from orca_auto.core.utils import lock as lock_module
from orca_auto.core.utils.lock import (
    file_lock,
    file_lock_at,
    tmpfs_file_lock,
    tmpfs_file_lock_at,
)


def _hold_lock_until_released(lock_path: str, ready, release) -> None:
    with file_lock(Path(lock_path), timeout_seconds=1.0):
        ready.set()
        release.wait()


def _hold_tmpfs_lock_until_released(
    logical_lock_path: str,
    tmpfs_root: str,
    ready,
    release,
) -> None:
    lock_module._TMPFS_ROOT = Path(tmpfs_root)
    with tmpfs_file_lock(Path(logical_lock_path), timeout_seconds=1.0):
        ready.set()
        release.wait()


def _try_inherited_tmpfs_lock(
    logical_lock_path: str,
    tmpfs_root: str,
    result_queue,
) -> None:
    lock_module._TMPFS_ROOT = Path(tmpfs_root)
    try:
        with tmpfs_file_lock(Path(logical_lock_path), timeout_seconds=0.05):
            result_queue.put("acquired")
    except TimeoutError:
        result_queue.put("timeout")


def _use_fake_tmpfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    root = tmp_path / "shm"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(lock_module, "_TMPFS_ROOT", root)
    return root


def _tmpfs_namespace(root: Path) -> Path:
    return root / f"orca_auto-locks-{os.geteuid()}"


def _tmpfs_lock_path(root: Path, logical_lock_path: Path) -> Path:
    lock_name = lock_module._tmpfs_lock_name(
        logical_lock_path,
        effective_uid=os.geteuid(),
    )
    return _tmpfs_namespace(root) / lock_name


def _tmpfs_lock_path_at(root: Path, directory_fd: int, lock_name: str) -> Path:
    name = lock_module._tmpfs_lock_name_at(
        os.fstat(directory_fd),
        lock_name,
        effective_uid=os.geteuid(),
    )
    return _tmpfs_namespace(root) / name


def _directory_status(
    *,
    st_mode: int = stat.S_IFDIR | 0o700,
    st_ino: int = 1,
    st_dev: int = 1,
    st_nlink: int = 1,
    st_uid: int | None = None,
) -> os.stat_result:
    owner_uid = os.geteuid() if st_uid is None else st_uid
    return os.stat_result((st_mode, st_ino, st_dev, st_nlink, owner_uid, os.getegid(), 0, 0, 0, 0))


def _colliding_logical_paths(tmp_path: Path) -> tuple[Path, Path, str]:
    effective_uid = os.geteuid()
    seen: dict[str, Path] = {}
    for index in range(lock_module._TMPFS_LOCK_STRIPE_COUNT + 1):
        candidate = tmp_path / "logical" / f"resource-{index}.lock"
        name = lock_module._tmpfs_lock_name(candidate, effective_uid=effective_uid)
        previous = seen.get(name)
        if previous is not None:
            return previous, candidate, name
        seen[name] = candidate
    raise AssertionError("fixed stripe pool did not produce a deterministic collision")


def _colliding_pathname_and_at_lock(
    tmp_path: Path,
    directory_fd: int,
) -> tuple[Path, str, str]:
    effective_uid = os.geteuid()
    logical_path = tmp_path / "pathname-resource.lock"
    shared_name = lock_module._tmpfs_lock_name(
        logical_path,
        effective_uid=effective_uid,
    )
    directory_status = os.fstat(directory_fd)
    for index in range(lock_module._TMPFS_LOCK_STRIPE_COUNT * 32):
        lock_name = f"dirfd-resource-{index}.lock"
        if (
            lock_module._tmpfs_lock_name_at(
                directory_status,
                lock_name,
                effective_uid=effective_uid,
            )
            == shared_name
        ):
            return logical_path, lock_name, shared_name
    raise AssertionError("pathname and dirfd identities did not collide within the bounded search")


def test_file_lock_writes_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "nested" / "resource.lock"
    monkeypatch.setattr("orca_auto.core.utils.lock.os.getpid", lambda: 4321)
    monkeypatch.setattr(
        "orca_auto.core.utils.lock.now_utc_iso", lambda: "2026-04-19T12:34:56+00:00"
    )

    with file_lock(lock_path):
        contents = lock_path.read_text(encoding="utf-8")

    assert lock_path.parent.exists()
    assert contents == "pid=4321\nacquired_at=2026-04-19T12:34:56+00:00\n"


def test_file_lock_times_out_when_lock_is_held(tmp_path: Path) -> None:
    ctx = get_context("fork")
    lock_path = tmp_path / "held.lock"
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_lock_until_released,
        args=(str(lock_path), ready, release),
    )
    process.start()

    try:
        acquired = False
        for _ in range(100):
            acquired = ready.wait(0.1)
            if acquired or process.exitcode is not None:
                break
        assert acquired, "child process never acquired the lock"

        with pytest.raises(TimeoutError, match=r"Timed out acquiring lock: .*held\.lock"):
            with file_lock(lock_path, timeout_seconds=0.05):
                pass
    finally:
        release.set()
        for _ in range(100):
            process.join(timeout=0.1)
            if not process.is_alive():
                break
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_file_lock_ignores_unlock_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock_path = tmp_path / "resource.lock"
    calls: list[int] = []

    def fake_flock(fd: int, flags: int) -> None:
        calls.append(flags)
        if flags & fcntl.LOCK_UN:
            raise OSError("unlock failed")

    monkeypatch.setattr("orca_auto.core.utils.lock.fcntl.flock", fake_flock)

    with file_lock(lock_path):
        pass

    assert fcntl.LOCK_UN in calls


def test_file_lock_at_uses_pinned_directory_descriptor(tmp_path: Path) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with file_lock_at(
            directory_fd,
            "resource.lock",
            display_path=tmp_path / "resource.lock",
        ):
            contents = (tmp_path / "resource.lock").read_text(encoding="utf-8")
    finally:
        os.close(directory_fd)

    assert f"pid={os.getpid()}\n" in contents


@pytest.mark.parametrize("lock_name", ["", ".", "..", "nested/resource.lock"])
def test_file_lock_at_rejects_non_plain_names(tmp_path: Path, lock_name: str) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="one plain filename"):
            with file_lock_at(directory_fd, lock_name):
                pass
    finally:
        os.close(directory_fd)


def test_tmpfs_file_lock_uses_fixed_root() -> None:
    assert lock_module._TMPFS_ROOT == Path("/dev/shm")


def test_tmpfs_lock_mapping_uses_one_bounded_versioned_stripe_pool(
    tmp_path: Path,
) -> None:
    stripe_count = lock_module._TMPFS_LOCK_STRIPE_COUNT
    effective_uid = os.geteuid()
    assert stripe_count == 1 << 12
    assert stripe_count & (stripe_count - 1) == 0

    names = {
        lock_module._tmpfs_lock_name(
            tmp_path / "logical" / f"resource-{index}.lock",
            effective_uid=effective_uid,
        )
        for index in range(stripe_count + 1)
    }
    names.update(
        lock_module._tmpfs_lock_name_at(
            _directory_status(st_dev=17, st_ino=index),
            "resource.lock",
            effective_uid=effective_uid,
        )
        for index in range(stripe_count + 1)
    )

    assert len(names) <= stripe_count
    for name in names:
        assert name.startswith(lock_module._TMPFS_LOCK_STRIPE_FILENAME_PREFIX)
        assert name.endswith(".lock")
        stripe_text = name.removeprefix(
            lock_module._TMPFS_LOCK_STRIPE_FILENAME_PREFIX
        ).removesuffix(".lock")
        assert len(stripe_text) == 4
        assert stripe_text.isdecimal()
        assert 0 <= int(stripe_text) < stripe_count


def test_tmpfs_lock_mapping_is_stable_for_same_logical_identity(tmp_path: Path) -> None:
    effective_uid = os.geteuid()
    canonical = tmp_path / "resource.lock"
    alias = tmp_path / "nested" / ".." / "resource.lock"
    directory_status = _directory_status(st_dev=17, st_ino=23)
    directory_alias_status = _directory_status(st_dev=17, st_ino=23)

    assert lock_module._tmpfs_lock_name(
        alias,
        effective_uid=effective_uid,
    ) == lock_module._tmpfs_lock_name(canonical, effective_uid=effective_uid)
    assert lock_module._tmpfs_lock_name_at(
        directory_status,
        "resource.lock",
        effective_uid=effective_uid,
    ) == lock_module._tmpfs_lock_name_at(
        directory_alias_status,
        "resource.lock",
        effective_uid=effective_uid,
    )


def test_tmpfs_file_lock_reenters_pathname_and_dirfd_same_thread_stripe_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    directory = tmp_path / "dirfd-parent"
    directory.mkdir()
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        logical_path, lock_name, shared_name = _colliding_pathname_and_at_lock(
            tmp_path,
            directory_fd,
        )
        with tmpfs_file_lock(logical_path):
            with tmpfs_file_lock_at(directory_fd, lock_name, timeout_seconds=0.0):
                assert _tmpfs_lock_path(root, logical_path) == _tmpfs_lock_path_at(
                    root,
                    directory_fd,
                    lock_name,
                )
    finally:
        os.close(directory_fd)

    assert [path.name for path in _tmpfs_namespace(root).iterdir()] == [shared_name]


def test_tmpfs_file_lock_exact_identity_reentry_acquires_one_external_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "logical.lock"
    real_flock = fcntl.flock
    operations: list[int] = []

    def recording_flock(descriptor: int, operation: int) -> None:
        operations.append(operation)
        real_flock(descriptor, operation)

    monkeypatch.setattr(lock_module.fcntl, "flock", recording_flock)

    with tmpfs_file_lock(logical):
        with tmpfs_file_lock(logical):
            pass

    assert sum(bool(operation & fcntl.LOCK_EX) for operation in operations) == 1
    assert sum(bool(operation & fcntl.LOCK_UN) for operation in operations) == 1


def test_tmpfs_file_lock_same_stripe_still_serializes_other_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fake_tmpfs(tmp_path, monkeypatch)
    first, second, _shared_name = _colliding_logical_paths(tmp_path)
    started = threading.Event()
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def acquire_colliding_identity() -> None:
        try:
            started.set()
            with tmpfs_file_lock(second, timeout_seconds=2.0):
                acquired.set()
                release.wait(timeout=2.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=acquire_colliding_identity)
    with tmpfs_file_lock(first):
        thread.start()
        assert started.wait(timeout=1.0)
        assert not acquired.wait(timeout=0.2)

    try:
        assert acquired.wait(timeout=2.0)
    finally:
        release.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert errors == []


def test_tmpfs_file_lock_fork_child_cannot_reuse_inherited_thread_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "logical.lock"
    ctx = get_context("fork")
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=_try_inherited_tmpfs_lock,
        args=(str(logical), str(root), result_queue),
    )

    with tmpfs_file_lock(logical):
        process.start()
        assert result_queue.get(timeout=5) == "timeout"

    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    assert process.exitcode == 0


def test_tmpfs_file_lock_aliases_share_one_deterministic_inode_without_disk_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical_parent = tmp_path / "disk-state"
    canonical = logical_parent / "resource.lock"
    alias = logical_parent / "nested" / ".." / "resource.lock"

    with tmpfs_file_lock(alias):
        first_status = _tmpfs_lock_path(root, alias).stat()
    with tmpfs_file_lock(canonical):
        second_status = _tmpfs_lock_path(root, canonical).stat()

    assert (first_status.st_dev, first_status.st_ino) == (
        second_status.st_dev,
        second_status.st_ino,
    )
    assert len(list(_tmpfs_namespace(root).iterdir())) == 1
    assert not logical_parent.exists()


def test_tmpfs_file_lock_does_not_follow_replaced_logical_leaf_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "resource.lock"
    first_target = tmp_path / "first-target"
    second_target = tmp_path / "second-target"
    first_target.touch()
    second_target.touch()
    logical.symlink_to(first_target)

    with tmpfs_file_lock(logical):
        first_lock_path = _tmpfs_lock_path(root, logical)
        logical.unlink()
        logical.symlink_to(second_target)
        second_lock_path = _tmpfs_lock_path(root, logical)

        assert second_lock_path == first_lock_path
        with tmpfs_file_lock(logical, timeout_seconds=0.0):
            assert _tmpfs_lock_path(root, logical) == first_lock_path

    assert len(list(_tmpfs_namespace(root).iterdir())) == 1


def test_tmpfs_file_lock_at_rename_aliases_share_one_inode_without_disk_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    original = tmp_path / "original"
    renamed = tmp_path / "renamed"
    original.mkdir()
    first_fd = os.open(original, os.O_RDONLY | os.O_DIRECTORY)
    original.rename(renamed)
    second_fd = os.open(renamed, os.O_RDONLY | os.O_DIRECTORY)
    try:
        actual_lock = _tmpfs_lock_path_at(root, first_fd, "resource.lock")
        assert actual_lock == _tmpfs_lock_path_at(root, second_fd, "resource.lock")

        with tmpfs_file_lock_at(first_fd, "resource.lock"):
            assert actual_lock.is_file()
            with tmpfs_file_lock_at(second_fd, "resource.lock", timeout_seconds=0.0):
                assert _tmpfs_lock_path_at(root, second_fd, "resource.lock") == actual_lock

        assert actual_lock.is_file()
        assert not (renamed / "resource.lock").exists()
        assert len(list(_tmpfs_namespace(root).iterdir())) == 1
    finally:
        os.close(second_fd)
        os.close(first_fd)


@pytest.mark.parametrize("lock_name", ["", ".", "..", "nested/resource.lock"])
def test_tmpfs_file_lock_at_rejects_non_plain_names(tmp_path: Path, lock_name: str) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="one plain filename"):
            with tmpfs_file_lock_at(directory_fd, lock_name):
                pass
    finally:
        os.close(directory_fd)


def test_tmpfs_file_lock_at_rejects_non_directory_fd(tmp_path: Path) -> None:
    target = tmp_path / "regular-file"
    target.touch()
    target_fd = os.open(target, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="must reference a directory"):
            with tmpfs_file_lock_at(target_fd, "resource.lock"):
                pass
    finally:
        os.close(target_fd)


def test_tmpfs_file_lock_at_rejects_closed_fd(tmp_path: Path) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    os.close(directory_fd)

    with pytest.raises(ValueError, match="cannot be pinned safely"):
        with tmpfs_file_lock_at(directory_fd, "resource.lock"):
            pass


def test_tmpfs_file_lock_at_rejects_foreign_directory_owner(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="owned"):
        lock_module._validate_tmpfs_lock_directory_status(
            _directory_status(st_uid=os.geteuid() + 1),
            effective_uid=os.geteuid(),
            display_path=tmp_path / "resource.lock",
        )


def test_tmpfs_file_lock_creates_private_namespace_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "disk-state" / "resource.lock"

    with tmpfs_file_lock(logical):
        namespace_status = _tmpfs_namespace(root).stat()
        lock_status = _tmpfs_lock_path(root, logical).stat()

    assert namespace_status.st_uid == os.geteuid()
    assert namespace_status.st_dev == root.stat().st_dev
    assert stat.S_IMODE(namespace_status.st_mode) == 0o700
    assert stat.S_ISREG(lock_status.st_mode)
    assert lock_status.st_uid == os.geteuid()
    assert lock_status.st_nlink == 1
    assert stat.S_IMODE(lock_status.st_mode) == 0o600


@pytest.mark.parametrize("unsafe_kind", ["file", "symlink"])
def test_tmpfs_file_lock_rejects_unsafe_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    namespace = _tmpfs_namespace(root)
    if unsafe_kind == "file":
        namespace.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path / "namespace-target"
        target.mkdir()
        namespace.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="namespace"):
        with tmpfs_file_lock(tmp_path / "logical.lock"):
            pass


@pytest.mark.parametrize(
    ("status_overrides", "message"),
    [
        ({"st_uid": os.geteuid() + 1}, "owned"),
        ({"st_mode": stat.S_IFDIR | 0o755}, "0700"),
        ({"st_dev": 987654321}, "/dev/shm"),
    ],
)
def test_tmpfs_namespace_status_rejects_wrong_owner_mode_or_device(
    tmp_path: Path,
    status_overrides: dict[str, int],
    message: str,
) -> None:
    values = {
        "st_mode": stat.S_IFDIR | 0o700,
        "st_uid": os.geteuid(),
        "st_dev": 12345,
    }
    values.update(status_overrides)

    with pytest.raises(ValueError, match=message):
        lock_module._validate_tmpfs_namespace_status(
            _directory_status(
                st_mode=values["st_mode"],
                st_uid=values["st_uid"],
                st_dev=values["st_dev"],
            ),
            effective_uid=os.geteuid(),
            root_device=12345,
            display_path=tmp_path / "namespace",
        )


def test_tmpfs_file_lock_rejects_lock_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    namespace = _tmpfs_namespace(root)
    namespace.mkdir(mode=0o700)
    logical = tmp_path / "logical.lock"
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    _tmpfs_lock_path(root, logical).symlink_to(target)

    with pytest.raises(ValueError, match="opened safely"):
        with tmpfs_file_lock(logical):
            pass


def test_tmpfs_file_lock_rejects_hardlinked_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    namespace = _tmpfs_namespace(root)
    namespace.mkdir(mode=0o700)
    logical = tmp_path / "logical.lock"
    source = namespace / "source"
    source.touch(mode=0o600)
    os.link(source, _tmpfs_lock_path(root, logical))

    with pytest.raises(ValueError, match="single-link"):
        with tmpfs_file_lock(logical):
            pass


def test_tmpfs_file_lock_rejects_named_inode_replacement_after_acquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "logical.lock"
    with tmpfs_file_lock(logical):
        pass

    lock_path = _tmpfs_lock_path(root, logical)
    real_flock = fcntl.flock
    replaced = False

    def replacing_flock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if operation & fcntl.LOCK_EX and not replaced:
            replacement = lock_path.with_name("replacement")
            replacement.touch(mode=0o600)
            os.replace(replacement, lock_path)
            replaced = True

    monkeypatch.setattr(lock_module.fcntl, "flock", replacing_flock)

    with pytest.raises(ValueError, match="changed during acquisition"):
        with tmpfs_file_lock(logical):
            pass


def test_tmpfs_file_lock_keeps_and_reuses_inode_after_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "logical.lock"
    lock_path = _tmpfs_lock_path(root, logical)

    with tmpfs_file_lock(logical):
        first = lock_path.stat()
    assert lock_path.exists()
    with tmpfs_file_lock(logical):
        second = lock_path.stat()

    assert (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def test_tmpfs_file_lock_recreates_namespace_after_it_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    logical = tmp_path / "logical.lock"
    namespace = _tmpfs_namespace(root)
    lock_path = _tmpfs_lock_path(root, logical)

    with tmpfs_file_lock(logical):
        pass
    lock_path.unlink()
    namespace.rmdir()

    with tmpfs_file_lock(logical):
        assert lock_path.is_file()
        assert stat.S_IMODE(namespace.stat().st_mode) == 0o700


def test_tmpfs_file_lock_times_out_when_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _use_fake_tmpfs(tmp_path, monkeypatch)
    ctx = get_context("fork")
    logical = tmp_path / "logical.lock"
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_tmpfs_lock_until_released,
        args=(str(logical), str(root), ready, release),
    )
    process.start()

    try:
        assert ready.wait(5), "child process never acquired the tmpfs lock"
        with pytest.raises(TimeoutError, match="Timed out acquiring lock"):
            with tmpfs_file_lock(logical, timeout_seconds=0.05):
                pass
    finally:
        release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_tmpfs_file_lock_has_no_disk_fallback_when_root_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root = tmp_path / "missing-shm"
    logical_parent = tmp_path / "disk-state"
    logical = logical_parent / "resource.lock"
    monkeypatch.setattr(lock_module, "_TMPFS_ROOT", missing_root)

    with pytest.raises(FileNotFoundError):
        with tmpfs_file_lock(logical):
            pass

    assert not logical_parent.exists()
