from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from orca_auto.core.config.files import validated_runs_root_text
from orca_auto.core.paths import SMOKE_RESULTS_DIRNAME
from orca_auto.core.queue.engine.input_snapshot import read_stable_regular_file
from orca_auto.core.utils.lock import file_lock, file_lock_at
from orca_auto.core.utils.persistence import atomic_write_json, now_utc_iso

from .catalog import SmokeScenario

SMOKE_SCHEMA_VERSION = 1
SMOKE_OWNER_FILENAME = ".owner.json"
SMOKE_BATCH_FILENAME = "batch.json"
SMOKE_CASE_FILENAME = "case.json"
SMOKE_INDEX_FILENAME = "index.json"
MAX_SMOKE_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_UNTRACKED_DIGEST_BYTES = 256 * 1024 * 1024
MAX_RUNTIME_SCAN_ENTRIES = 100_000
MAX_RUNTIME_SCAN_DEPTH = 64
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_BATCH_PROFILE_CODES = {
    "fake": "f",
    "real-orca": "ro",
    "real-xtb": "rx",
    "all": "a",
}
_SMOKE_INIT_LOCK_FILENAME = ".orca_auto_smoke.init.lock"


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _AtomicJsonSnapshot:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True)
class _AtomicJsonBackup:
    name: str
    snapshot: _AtomicJsonSnapshot


@dataclass(frozen=True)
class _ScannedRegularFile:
    relative_path: str
    name: str
    payload: bytes | None


@dataclass
class _PinnedBatchDirectory:
    smoke_root: Path
    batch_dir: Path
    manifest: dict[str, Any]
    smoke_root_fd: int
    batches_fd: int
    batch_fd: int
    cases_fd: int
    smoke_identity: _DirectoryIdentity
    batches_identity: _DirectoryIdentity
    batch_identity: _DirectoryIdentity
    cases_identity: _DirectoryIdentity

    @property
    def smoke_access_path(self) -> Path:
        return Path("/proc") / "self" / "fd" / str(self.smoke_root_fd)

    @property
    def batch_access_path(self) -> Path:
        # Scenario subprocesses do not inherit this descriptor. The stable
        # same-UID procfs view of the runner's open fd keeps their basetemp
        # rooted in the pinned batch inode for the lifetime of the suite.
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.batch_fd)

    def assert_namespace_identity(self) -> None:
        _assert_path_directory_identity(
            self.smoke_root,
            self.smoke_identity,
            label="Smoke results root",
        )
        _assert_named_directory_identity(
            self.smoke_root_fd,
            "batches",
            self.batches_identity,
            label="Batches root",
        )
        _assert_named_directory_identity(
            self.batches_fd,
            self.batch_dir.name,
            self.batch_identity,
            label="Smoke batch directory",
        )
        _assert_named_directory_identity(
            self.batch_fd,
            "cases",
            self.cases_identity,
            label="Smoke cases directory",
        )

    def close(self) -> None:
        for descriptor in (
            self.cases_fd,
            self.batch_fd,
            self.batches_fd,
            self.smoke_root_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass


@dataclass
class _PinnedCaseDirectory:
    case_id: str
    batch_fd: int
    cases_fd: int
    case_fd: int
    runtime_fd: int
    harness_fd: int
    pytest_fd: int
    cases_identity: _DirectoryIdentity
    case_identity: _DirectoryIdentity
    runtime_identity: _DirectoryIdentity
    harness_identity: _DirectoryIdentity
    pytest_identity: _DirectoryIdentity

    @property
    def case_access_path(self) -> Path:
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.case_fd)

    @property
    def runtime_access_path(self) -> Path:
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.runtime_fd)

    @property
    def harness_access_path(self) -> Path:
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.harness_fd)

    @property
    def pytest_access_path(self) -> Path:
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.pytest_fd)

    def assert_namespace_identity(self) -> None:
        _assert_named_directory_identity(
            self.batch_fd,
            "cases",
            self.cases_identity,
            label="Smoke cases directory",
        )
        _assert_named_directory_identity(
            self.cases_fd,
            self.case_id,
            self.case_identity,
            label="Smoke case directory",
        )
        _assert_named_directory_identity(
            self.case_fd,
            "runtime",
            self.runtime_identity,
            label="Smoke case runtime directory",
        )
        _assert_named_directory_identity(
            self.runtime_fd,
            "_smoke_harness",
            self.harness_identity,
            label="Smoke harness directory",
        )
        _assert_named_directory_identity(
            self.runtime_fd,
            "pytest",
            self.pytest_identity,
            label="Smoke pytest directory",
        )

    def close(self) -> None:
        for descriptor in (
            self.pytest_fd,
            self.harness_fd,
            self.runtime_fd,
            self.case_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _single_link_regular_file(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and details.st_nlink == 1


def _bounded_json_mapping(path: Path) -> dict[str, Any] | None:
    if not _single_link_regular_file(path):
        return None
    try:
        payload = read_stable_regular_file(
            path,
            max_bytes=MAX_SMOKE_MANIFEST_BYTES,
            require_single_link=True,
        )
        raw = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _directory_identity(directory_fd: int) -> _DirectoryIdentity:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("smoke path identity is not a directory")
    return _DirectoryIdentity(details.st_dev, details.st_ino)


def _validate_owned_directory(directory_fd: int, *, label: str) -> _DirectoryIdentity:
    details = os.fstat(directory_fd)
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"{label} must be a directory")
    if details.st_uid != os.geteuid():
        raise ValueError(f"{label} must be owned by the current user")
    if stat.S_IMODE(details.st_mode) & 0o022:
        raise ValueError(f"{label} must not be group- or world-writable")
    return _DirectoryIdentity(details.st_dev, details.st_ino)


def _assert_named_directory_identity(
    parent_fd: int,
    name: str,
    expected: _DirectoryIdentity,
    *,
    label: str,
) -> None:
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"{label} identity changed") from exc
    try:
        if _directory_identity(descriptor) != expected:
            raise ValueError(f"{label} identity changed")
    finally:
        os.close(descriptor)


def _assert_path_directory_identity(
    path: Path,
    expected: _DirectoryIdentity,
    *,
    label: str,
) -> None:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as exc:
        raise ValueError(f"{label} identity changed") from exc
    try:
        if _directory_identity(descriptor) != expected:
            raise ValueError(f"{label} identity changed")
    finally:
        os.close(descriptor)


def _validate_path_component(name: str, *, label: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
        or len(os.fsencode(name)) > 255
    ):
        raise ValueError(f"{label} must be one safe path component")


def _create_owned_directory_at(
    parent_fd: int, name: str, *, label: str
) -> tuple[int, _DirectoryIdentity]:
    _validate_path_component(name, label=label)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        directory_fd = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileExistsError as exc:
        raise ValueError(f"{label} already exists") from exc
    except OSError as exc:
        raise ValueError(f"{label} could not be created safely") from exc
    try:
        identity = _validate_owned_directory(directory_fd, label=label)
        _assert_named_directory_identity(parent_fd, name, identity, label=label)
        return directory_fd, identity
    except BaseException:
        os.close(directory_fd)
        raise


def _create_pinned_case_directory(
    *,
    batch_fd: int,
    cases_fd: int,
    cases_identity: _DirectoryIdentity,
    case_id: str,
) -> _PinnedCaseDirectory:
    _assert_named_directory_identity(
        batch_fd,
        "cases",
        cases_identity,
        label="Smoke cases directory",
    )
    case_fd: int | None = None
    runtime_fd: int | None = None
    harness_fd: int | None = None
    pytest_fd: int | None = None
    try:
        case_fd, case_identity = _create_owned_directory_at(
            cases_fd,
            case_id,
            label="Smoke case directory",
        )
        runtime_fd, runtime_identity = _create_owned_directory_at(
            case_fd,
            "runtime",
            label="Smoke case runtime directory",
        )
        harness_fd, harness_identity = _create_owned_directory_at(
            runtime_fd,
            "_smoke_harness",
            label="Smoke harness directory",
        )
        pytest_fd, pytest_identity = _create_owned_directory_at(
            runtime_fd,
            "pytest",
            label="Smoke pytest directory",
        )
        pinned = _PinnedCaseDirectory(
            case_id=case_id,
            batch_fd=batch_fd,
            cases_fd=cases_fd,
            case_fd=case_fd,
            runtime_fd=runtime_fd,
            harness_fd=harness_fd,
            pytest_fd=pytest_fd,
            cases_identity=cases_identity,
            case_identity=case_identity,
            runtime_identity=runtime_identity,
            harness_identity=harness_identity,
            pytest_identity=pytest_identity,
        )
        pinned.assert_namespace_identity()
        return pinned
    except BaseException:
        for descriptor in (pytest_fd, harness_fd, runtime_fd, case_fd):
            if descriptor is not None:
                os.close(descriptor)
        raise


def _file_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _digest_descriptor(descriptor: int, *, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("smoke manifest exceeds the bounded publication limit")
        digest.update(chunk)
    return digest.hexdigest(), total


def _verify_atomic_json_entry(
    directory_fd: int,
    name: str,
    snapshot: _AtomicJsonSnapshot,
    *,
    expected_nlinks: frozenset[int],
) -> None:
    try:
        descriptor = os.open(name, _file_open_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError("smoke manifest publication identity changed") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in expected_nlinks
            or before.st_dev != snapshot.device
            or before.st_ino != snapshot.inode
            or before.st_size != snapshot.size_bytes
            or before.st_mtime_ns != snapshot.modified_ns
        ):
            raise ValueError("smoke manifest publication identity changed")
        digest, size_bytes = _digest_descriptor(
            descriptor,
            max_bytes=MAX_SMOKE_MANIFEST_BYTES,
        )
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            size_bytes != snapshot.size_bytes
            or digest != snapshot.sha256
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (snapshot.device, snapshot.inode, snapshot.size_bytes, snapshot.modified_ns)
            or after.st_nlink not in expected_nlinks
            or (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            != (snapshot.device, snapshot.inode, snapshot.size_bytes, snapshot.modified_ns)
            or named.st_nlink not in expected_nlinks
        ):
            raise ValueError("smoke manifest publication identity changed")
    finally:
        os.close(descriptor)


def _capture_atomic_json_snapshot(
    directory_fd: int,
    name: str,
) -> _AtomicJsonSnapshot | None:
    try:
        opened = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise ValueError("existing smoke manifest is unsafe")
    if opened.st_size > MAX_SMOKE_MANIFEST_BYTES:
        raise ValueError("existing smoke manifest exceeds the backup limit")
    descriptor = os.open(name, _file_open_flags(), dir_fd=directory_fd)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise ValueError("existing smoke manifest changed during backup")
        digest, size_bytes = _digest_descriptor(
            descriptor,
            max_bytes=MAX_SMOKE_MANIFEST_BYTES,
        )
        after = os.fstat(descriptor)
        if (
            size_bytes != details.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)
            or after.st_nlink != 1
        ):
            raise ValueError("existing smoke manifest changed during backup")
        return _AtomicJsonSnapshot(
            device=details.st_dev,
            inode=details.st_ino,
            size_bytes=details.st_size,
            modified_ns=details.st_mtime_ns,
            sha256=digest,
        )
    finally:
        os.close(descriptor)


def _create_atomic_json_backup(
    directory_fd: int,
    target_name: str,
) -> _AtomicJsonBackup | None:
    snapshot = _capture_atomic_json_snapshot(directory_fd, target_name)
    if snapshot is None:
        return None
    backup_name = f".{target_name}.{token_hex(12)}.bak"
    linked = False
    try:
        _verify_atomic_json_entry(
            directory_fd,
            target_name,
            snapshot,
            expected_nlinks=frozenset({1}),
        )
        os.link(
            target_name,
            backup_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        for candidate in (target_name, backup_name):
            _verify_atomic_json_entry(
                directory_fd,
                candidate,
                snapshot,
                expected_nlinks=frozenset({2}),
            )
        os.fsync(directory_fd)
        return _AtomicJsonBackup(name=backup_name, snapshot=snapshot)
    except BaseException:
        if linked:
            try:
                os.unlink(backup_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
        raise


def _atomic_json_entry_matches(
    directory_fd: int,
    name: str,
    snapshot: _AtomicJsonSnapshot,
) -> bool:
    try:
        descriptor = os.open(name, _file_open_flags(), dir_fd=directory_fd)
    except OSError:
        return False
    try:
        details = os.fstat(descriptor)
        return (
            stat.S_ISREG(details.st_mode)
            and details.st_dev == snapshot.device
            and details.st_ino == snapshot.inode
        )
    finally:
        os.close(descriptor)


def _restore_atomic_json_entry(
    directory_fd: int,
    target_name: str,
    backup: _AtomicJsonBackup | None,
) -> None:
    if backup is None:
        try:
            os.unlink(target_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)
        return
    if _atomic_json_entry_matches(directory_fd, target_name, backup.snapshot):
        for candidate in (target_name, backup.name):
            _verify_atomic_json_entry(
                directory_fd,
                candidate,
                backup.snapshot,
                expected_nlinks=frozenset({2}),
            )
        os.unlink(backup.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    else:
        _verify_atomic_json_entry(
            directory_fd,
            backup.name,
            backup.snapshot,
            expected_nlinks=frozenset({1}),
        )
        os.replace(
            backup.name,
            target_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    _verify_atomic_json_entry(
        directory_fd,
        target_name,
        backup.snapshot,
        expected_nlinks=frozenset({1}),
    )


def _discard_atomic_json_backup(
    directory_fd: int,
    backup: _AtomicJsonBackup,
) -> None:
    try:
        _verify_atomic_json_entry(
            directory_fd,
            backup.name,
            backup.snapshot,
            expected_nlinks=frozenset({1, 2}),
        )
        os.unlink(backup.name, dir_fd=directory_fd)
    except (OSError, ValueError):
        # Publication/rollback has already reached its verified state. Hidden
        # cleanup residue is safer than turning that committed state into a
        # false failure or masking the original publication error.
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # The new target was already verified and durably published. A hidden
        # backup link can safely survive until ordinary batch cleanup.
        pass


def _atomic_write_json_at(
    directory_fd: int,
    name: str,
    payload: Mapping[str, Any],
    *,
    mode: int = 0o600,
) -> None:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_SMOKE_MANIFEST_BYTES:
        raise ValueError("smoke manifest exceeds the bounded publication limit")
    temporary = f".{name}.{token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    backup: _AtomicJsonBackup | None = None
    preserve_backup = False
    try:
        descriptor = os.open(temporary, flags, mode, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ValueError("smoke manifest staging file is unsafe")
            expected = _AtomicJsonSnapshot(
                device=details.st_dev,
                inode=details.st_ino,
                size_bytes=details.st_size,
                modified_ns=details.st_mtime_ns,
                sha256=hashlib.sha256(encoded).hexdigest(),
            )
        _verify_atomic_json_entry(
            directory_fd,
            temporary,
            expected,
            expected_nlinks=frozenset({1}),
        )
        backup = _create_atomic_json_backup(directory_fd, name)
        try:
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            _verify_atomic_json_entry(
                directory_fd,
                name,
                expected,
                expected_nlinks=frozenset({1}),
            )
            os.fsync(directory_fd)
            _verify_atomic_json_entry(
                directory_fd,
                name,
                expected,
                expected_nlinks=frozenset({1}),
            )
        except BaseException:
            try:
                _restore_atomic_json_entry(directory_fd, name, backup)
                backup = None
            except BaseException as restore_error:
                preserve_backup = True
                raise OSError("previous smoke manifest could not be restored") from restore_error
            raise
    finally:
        if backup is not None and not preserve_backup:
            _discard_atomic_json_backup(directory_fd, backup)
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            # A verified target or verified rollback is already authoritative.
            # Best-effort staging cleanup must never reverse that outcome.
            pass


def _bounded_json_mapping_at(directory_fd: int, name: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > MAX_SMOKE_MANIFEST_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or identity != (
            named.st_dev,
            named.st_ino,
            named.st_size,
            named.st_mtime_ns,
        ):
            return None
        raw = json.loads(b"".join(chunks).decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)
    return raw if isinstance(raw, dict) else None


def _assert_directory_no_symlink(path: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if path.is_symlink() or resolved.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"{label} must be a directory: {resolved}")
    return resolved


def prepare_smoke_root(runs_root: str | Path) -> Path:
    validated_root = Path(validated_runs_root_text(str(runs_root))).expanduser().resolve()
    validated_root.mkdir(parents=True, exist_ok=True)
    smoke_root = validated_root / SMOKE_RESULTS_DIRNAME
    owner_payload = {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "kind": "orca_auto_smoke_results",
    }
    root_fd = os.open(validated_root, _directory_open_flags())
    root_identity = _directory_identity(root_fd)
    smoke_fd: int | None = None
    try:
        with file_lock_at(
            root_fd,
            _SMOKE_INIT_LOCK_FILENAME,
            display_path=validated_root / _SMOKE_INIT_LOCK_FILENAME,
        ):
            created = False
            try:
                os.mkdir(SMOKE_RESULTS_DIRNAME, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
                created = True
            except FileExistsError:
                pass
            try:
                smoke_entry = os.stat(
                    SMOKE_RESULTS_DIRNAME,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(smoke_entry.st_mode):
                    raise ValueError(f"Smoke results root must not be a symlink: {smoke_root}")
                smoke_fd = os.open(
                    SMOKE_RESULTS_DIRNAME,
                    _directory_open_flags(),
                    dir_fd=root_fd,
                )
            except ValueError:
                raise
            except OSError as exc:
                raise ValueError(
                    f"Smoke results root is unavailable or unsafe: {smoke_root}"
                ) from exc
            smoke_identity = _validate_owned_directory(smoke_fd, label="Smoke results root")
            if created:
                _atomic_write_json_at(
                    smoke_fd,
                    SMOKE_OWNER_FILENAME,
                    owner_payload,
                )
            owner = _bounded_json_mapping_at(smoke_fd, SMOKE_OWNER_FILENAME)
            if owner != owner_payload:
                raise ValueError(f"Refusing to adopt unowned smoke results directory: {smoke_root}")
            _assert_named_directory_identity(
                root_fd,
                SMOKE_RESULTS_DIRNAME,
                smoke_identity,
                label="Smoke results root",
            )
            _assert_path_directory_identity(
                validated_root,
                root_identity,
                label="Runs root",
            )
    finally:
        if smoke_fd is not None:
            os.close(smoke_fd)
        os.close(root_fd)
    return smoke_root


def _git_text(repo_root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _git_diff_digest(repo_root: Path) -> str:
    digest = hashlib.sha256()
    process = subprocess.Popen(
        ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = process.stdout
    assert stdout is not None
    for chunk in iter(lambda: stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    return_code = process.wait(timeout=30)
    return digest.hexdigest() if return_code == 0 else "unavailable"


def _untracked_source_identity(repo_root: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "untracked_digest": "unavailable",
            "untracked_file_count": 0,
            "untracked_digest_complete": False,
        }
    if result.returncode != 0:
        return {
            "untracked_digest": "unavailable",
            "untracked_file_count": 0,
            "untracked_digest_complete": False,
        }

    digest = hashlib.sha256(b"orca_auto_untracked_source_v2\0")
    total_bytes = 0
    file_count = 0
    complete = True
    content_budget_exhausted = False
    for raw_relative in (item for item in result.stdout.split(b"\0") if item):
        relative = Path(os.fsdecode(raw_relative))
        candidate = repo_root / relative
        file_count += 1
        digest.update(b"path\0" + raw_relative + b"\0")
        try:
            details = candidate.lstat()
            file_type = stat.S_IFMT(details.st_mode)
            file_mode = stat.S_IMODE(details.st_mode)
            digest.update(
                f"lstat-type\0{file_type:o}\0mode\0{file_mode:o}\0size\0{details.st_size}\0".encode()
            )
            if stat.S_ISLNK(details.st_mode):
                digest.update(b"symlink\0" + os.fsencode(os.readlink(candidate)) + b"\0")
                after_link = candidate.lstat()
                if (
                    after_link.st_dev != details.st_dev
                    or after_link.st_ino != details.st_ino
                    or after_link.st_size != details.st_size
                    or stat.S_IFMT(after_link.st_mode) != file_type
                    or stat.S_IMODE(after_link.st_mode) != file_mode
                ):
                    complete = False
                    digest.update(b"symlink-changed-while-read\0")
                continue
            if not stat.S_ISREG(details.st_mode):
                complete = False
                digest.update(b"nonregular\0")
                continue
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            file_descriptor = os.open(candidate, flags)
            try:
                opened = os.fstat(file_descriptor)
                opened_file_type = stat.S_IFMT(opened.st_mode)
                opened_mode = stat.S_IMODE(opened.st_mode)
                digest.update(
                    f"opened-type\0{opened_file_type:o}\0mode\0{opened_mode:o}\0"
                    f"size\0{opened.st_size}\0".encode()
                )
                if not stat.S_ISREG(opened.st_mode):
                    complete = False
                    digest.update(b"changed-before-open\0")
                    continue
                if (
                    opened.st_dev != details.st_dev
                    or opened.st_ino != details.st_ino
                    or opened.st_size != details.st_size
                    or opened_file_type != file_type
                    or opened_mode != file_mode
                ):
                    complete = False
                    digest.update(b"changed-before-open\0")
                if (
                    content_budget_exhausted
                    or total_bytes + opened.st_size > MAX_UNTRACKED_DIGEST_BYTES
                ):
                    complete = False
                    content_budget_exhausted = True
                    digest.update(b"content-omitted-after-limit\0")
                    continue
                digest.update(b"content\0")
                bytes_read = 0
                while bytes_read < opened.st_size:
                    chunk = os.read(
                        file_descriptor,
                        min(1024 * 1024, opened.st_size - bytes_read),
                    )
                    if not chunk:
                        break
                    digest.update(chunk)
                    bytes_read += len(chunk)
                digest.update(b"\0content-end\0")
                after = os.fstat(file_descriptor)
                if (
                    bytes_read != opened.st_size
                    or after.st_dev != opened.st_dev
                    or after.st_ino != opened.st_ino
                    or after.st_size != opened.st_size
                    or stat.S_IFMT(after.st_mode) != opened_file_type
                    or stat.S_IMODE(after.st_mode) != opened_mode
                ):
                    complete = False
                    digest.update(b"changed-while-read\0")
                total_bytes += bytes_read
            finally:
                os.close(file_descriptor)
        except OSError:
            complete = False
            digest.update(b"unavailable\0")
    return {
        "untracked_digest": digest.hexdigest(),
        "untracked_file_count": file_count,
        "untracked_digest_complete": complete,
    }


def source_identity(repo_root: Path) -> dict[str, Any]:
    status = _git_text(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    identity = {
        "git_head": _git_text(repo_root, "rev-parse", "HEAD") or "unknown",
        "git_short": _git_text(repo_root, "rev-parse", "--short=12", "HEAD") or "unknown",
        "dirty": bool(status),
        "working_tree_digest": _git_diff_digest(repo_root),
        "status_digest": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }
    identity.update(_untracked_source_identity(repo_root))
    return identity


def _create_pinned_batch_directory(
    smoke_root: Path,
    *,
    profile: str,
    repo_root: Path,
) -> _PinnedBatchDirectory:
    smoke_root = smoke_root.expanduser()
    smoke_fd = os.open(smoke_root, _directory_open_flags())
    batches_fd: int | None = None
    batch_fd: int | None = None
    cases_fd: int | None = None
    try:
        smoke_identity = _validate_owned_directory(smoke_fd, label="Smoke results root")
        if _bounded_json_mapping_at(smoke_fd, SMOKE_OWNER_FILENAME) != {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "kind": "orca_auto_smoke_results",
        }:
            raise ValueError(f"Refusing to use unowned smoke results directory: {smoke_root}")
        try:
            os.mkdir("batches", mode=0o700, dir_fd=smoke_fd)
            os.fsync(smoke_fd)
        except FileExistsError:
            pass
        batches_fd = os.open("batches", _directory_open_flags(), dir_fd=smoke_fd)
        batches_identity = _validate_owned_directory(batches_fd, label="Batches root")
        identity = source_identity(repo_root)
        _assert_path_directory_identity(
            smoke_root,
            smoke_identity,
            label="Smoke results root",
        )
        _assert_named_directory_identity(
            smoke_fd,
            "batches",
            batches_identity,
            label="Batches root",
        )
    except BaseException:
        if batches_fd is not None:
            os.close(batches_fd)
        os.close(smoke_fd)
        raise
    assert batches_fd is not None
    try:
        profile_code = _BATCH_PROFILE_CODES[profile]
    except KeyError as exc:
        os.close(batches_fd)
        os.close(smoke_fd)
        raise ValueError(f"unsupported smoke profile: {profile}") from exc
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # Source identity remains complete in batch.json; the directory name stays
    # short enough for Windows WSL UNC review paths.
    batch_id = f"{stamp}-{profile_code}-{token_hex(3)}"
    batch_dir = smoke_root / "batches" / batch_id
    try:
        os.mkdir(batch_id, mode=0o700, dir_fd=batches_fd)
        os.fsync(batches_fd)
        batch_fd = os.open(batch_id, _directory_open_flags(), dir_fd=batches_fd)
        batch_identity = _validate_owned_directory(batch_fd, label="Smoke batch directory")
    except BaseException:
        os.close(batches_fd)
        os.close(smoke_fd)
        raise
    assert batch_fd is not None
    try:
        cases_fd, cases_identity = _create_owned_directory_at(
            batch_fd,
            "cases",
            label="Smoke cases directory",
        )
    except BaseException:
        os.close(batch_fd)
        os.close(batches_fd)
        os.close(smoke_fd)
        raise
    assert cases_fd is not None
    manifest: dict[str, Any] = {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "batch_id": batch_id,
        "profile": profile,
        "status": "running",
        "review_status": "unreviewed",
        "started_at": now_utc_iso(),
        "finished_at": None,
        "source": identity,
        "cases": [],
    }
    try:
        _atomic_write_json_at(batch_fd, SMOKE_BATCH_FILENAME, manifest)
        pinned = _PinnedBatchDirectory(
            smoke_root=smoke_root,
            batch_dir=batch_dir,
            manifest=manifest,
            smoke_root_fd=smoke_fd,
            batches_fd=batches_fd,
            batch_fd=batch_fd,
            cases_fd=cases_fd,
            smoke_identity=smoke_identity,
            batches_identity=batches_identity,
            batch_identity=batch_identity,
            cases_identity=cases_identity,
        )
        pinned.assert_namespace_identity()
        return pinned
    except BaseException:
        os.close(cases_fd)
        os.close(batch_fd)
        os.close(batches_fd)
        os.close(smoke_fd)
        raise


def create_batch_directory(
    smoke_root: Path,
    *,
    profile: str,
    repo_root: Path,
) -> tuple[Path, dict[str, Any]]:
    pinned = _create_pinned_batch_directory(
        smoke_root,
        profile=profile,
        repo_root=repo_root,
    )
    try:
        return pinned.batch_dir, pinned.manifest
    finally:
        pinned.close()


def write_batch_manifest(
    batch_dir: Path,
    manifest: Mapping[str, Any],
    *,
    directory_fd: int | None = None,
) -> Path:
    path = batch_dir / SMOKE_BATCH_FILENAME
    owned_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = os.open(batch_dir, _directory_open_flags())
    try:
        _atomic_write_json_at(directory_fd, SMOKE_BATCH_FILENAME, manifest)
    finally:
        if owned_fd:
            os.close(directory_fd)
    return path


def write_case_manifest(
    case_dir: Path,
    manifest: Mapping[str, Any],
    *,
    directory_fd: int | None = None,
) -> Path:
    path = case_dir / SMOKE_CASE_FILENAME
    owned_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = os.open(case_dir, _directory_open_flags())
    try:
        _atomic_write_json_at(directory_fd, SMOKE_CASE_FILENAME, manifest)
    finally:
        if owned_fd:
            os.close(directory_fd)
    return path


def _read_scanned_regular_file(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> bytes | None:
    if expected.st_size > MAX_SMOKE_MANIFEST_BYTES:
        return None
    descriptor = os.open(name, _file_open_flags(), dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (expected.st_dev, expected.st_ino, expected.st_size, expected.st_mtime_ns)
        ):
            raise ValueError("smoke runtime file changed before it was read")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (
            identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or identity != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
            or after.st_nlink != 1
            or named.st_nlink != 1
        ):
            raise ValueError("smoke runtime file changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _scan_regular_files_at(
    directory_fd: int,
    *,
    payload_names: frozenset[str],
) -> list[_ScannedRegularFile]:
    results: list[_ScannedRegularFile] = []
    entries_seen = 0

    def walk(current_fd: int, relative_parts: tuple[str, ...], depth: int) -> None:
        nonlocal entries_seen
        if depth > MAX_RUNTIME_SCAN_DEPTH:
            raise ValueError("smoke runtime scan exceeded the directory depth limit")
        current_identity = _directory_identity(current_fd)
        with os.scandir(current_fd) as iterator:
            names = [entry.name for entry in iterator]
        entries_seen += len(names)
        if entries_seen > MAX_RUNTIME_SCAN_ENTRIES:
            raise ValueError("smoke runtime scan exceeded the entry limit")
        for name in sorted(names):
            try:
                opened = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("smoke runtime entry became unavailable") from exc
            if stat.S_ISLNK(opened.st_mode):
                continue
            if stat.S_ISDIR(opened.st_mode):
                try:
                    child_fd = os.open(name, _directory_open_flags(), dir_fd=current_fd)
                except OSError as exc:
                    raise ValueError("smoke runtime directory could not be opened safely") from exc
                try:
                    child_identity = _directory_identity(child_fd)
                    if child_identity != _DirectoryIdentity(opened.st_dev, opened.st_ino):
                        raise ValueError("smoke runtime directory identity changed")
                    walk(child_fd, (*relative_parts, name), depth + 1)
                    _assert_named_directory_identity(
                        current_fd,
                        name,
                        child_identity,
                        label="Smoke runtime directory",
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                continue
            payload = (
                _read_scanned_regular_file(current_fd, name, opened)
                if name in payload_names
                else None
            )
            if name not in payload_names:
                descriptor = os.open(name, _file_open_flags(), dir_fd=current_fd)
                try:
                    verified = os.fstat(descriptor)
                    named = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                    expected_identity = (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_size,
                        opened.st_mtime_ns,
                    )
                    if (
                        not stat.S_ISREG(verified.st_mode)
                        or verified.st_nlink != 1
                        or named.st_nlink != 1
                        or expected_identity
                        != (
                            verified.st_dev,
                            verified.st_ino,
                            verified.st_size,
                            verified.st_mtime_ns,
                        )
                        or expected_identity
                        != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
                    ):
                        raise ValueError("smoke runtime file identity changed")
                finally:
                    os.close(descriptor)
            results.append(
                _ScannedRegularFile(
                    relative_path="/".join((*relative_parts, name)),
                    name=name,
                    payload=payload,
                )
            )
        if _directory_identity(current_fd) != current_identity:
            raise ValueError("smoke runtime directory identity changed")

    walk(directory_fd, (), 0)
    return results


def _scan_runtime(
    runtime_dir: Path,
    *,
    payload_names: frozenset[str],
    directory_fd: int | None = None,
) -> list[_ScannedRegularFile]:
    owned_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = os.open(runtime_dir, _directory_open_flags())
    else:
        directory_fd = os.dup(directory_fd)
        owned_fd = True
    try:
        return _scan_regular_files_at(directory_fd, payload_names=payload_names)
    finally:
        if owned_fd:
            os.close(directory_fd)


def _artifact_counts_from_scan(
    files: Iterable[_ScannedRegularFile],
    required_names: Iterable[str],
) -> dict[str, int]:
    expected = tuple(dict.fromkeys(required_names))
    counts = {name: 0 for name in expected}
    for record in files:
        if record.name in counts:
            counts[record.name] += 1
    return counts


def artifact_counts(
    runtime_dir: Path,
    required_names: Iterable[str],
    *,
    directory_fd: int | None = None,
) -> dict[str, int]:
    files = _scan_runtime(runtime_dir, payload_names=frozenset(), directory_fd=directory_fd)
    return _artifact_counts_from_scan(files, required_names)


def _normalized_state_status(payload: Mapping[str, Any]) -> tuple[str, str]:
    raw_status = payload.get("status")
    status = ""
    reason = ""
    if isinstance(raw_status, Mapping):
        status = str(raw_status.get("state") or "").strip().lower()
        reason = str(raw_status.get("reason") or "").strip()
    else:
        status = str(raw_status or "").strip().lower()

    final_result = payload.get("final_result")
    if isinstance(final_result, Mapping):
        status = str(final_result.get("status") or status).strip().lower()
        reason = str(final_result.get("reason") or reason).strip()
    return status, reason


def _terminal_records_from_scan(
    files: Iterable[_ScannedRegularFile],
    *,
    surface: str,
) -> list[dict[str, str]]:
    target_name = "workflow.json" if surface == "workflow" else "job_state.json"
    records: list[dict[str, str]] = []
    for record in files:
        if record.name != target_name:
            continue
        try:
            raw = json.loads((record.payload or b"").decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raw = None
        payload = raw if isinstance(raw, dict) else None
        if payload is None:
            records.append(
                {
                    "path": record.relative_path,
                    "status": "invalid",
                    "reason": "state_record_invalid",
                }
            )
            continue
        status, reason = _normalized_state_status(payload)
        if surface == "workflow":
            status = str(payload.get("status") or status).strip().lower()
            metadata = payload.get("metadata")
            if isinstance(metadata, Mapping):
                workflow_error = metadata.get("workflow_error")
                if isinstance(workflow_error, Mapping):
                    reason = str(workflow_error.get("reason") or reason).strip()
        if not status:
            status = "missing"
            reason = reason or "state_status_missing"
        records.append({"path": record.relative_path, "status": status, "reason": reason})
    return records


def _observe_terminal_from_scan(
    files: Iterable[_ScannedRegularFile],
    *,
    surface: str,
) -> dict[str, Any]:
    records = _terminal_records_from_scan(files, surface=surface)
    statuses = {record["status"] for record in records}
    if not statuses:
        aggregate_status = "missing"
    elif len(statuses) == 1:
        aggregate_status = statuses.pop()
    else:
        aggregate_status = "mixed"
    reasons = sorted({record["reason"] for record in records if record["reason"]})
    return {
        "status": aggregate_status,
        "reasons": reasons,
        "records": records,
    }


def observe_terminal(
    runtime_dir: Path,
    *,
    surface: str,
    directory_fd: int | None = None,
) -> dict[str, Any]:
    target_name = "workflow.json" if surface == "workflow" else "job_state.json"
    files = _scan_runtime(
        runtime_dir,
        payload_names=frozenset({target_name}),
        directory_fd=directory_fd,
    )
    return _observe_terminal_from_scan(files, surface=surface)


def build_case_manifest(
    *,
    scenario: SmokeScenario,
    batch_dir: Path,
    case_dir: Path,
    runtime_dir: Path,
    pytest_result: Mapping[str, Any],
    started_at: str,
    finished_at: str,
    runtime_fd: int | None = None,
) -> dict[str, Any]:
    target_name = "workflow.json" if scenario.surface == "workflow" else "job_state.json"
    files = _scan_runtime(
        runtime_dir,
        payload_names=frozenset({target_name}),
        directory_fd=runtime_fd,
    )
    observed = _observe_terminal_from_scan(files, surface=scenario.surface)
    counts = _artifact_counts_from_scan(files, scenario.required_artifacts)
    missing_artifacts = sorted(name for name, count in counts.items() if count < 1)
    pytest_passed = bool(pytest_result.get("passed"))
    skipped = int(pytest_result.get("skipped") or 0)
    expected_matches = (
        observed["status"] in TERMINAL_STATUSES and observed["status"] == scenario.expected_status
    )
    verdict = "passed"
    failure_reasons: list[str] = []
    if skipped:
        verdict = "not_run"
        failure_reasons.append("pytest_scenario_skipped")
    else:
        if not pytest_passed:
            verdict = "failed"
            failure_reasons.append("pytest_scenario_failed")
        if not expected_matches:
            verdict = "failed"
            failure_reasons.append("terminal_status_mismatch")
        if missing_artifacts:
            verdict = "failed"
            failure_reasons.append("required_artifacts_missing")

    return {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "case_id": scenario.scenario_id,
        "surface": scenario.surface,
        "scenario": scenario.description,
        "profile": scenario.profile,
        "case_dir": str(case_dir.relative_to(batch_dir)),
        "runtime_dir": str(runtime_dir.relative_to(case_dir)),
        "started_at": started_at,
        "finished_at": finished_at,
        "expected_terminal": {"status": scenario.expected_status},
        "observed_terminal": observed,
        "verdict": verdict,
        "failure_reasons": failure_reasons,
        "pytest": dict(pytest_result),
        "required_artifacts": counts,
        "missing_artifacts": missing_artifacts,
        "logs": {
            "stdout": "runtime/_smoke_harness/harness.stdout.log",
            "stderr": "runtime/_smoke_harness/harness.stderr.log",
            "junit": "runtime/_smoke_harness/pytest.xml",
        },
    }


def rebuild_smoke_index(
    smoke_root: Path,
    *,
    smoke_root_fd: int | None = None,
    batches_fd: int | None = None,
) -> Path:
    batches_root = (
        Path("/proc") / "self" / "fd" / str(batches_fd)
        if batches_fd is not None
        else smoke_root / "batches"
    )
    rows: list[dict[str, Any]] = []
    lock_context = (
        file_lock_at(
            smoke_root_fd,
            "index.lock",
            display_path=smoke_root / "index.lock",
        )
        if smoke_root_fd is not None
        else file_lock(smoke_root / "index.lock")
    )
    with lock_context:
        if batches_fd is not None or (batches_root.is_dir() and not batches_root.is_symlink()):
            for candidate in batches_root.iterdir():
                if not candidate.is_dir() or candidate.is_symlink():
                    continue
                manifest = _bounded_json_mapping(candidate / SMOKE_BATCH_FILENAME)
                if manifest is None:
                    continue
                rows.append(
                    {
                        "batch_id": str(manifest.get("batch_id") or candidate.name),
                        "profile": str(manifest.get("profile") or ""),
                        "status": str(manifest.get("status") or "unknown"),
                        "review_status": str(manifest.get("review_status") or "unreviewed"),
                        "started_at": manifest.get("started_at"),
                        "finished_at": manifest.get("finished_at"),
                        "path": f"batches/{candidate.name}",
                    }
                )
        rows.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
        path = smoke_root / SMOKE_INDEX_FILENAME
        payload = {
            "schema_version": SMOKE_SCHEMA_VERSION,
            "generated_at": now_utc_iso(),
            "batches": rows,
        }
        if smoke_root_fd is None:
            atomic_write_json(path, payload, ensure_ascii=False)
        else:
            _atomic_write_json_at(smoke_root_fd, SMOKE_INDEX_FILENAME, payload)
    return path


__all__ = [
    "SMOKE_BATCH_FILENAME",
    "SMOKE_CASE_FILENAME",
    "SMOKE_SCHEMA_VERSION",
    "artifact_counts",
    "build_case_manifest",
    "create_batch_directory",
    "observe_terminal",
    "prepare_smoke_root",
    "rebuild_smoke_index",
    "source_identity",
    "write_batch_manifest",
    "write_case_manifest",
]
