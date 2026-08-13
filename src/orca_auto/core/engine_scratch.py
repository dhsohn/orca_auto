from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import (
    RUN_REPORT_HTML_FILE,
    RUN_REPORT_JSON_FILE,
    RUN_STATE_FILE,
    SI_BLOCK_MD_FILE,
)
from orca_auto.core.engine_process import require_confined_regular_file
from orca_auto.core.queue.engine.input_snapshot import MAX_INPUT_SNAPSHOT_BYTES
from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.lock import file_lock_at
from orca_auto.core.utils.process_tracking import RUN_LOCK_FILE_NAME

SCRATCH_MANIFEST_FILE_NAME = ".orca_auto_scratch.json"
SCRATCH_RUNTIME_HOME_DIR_NAME = ".orca_auto_scratch_runtime_home"
SCRATCH_WORKSPACE_PREFIX = "attempt-"
_SCRATCH_ROOT_PARENT = Path("/dev/shm")
_SCRATCH_ROOT_LOCK_FILE_NAME = ".orca_auto_scratch.lock"
_PUBLICATION_JOURNAL_FILE_NAME = ".orca_auto_scratch_publication.json"
_PUBLICATION_TEMP_PREFIX = ".orca_auto_publish."
_PUBLICATION_BACKUP_PREFIX = ".orca_auto_backup."
_PUBLICATION_META_TEMP_PREFIX = ".orca_auto_publish_meta."
_STAGING_TEMP_PREFIX = ".orca_auto_stage."
_PUBLICATION_TEMP_NAME_RE = re.compile(r"^\.orca_auto_publish\.[0-9a-f]{32}\.tmp$")
_PUBLICATION_BACKUP_NAME_RE = re.compile(r"^\.orca_auto_backup\.[0-9a-f]{32}$")
_TRANSIENT_FILE_RE = re.compile(r"(?:^|\.)tmp(?:\.|$)", re.IGNORECASE)
_SCRATCH_CONTROL_FILE_NAMES = frozenset(
    {
        SCRATCH_MANIFEST_FILE_NAME,
        SCRATCH_RUNTIME_HOME_DIR_NAME,
    }
)
_DURABLE_RESERVED_FILE_NAMES = frozenset(
    {
        RUN_STATE_FILE,
        RUN_REPORT_JSON_FILE,
        RUN_REPORT_HTML_FILE,
        SI_BLOCK_MD_FILE,
        RUN_LOCK_FILE_NAME,
        ".job_state.mutation.lock",
        _PUBLICATION_JOURNAL_FILE_NAME,
    }
)
_COPY_CHUNK_BYTES = 1024 * 1024


class EngineScratchError(RuntimeError):
    """Raised when a RAM scratch workspace cannot be used or published safely."""


@dataclass(frozen=True)
class EngineScratchPolicy:
    root: Path
    min_free_bytes: int
    max_task_memory_bytes: int
    dependency_names_from_primary: Callable[[bytes], Sequence[str]] | None = None
    normalize_primary_newline: bool = False
    publish_name: Callable[[str], bool] | None = None

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve(strict=False)
        parent = _SCRATCH_ROOT_PARENT.resolve()
        if root == parent or not root.is_relative_to(parent):
            raise ValueError("Engine scratch root must be a dedicated directory below /dev/shm")
        if self.min_free_bytes < 1:
            raise ValueError("Engine scratch minimum free bytes must be positive")
        if self.max_task_memory_bytes < 1:
            raise ValueError("Engine task memory limit must be positive")
        object.__setattr__(self, "root", root)


@dataclass(frozen=True)
class _StagedInput:
    source: Path
    name: str
    source_sha256: str
    source_size_bytes: int
    staged_sha256: str
    staged_size_bytes: int
    mode: int


@dataclass(frozen=True)
class _CapturedInput:
    name: str
    payload: bytes
    mode: int


@dataclass(frozen=True)
class _PreparedPublication:
    source: Path
    temporary_name: str
    target_name: str
    backup_name: str | None
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ScratchPublication:
    paths: tuple[Path, ...]
    omitted_transient_files: tuple[str, ...]
    omitted_transient_bytes: int


_EXCEPTION_SCRATCH_PROVENANCE_ATTRIBUTE = "_engine_scratch_provenance"


def scratch_publication_provenance(publication: ScratchPublication) -> dict[str, Any]:
    return {
        "used": True,
        "filesystem": "tmpfs",
        "publication_status": "committed",
        "published_files": [path.name for path in publication.paths],
        "omitted_transient_files": list(publication.omitted_transient_files),
        "omitted_transient_bytes": publication.omitted_transient_bytes,
    }


def attach_scratch_provenance_to_exception(
    exc: BaseException,
    publication: ScratchPublication,
) -> None:
    setattr(
        exc,
        _EXCEPTION_SCRATCH_PROVENANCE_ATTRIBUTE,
        scratch_publication_provenance(publication),
    )


def attach_scratch_provenance_mapping_to_exception(
    exc: BaseException,
    provenance: dict[str, Any],
) -> None:
    setattr(exc, _EXCEPTION_SCRATCH_PROVENANCE_ATTRIBUTE, dict(provenance))


def scratch_provenance_from_exception(exc: BaseException) -> dict[str, Any]:
    provenance = getattr(exc, _EXCEPTION_SCRATCH_PROVENANCE_ATTRIBUTE, None)
    return dict(provenance) if isinstance(provenance, dict) else {}


@dataclass
class EngineScratchWorkspace:
    policy: EngineScratchPolicy
    durable_input: Path
    durable_output_dir: Path
    path: Path
    scratch_input: Path
    staged_inputs: dict[str, _StagedInput] = field(default_factory=dict)
    input_dir_fd: int = -1
    input_dir_identity: tuple[int, int] = (-1, -1)
    durable_dir_fd: int = -1
    durable_dir_identity: tuple[int, int] = (-1, -1)
    scratch_root_identity: tuple[int, int] = (-1, -1)
    workspace_identity: tuple[int, int] = (-1, -1)
    workspace_dir_fd: int = -1
    _published: bool = False
    _closed: bool = False

    @classmethod
    def create(
        cls,
        policy: EngineScratchPolicy,
        durable_input: Path,
        *,
        durable_output_dir: Path | None = None,
        expected_durable_dir_identity: tuple[int, int] | None = None,
    ) -> EngineScratchWorkspace:
        durable = require_confined_regular_file(
            durable_input.parent,
            durable_input,
            label="engine durable scratch input",
        )
        output_dir = Path(durable_output_dir or durable.parent).expanduser().resolve()
        if not durable.is_relative_to(output_dir):
            raise EngineScratchError(
                "engine durable scratch input must stay inside its publication directory"
            )
        root = _prepare_scratch_root(policy)
        root_status = root.stat()
        root_identity = (int(root_status.st_dev), int(root_status.st_ino))
        workspace = root / f"{SCRATCH_WORKSPACE_PREFIX}{os.getpid()}-{secrets.token_hex(8)}"
        input_dir_fd = -1
        durable_dir_fd = -1
        workspace_dir_fd = -1
        workspace_identity = (-1, -1)
        root_fd, _observed_root_identity = _open_pinned_directory(
            root,
            label="engine scratch root",
            expected_identity=root_identity,
        )
        try:
            with file_lock_at(
                root_fd,
                _SCRATCH_ROOT_LOCK_FILE_NAME,
                display_path=root / _SCRATCH_ROOT_LOCK_FILE_NAME,
            ):
                _require_directory_path_identity(
                    root,
                    root_fd,
                    root_identity,
                    label="engine scratch root",
                )
                input_dir_fd, input_dir_identity = _open_pinned_directory(
                    durable.parent,
                    label="engine durable scratch input directory",
                )
                try:
                    durable_dir_fd, durable_dir_identity = _open_pinned_directory(
                        output_dir,
                        label="engine durable generation",
                        expected_identity=expected_durable_dir_identity,
                    )
                    _recover_incomplete_publication(
                        durable_dir_fd,
                        output_dir,
                        durable_dir_identity,
                    )
                    _assert_scratch_root_available(root, root_fd)
                    captured_inputs = _capture_input_closure(
                        input_dir_fd,
                        durable.parent,
                        durable.name,
                        dependency_names_from_primary=policy.dependency_names_from_primary,
                    )
                    required_bytes = _input_closure_size_bytes(
                        durable.name,
                        captured_inputs,
                        normalize_primary_newline=policy.normalize_primary_newline,
                    )
                    free_bytes = _filesystem_free_bytes(root_fd)
                    if free_bytes < policy.min_free_bytes + required_bytes:
                        raise EngineScratchError(
                            "engine scratch has insufficient free space: "
                            f"free={free_bytes}, required_inputs={required_bytes}, "
                            f"minimum_free={policy.min_free_bytes}"
                        )
                    available_memory_bytes = _linux_available_memory_bytes()
                    required_memory_bytes = (
                        policy.max_task_memory_bytes + free_bytes + policy.min_free_bytes
                    )
                    if available_memory_bytes < required_memory_bytes:
                        raise EngineScratchError(
                            "engine scratch cannot guarantee RAM headroom without swap: "
                            f"available_memory={available_memory_bytes}, "
                            f"task_memory_limit={policy.max_task_memory_bytes}, "
                            f"scratch_free={free_bytes}, host_reserve={policy.min_free_bytes}"
                        )
                    os.mkdir(workspace.name, mode=0o700, dir_fd=root_fd)
                    workspace_dir_fd, workspace_identity = _open_pinned_directory_at(
                        root_fd,
                        workspace.name,
                        display_path=workspace,
                        label="engine scratch workspace",
                    )
                    _write_workspace_manifest(
                        workspace,
                        output_dir,
                        workspace_dir_fd=workspace_dir_fd,
                    )
                    staged = _stage_input_closure(
                        workspace,
                        durable,
                        workspace_dir_fd=workspace_dir_fd,
                        captured_inputs=captured_inputs,
                        normalize_primary_newline=policy.normalize_primary_newline,
                    )
                    if _filesystem_free_bytes(root_fd) < policy.min_free_bytes:
                        raise EngineScratchError(
                            "engine scratch fell below its minimum free-space reserve while staging"
                        )
                    _require_directory_path_identity(
                        root,
                        root_fd,
                        root_identity,
                        label="engine scratch root",
                    )
                    _require_directory_path_identity(
                        workspace,
                        workspace_dir_fd,
                        workspace_identity,
                        label="engine scratch workspace",
                    )
                    return cls(
                        policy=policy,
                        durable_input=durable,
                        durable_output_dir=output_dir,
                        path=workspace,
                        scratch_input=workspace / durable.name,
                        staged_inputs=staged,
                        input_dir_fd=input_dir_fd,
                        input_dir_identity=input_dir_identity,
                        durable_dir_fd=durable_dir_fd,
                        durable_dir_identity=durable_dir_identity,
                        scratch_root_identity=root_identity,
                        workspace_identity=workspace_identity,
                        workspace_dir_fd=workspace_dir_fd,
                    )
                except BaseException:
                    if input_dir_fd >= 0:
                        os.close(input_dir_fd)
                    if durable_dir_fd >= 0:
                        os.close(durable_dir_fd)
                    if workspace_dir_fd >= 0:
                        os.close(workspace_dir_fd)
                    if workspace_identity != (-1, -1):
                        try:
                            _remove_owned_workspace(
                                root,
                                root_identity,
                                workspace.name,
                                workspace_identity,
                            )
                        except (FileNotFoundError, EngineScratchError, OSError):
                            pass
                    raise
        finally:
            os.close(root_fd)

    def publish(self) -> ScratchPublication:
        if self._published:
            raise EngineScratchError("engine scratch workspace was already published")
        self._require_open()
        _require_directory_path_identity(
            self.path,
            self.workspace_dir_fd,
            self.workspace_identity,
            label="engine scratch workspace",
        )
        _require_directory_path_identity(
            self.durable_input.parent,
            self.input_dir_fd,
            self.input_dir_identity,
            label="engine durable scratch input directory",
        )
        _require_directory_path_identity(
            self.durable_output_dir,
            self.durable_dir_fd,
            self.durable_dir_identity,
            label="engine durable generation",
        )
        _verify_staged_sources_unchanged(
            self.input_dir_fd,
            self.durable_input.parent,
            self.staged_inputs,
        )
        publication = _publish_workspace(
            self.path,
            self.workspace_dir_fd,
            self.workspace_identity,
            self.durable_output_dir,
            self.durable_dir_fd,
            self.durable_dir_identity,
            self.scratch_input.stem,
            self.staged_inputs,
            publish_name=self.policy.publish_name,
        )
        self._published = True
        return publication

    def cleanup(self) -> None:
        if not self._published:
            raise EngineScratchError("Refusing to remove unpublished engine scratch workspace")
        self._require_open()
        root_fd, _observed_identity = _open_pinned_directory(
            self.policy.root,
            label="engine scratch root",
            expected_identity=self.scratch_root_identity,
        )
        try:
            with file_lock_at(
                root_fd,
                _SCRATCH_ROOT_LOCK_FILE_NAME,
                display_path=self.policy.root / _SCRATCH_ROOT_LOCK_FILE_NAME,
            ):
                _remove_owned_workspace_at(
                    root_fd,
                    self.path.name,
                    self.workspace_identity,
                )
        finally:
            os.close(root_fd)
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.input_dir_fd >= 0:
            os.close(self.input_dir_fd)
        if self.durable_dir_fd >= 0:
            os.close(self.durable_dir_fd)
        if self.workspace_dir_fd >= 0:
            os.close(self.workspace_dir_fd)
        self.input_dir_fd = -1
        self.durable_dir_fd = -1
        self.workspace_dir_fd = -1
        self._closed = True

    def _require_open(self) -> None:
        if (
            self._closed
            or self.input_dir_fd < 0
            or self.durable_dir_fd < 0
            or self.workspace_dir_fd < 0
        ):
            raise EngineScratchError("engine scratch workspace is already closed")


def publish_engine_scratch_workspace(
    workspace: EngineScratchWorkspace,
    *,
    logger: logging.Logger,
) -> ScratchPublication:
    """Publish once, clean a committed workspace, and retain unresolved work."""

    try:
        publication = workspace.publish()
    except BaseException:
        workspace.close()
        raise
    try:
        workspace.cleanup()
    except BaseException:  # noqa: BLE001
        logger.exception(
            "Published engine scratch workspace could not be removed; future scratch runs "
            "will remain fail-closed until it is inspected: %s",
            workspace.path,
        )
    finally:
        workspace.close()
    return publication


def is_transient_scratch_file(name: str) -> bool:
    return _TRANSIENT_FILE_RE.search(name) is not None


def _linux_available_memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(":")
            if key == "MemAvailable" and separator:
                amount, unit = value.split()
                if unit != "kB":
                    break
                available = int(amount) * 1024
                if available > 0:
                    return available
                break
    except (OSError, UnicodeError, ValueError):
        pass
    raise EngineScratchError("Cannot determine available host memory for engine scratch")


def _prepare_scratch_root(policy: EngineScratchPolicy) -> Path:
    parent = _SCRATCH_ROOT_PARENT
    if parent.is_symlink() or not parent.is_dir():
        raise EngineScratchError("/dev/shm is unavailable or unsafe")
    resolved_parent = parent.resolve()
    if policy.root.parent != resolved_parent and not policy.root.parent.is_relative_to(
        resolved_parent
    ):
        raise EngineScratchError("engine scratch root escaped /dev/shm")
    current = resolved_parent
    for component in policy.root.relative_to(resolved_parent).parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise EngineScratchError(f"engine scratch path component is unsafe: {current}")
        if info.st_uid != os.getuid():
            raise EngineScratchError(
                f"engine scratch path is not owned by the worker user: {current}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise EngineScratchError(
                f"engine scratch path is accessible by another user: {current}"
            )
    if policy.root.stat().st_dev != resolved_parent.stat().st_dev:
        raise EngineScratchError("engine scratch root is not on the /dev/shm filesystem")
    return policy.root


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= os.O_DIRECTORY
    flags |= os.O_NOFOLLOW
    flags |= os.O_CLOEXEC
    return flags


def _identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _filesystem_free_bytes(directory_fd: int) -> int:
    details = os.fstatvfs(directory_fd)
    return int(details.f_bavail) * int(details.f_frsize)


def _open_pinned_directory_at(
    parent_fd: int,
    name: str,
    *,
    display_path: Path,
    label: str,
) -> tuple[int, tuple[int, int]]:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise EngineScratchError(f"{label} has an unsafe basename: {name!r}")
    descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        path_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or _identity(info) != _identity(path_info):
            raise EngineScratchError(f"{label} identity changed: {display_path}")
        return descriptor, _identity(info)
    except BaseException:
        os.close(descriptor)
        raise


def _open_pinned_directory(
    path: Path,
    *,
    label: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    if path.is_symlink():
        raise EngineScratchError(f"{label} is a symlink: {path}")
    descriptor = os.open(path, _directory_open_flags())
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise EngineScratchError(f"{label} is not a directory: {path}")
        observed = _identity(info)
        if expected_identity is not None and observed != expected_identity:
            raise EngineScratchError(f"{label} identity changed before scratch launch: {path}")
        _require_directory_path_identity(
            path,
            descriptor,
            observed,
            label=label,
        )
        return descriptor, observed
    except BaseException:
        os.close(descriptor)
        raise


def _require_directory_path_identity(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    descriptor_info = os.fstat(descriptor)
    try:
        path_info = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise EngineScratchError(f"{label} pathname disappeared: {path}") from exc
    if (
        not stat.S_ISDIR(path_info.st_mode)
        or _identity(descriptor_info) != expected_identity
        or _identity(path_info) != expected_identity
    ):
        raise EngineScratchError(f"{label} pathname identity changed: {path}")


def _atomic_write_bytes_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise EngineScratchError(f"engine scratch staging name is unsafe: {name!r}")
    temporary_name = f"{_STAGING_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise EngineScratchError(f"Failed to stage engine scratch input: {name}")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _write_workspace_manifest(
    workspace: Path,
    durable_dir: Path,
    *,
    workspace_dir_fd: int,
) -> None:
    boot_id = process_utils.linux_boot_id(proc_root=Path("/proc"))
    owner_ticks = process_utils.current_process_start_ticks()
    if not boot_id or owner_ticks is None:
        raise EngineScratchError("Cannot bind engine scratch ownership to this boot and process")
    payload = {
        "schema_version": 1,
        "owner_pid": os.getpid(),
        "owner_process_start_ticks": owner_ticks,
        "owner_boot_id": boot_id,
        "durable_dir": str(durable_dir.resolve()),
    }
    _atomic_write_bytes_at(
        workspace_dir_fd,
        SCRATCH_MANIFEST_FILE_NAME,
        (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        mode=0o600,
    )


def _manifest_owner_is_live(payload: dict[str, Any]) -> bool:
    owner_pid = payload.get("owner_pid")
    owner_ticks = payload.get("owner_process_start_ticks")
    owner_boot = payload.get("owner_boot_id")
    if type(owner_pid) is not int or owner_pid <= 0:
        return True
    if type(owner_ticks) is not int or owner_ticks <= 0 or not isinstance(owner_boot, str):
        return True
    current_boot = process_utils.linux_boot_id(proc_root=Path("/proc"))
    if not current_boot:
        return True
    if current_boot != owner_boot:
        return False
    if not process_utils.is_process_alive(owner_pid):
        return False
    observed_ticks = process_utils.process_start_ticks(owner_pid)
    if observed_ticks is None:
        return True
    return observed_ticks == owner_ticks


def _assert_scratch_root_available(root: Path, root_fd: int) -> None:
    for name in os.listdir(root_fd):
        if not name.startswith(SCRATCH_WORKSPACE_PREFIX):
            continue
        candidate = root / name
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise EngineScratchError(f"engine scratch root contains an unsafe entry: {candidate}")
        workspace_fd, _workspace_identity = _open_pinned_directory_at(
            root_fd,
            name,
            display_path=candidate,
            label="engine scratch workspace",
        )
        try:
            try:
                raw_payload, _mode = _read_stable_regular_file_at(
                    workspace_fd,
                    SCRATCH_MANIFEST_FILE_NAME,
                    display_path=candidate / SCRATCH_MANIFEST_FILE_NAME,
                    max_bytes=64 * 1024,
                )
                parsed = json.loads(raw_payload.decode("utf-8", errors="strict"))
                payload = parsed if isinstance(parsed, dict) else None
            except (FileNotFoundError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
                payload = None
        finally:
            os.close(workspace_fd)
        if payload is None or payload.get("schema_version") != 1:
            raise EngineScratchError(
                f"engine scratch contains an unresolved workspace without valid ownership: {candidate}"
            )
        if _manifest_owner_is_live(payload):
            raise EngineScratchError(f"Another engine scratch attempt is active: {candidate}")
        raise EngineScratchError(
            "engine scratch contains a stale workspace with uncertain child ownership; "
            f"preserving it for inspection: {candidate}"
        )


def _read_stable_regular_file_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
    max_bytes: int = MAX_INPUT_SNAPSHOT_BYTES,
) -> tuple[bytes, int]:
    flags = os.O_RDONLY
    flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EngineScratchError(f"engine input is not a private regular file: {display_path}")
        if before.st_size > max_bytes:
            raise EngineScratchError(
                f"engine input exceeds the scratch staging limit ({max_bytes} bytes): {display_path}"
            )
        payload = bytearray()
        while chunk := os.read(descriptor, min(_COPY_CHUNK_BYTES, max_bytes + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise EngineScratchError(
                    f"engine input exceeds the scratch staging limit ({max_bytes} bytes): "
                    f"{display_path}"
                )
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or len(payload) != after.st_size:
            raise EngineScratchError(f"engine input changed while staged: {display_path}")
        return bytes(payload), stat.S_IMODE(after.st_mode)
    finally:
        os.close(descriptor)


def _capture_input_closure(
    durable_dir_fd: int,
    durable_dir: Path,
    selected_name: str,
    *,
    dependency_names_from_primary: Callable[[bytes], Sequence[str]] | None,
) -> dict[str, _CapturedInput]:
    selected_payload, selected_mode = _read_stable_regular_file_at(
        durable_dir_fd,
        selected_name,
        display_path=durable_dir / selected_name,
    )
    captured = {
        selected_name: _CapturedInput(
            name=selected_name,
            payload=selected_payload,
            mode=selected_mode,
        )
    }
    dependencies = (
        dependency_names_from_primary(selected_payload)
        if dependency_names_from_primary is not None
        else ()
    )
    for dependency_name in dependencies:
        raw = Path(str(dependency_name))
        if raw.is_absolute() or raw.name != str(dependency_name) or raw.name in {"", ".", ".."}:
            raise EngineScratchError(
                "RAM scratch requires input dependencies to use one relative basename: "
                f"{dependency_name!r}"
            )
        if raw.name in _SCRATCH_CONTROL_FILE_NAMES or raw.name in _DURABLE_RESERVED_FILE_NAMES:
            raise EngineScratchError(
                f"ORCA RAM scratch dependency collides with runtime state: {raw.name}"
            )
        payload, mode = _read_stable_regular_file_at(
            durable_dir_fd,
            raw.name,
            display_path=durable_dir / raw.name,
        )
        captured.setdefault(
            raw.name,
            _CapturedInput(name=raw.name, payload=payload, mode=mode),
        )
    return captured


def _input_closure_size_bytes(
    selected_name: str,
    captured_inputs: dict[str, _CapturedInput],
    *,
    normalize_primary_newline: bool,
) -> int:
    required = sum(len(item.payload) for item in captured_inputs.values())
    selected_payload = captured_inputs[selected_name].payload
    if normalize_primary_newline and selected_payload and not selected_payload.endswith(b"\n"):
        required += 1
    return required


def _stage_input_closure(
    workspace: Path,
    durable_input: Path,
    *,
    workspace_dir_fd: int,
    captured_inputs: dict[str, _CapturedInput],
    normalize_primary_newline: bool,
) -> dict[str, _StagedInput]:
    staged: dict[str, _StagedInput] = {}
    for name, captured in captured_inputs.items():
        source = durable_input.parent / name
        payload = captured.payload
        staged_payload = payload
        if (
            normalize_primary_newline
            and source == durable_input
            and payload
            and not payload.endswith(b"\n")
        ):
            staged_payload += b"\n"
        item = _StagedInput(
            source=source,
            name=name,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_size_bytes=len(payload),
            staged_sha256=hashlib.sha256(staged_payload).hexdigest(),
            staged_size_bytes=len(staged_payload),
            mode=captured.mode,
        )
        _atomic_write_bytes_at(
            workspace_dir_fd,
            name,
            staged_payload,
            mode=item.mode,
        )
        staged[name] = item
    return staged


def _regular_file_sha256_at(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
) -> tuple[str, int]:
    flags = os.O_RDONLY
    flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EngineScratchError(f"engine durable artifact is unsafe: {display_path}")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or size != after.st_size:
            raise EngineScratchError(f"engine durable artifact changed while read: {display_path}")
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _verify_staged_sources_unchanged(
    durable_dir_fd: int,
    durable_dir: Path,
    staged: dict[str, _StagedInput],
) -> None:
    for item in staged.values():
        digest, size = _regular_file_sha256_at(
            durable_dir_fd,
            item.name,
            display_path=durable_dir / item.name,
        )
        if digest != item.source_sha256 or size != item.source_size_bytes:
            raise EngineScratchError(
                f"Durable engine input changed during scratch run: {item.source}"
            )


def _validate_publication_target_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise EngineScratchError(f"engine scratch artifact has an unsafe basename: {name!r}")
    if (
        name in _DURABLE_RESERVED_FILE_NAMES
        or name.startswith(_PUBLICATION_TEMP_PREFIX)
        or name.startswith(_PUBLICATION_BACKUP_PREFIX)
        or name.startswith(_PUBLICATION_META_TEMP_PREFIX)
    ):
        raise EngineScratchError(f"engine scratch artifact collides with runtime state: {name}")


def _copy_artifact_to_staging(
    source_name: str,
    workspace: Path,
    workspace_dir_fd: int,
    durable_dir: Path,
    durable_dir_fd: int,
) -> _PreparedPublication:
    source = workspace / source_name
    target_name = source_name
    _validate_publication_target_name(target_name)
    try:
        target_info = os.stat(target_name, dir_fd=durable_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        target_info = None
    if target_info is not None and (
        not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 1
    ):
        raise EngineScratchError(
            f"Durable engine artifact target is unsafe: {durable_dir / target_name}"
        )
    temporary_name = f"{_PUBLICATION_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
    source_flags = os.O_RDONLY
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    source_flags |= os.O_NOFOLLOW
    destination_flags |= os.O_NOFOLLOW
    source_fd = os.open(source_name, source_flags, dir_fd=workspace_dir_fd)
    destination_fd: int | None = None
    backup_name: str | None = None
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise EngineScratchError(f"engine scratch artifact is unsafe: {source}")
        destination_fd = os.open(
            temporary_name,
            destination_flags,
            0o600,
            dir_fd=durable_dir_fd,
        )
        digest = hashlib.sha256()
        copied = 0
        while chunk := os.read(source_fd, _COPY_CHUNK_BYTES):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise EngineScratchError(f"Failed to publish engine scratch artifact: {source}")
                view = view[written:]
            copied += len(chunk)
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or copied != after.st_size:
            raise EngineScratchError(f"engine scratch artifact changed while published: {source}")
        os.close(destination_fd)
        destination_fd = None
        if target_info is not None:
            backup_name = f"{_PUBLICATION_BACKUP_PREFIX}{secrets.token_hex(16)}"
            os.link(
                target_name,
                backup_name,
                src_dir_fd=durable_dir_fd,
                dst_dir_fd=durable_dir_fd,
                follow_symlinks=False,
            )
            linked = os.stat(backup_name, dir_fd=durable_dir_fd, follow_symlinks=False)
            current_target = os.stat(
                target_name,
                dir_fd=durable_dir_fd,
                follow_symlinks=False,
            )
            if _identity(linked) != _identity(target_info) or _identity(
                current_target
            ) != _identity(target_info):
                raise EngineScratchError(
                    f"Durable engine artifact target changed while prepared: {durable_dir / target_name}"
                )
        return _PreparedPublication(
            source=source,
            temporary_name=temporary_name,
            target_name=target_name,
            backup_name=backup_name,
            sha256=digest.hexdigest(),
            size_bytes=copied,
        )
    except BaseException:
        for name in (backup_name, temporary_name):
            if name:
                try:
                    os.unlink(name, dir_fd=durable_dir_fd)
                except FileNotFoundError:
                    pass
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _atomic_write_json_at(directory_fd: int, name: str, payload: dict[str, Any]) -> None:
    temporary_name = f"{_PUBLICATION_META_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    try:
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise EngineScratchError("Failed to persist engine scratch publication journal")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _publication_journal_payload(
    items: list[_PreparedPublication],
    *,
    phase: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": phase,
        "items": [
            {
                "temporary_name": item.temporary_name,
                "target_name": item.target_name,
                "backup_name": item.backup_name,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in items
        ],
    }


def _load_publication_journal(
    durable_dir_fd: int,
    durable_dir: Path,
) -> tuple[str, list[_PreparedPublication]] | None:
    try:
        payload, _mode = _read_stable_regular_file_at(
            durable_dir_fd,
            _PUBLICATION_JOURNAL_FILE_NAME,
            display_path=durable_dir / _PUBLICATION_JOURNAL_FILE_NAME,
            max_bytes=16 * 1024 * 1024,
        )
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise EngineScratchError("engine scratch publication journal is corrupt") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        raise EngineScratchError("engine scratch publication journal has an invalid schema")
    phase = parsed.get("phase")
    raw_items = parsed.get("items")
    if phase not in {"prepared", "committed"} or not isinstance(raw_items, list):
        raise EngineScratchError("engine scratch publication journal has invalid state")
    items: list[_PreparedPublication] = []
    seen_names: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise EngineScratchError("engine scratch publication journal has an invalid item")
        temporary_name = raw.get("temporary_name")
        target_name = raw.get("target_name")
        backup_name = raw.get("backup_name")
        digest = raw.get("sha256")
        size_bytes = raw.get("size_bytes")
        if (
            not isinstance(temporary_name, str)
            or _PUBLICATION_TEMP_NAME_RE.fullmatch(temporary_name) is None
            or not isinstance(target_name, str)
            or (backup_name is not None and not isinstance(backup_name, str))
            or (
                isinstance(backup_name, str)
                and _PUBLICATION_BACKUP_NAME_RE.fullmatch(backup_name) is None
            )
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size_bytes) is not int
            or size_bytes < 0
        ):
            raise EngineScratchError("engine scratch publication journal has an invalid item")
        _validate_publication_target_name(target_name)
        item_names = {temporary_name, target_name}
        if isinstance(backup_name, str):
            item_names.add(backup_name)
        if seen_names.intersection(item_names):
            raise EngineScratchError("engine scratch publication journal contains duplicate names")
        seen_names.update(item_names)
        items.append(
            _PreparedPublication(
                source=durable_dir / target_name,
                temporary_name=temporary_name,
                target_name=target_name,
                backup_name=backup_name,
                sha256=digest,
                size_bytes=size_bytes,
            )
        )
    return phase, items


def _unlink_at_if_present(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _assert_no_orphan_publication_files(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        if not name.startswith(
            (_PUBLICATION_TEMP_PREFIX, _PUBLICATION_BACKUP_PREFIX, _PUBLICATION_META_TEMP_PREFIX)
        ):
            continue
        raise EngineScratchError(
            f"engine durable generation contains an unresolved scratch publication entry: {name}"
        )


def _recover_incomplete_publication(
    durable_dir_fd: int,
    durable_dir: Path,
    durable_dir_identity: tuple[int, int],
    *,
    require_path_identity: bool = True,
) -> str | None:
    if require_path_identity:
        _require_directory_path_identity(
            durable_dir,
            durable_dir_fd,
            durable_dir_identity,
            label="engine durable generation",
        )
    loaded = _load_publication_journal(durable_dir_fd, durable_dir)
    if loaded is None:
        _assert_no_orphan_publication_files(durable_dir_fd)
        os.fsync(durable_dir_fd)
        return None
    phase, items = loaded
    if phase == "prepared":
        for item in items:
            if item.backup_name is None:
                continue
            info = os.stat(item.backup_name, dir_fd=durable_dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise EngineScratchError(
                    f"engine scratch publication backup is unsafe: {item.backup_name}"
                )
        for item in items:
            if item.backup_name is not None:
                backup_info = os.stat(
                    item.backup_name,
                    dir_fd=durable_dir_fd,
                    follow_symlinks=False,
                )
                try:
                    target_info = os.stat(
                        item.target_name,
                        dir_fd=durable_dir_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    target_info = None
                if target_info is not None and _identity(target_info) == _identity(backup_info):
                    _unlink_at_if_present(durable_dir_fd, item.backup_name)
                else:
                    if target_info is not None:
                        digest, size = _regular_file_sha256_at(
                            durable_dir_fd,
                            item.target_name,
                            display_path=durable_dir / item.target_name,
                        )
                        if digest != item.sha256 or size != item.size_bytes:
                            raise EngineScratchError(
                                "Prepared engine scratch publication target changed: "
                                f"{durable_dir / item.target_name}"
                            )
                    os.replace(
                        item.backup_name,
                        item.target_name,
                        src_dir_fd=durable_dir_fd,
                        dst_dir_fd=durable_dir_fd,
                    )
            else:
                try:
                    digest, size = _regular_file_sha256_at(
                        durable_dir_fd,
                        item.target_name,
                        display_path=durable_dir / item.target_name,
                    )
                except FileNotFoundError:
                    pass
                else:
                    if digest != item.sha256 or size != item.size_bytes:
                        raise EngineScratchError(
                            "Prepared engine scratch publication target changed: "
                            f"{durable_dir / item.target_name}"
                        )
                    _unlink_at_if_present(durable_dir_fd, item.target_name)
            _unlink_at_if_present(durable_dir_fd, item.temporary_name)
    else:
        for item in items:
            digest, size = _regular_file_sha256_at(
                durable_dir_fd,
                item.target_name,
                display_path=durable_dir / item.target_name,
            )
            if digest != item.sha256 or size != item.size_bytes:
                raise EngineScratchError(
                    f"Committed engine scratch artifact changed: {durable_dir / item.target_name}"
                )
        for item in items:
            if item.backup_name is not None:
                _unlink_at_if_present(durable_dir_fd, item.backup_name)
            _unlink_at_if_present(durable_dir_fd, item.temporary_name)
    _unlink_at_if_present(durable_dir_fd, _PUBLICATION_JOURNAL_FILE_NAME)
    _assert_no_orphan_publication_files(durable_dir_fd)
    os.fsync(durable_dir_fd)
    if require_path_identity:
        _require_directory_path_identity(
            durable_dir,
            durable_dir_fd,
            durable_dir_identity,
            label="engine durable generation",
        )
    return phase


def _scratch_entries(workspace: Path, workspace_dir_fd: int) -> list[str]:
    entries: list[str] = []
    for name in sorted(os.listdir(workspace_dir_fd)):
        if Path(name).name != name:
            raise EngineScratchError(f"engine scratch produced an unsafe entry name: {name!r}")
        if name in _SCRATCH_CONTROL_FILE_NAMES:
            continue
        entries.append(name)
    return entries


def _transient_classification_name(name: str, attempt_stem: str) -> str:
    prefix = f"{attempt_stem}."
    if name.startswith(prefix):
        return name[len(attempt_stem) :]
    return name


def _publish_workspace(
    workspace: Path,
    workspace_dir_fd: int,
    workspace_identity: tuple[int, int],
    durable_dir: Path,
    durable_dir_fd: int,
    durable_dir_identity: tuple[int, int],
    attempt_stem: str,
    staged_inputs: dict[str, _StagedInput],
    *,
    publish_name: Callable[[str], bool] | None,
) -> ScratchPublication:
    staged_publications: list[_PreparedPublication] = []
    omitted: list[str] = []
    omitted_bytes = 0
    journal_written = False
    commit_durable = False
    try:
        for source_name in _scratch_entries(workspace, workspace_dir_fd):
            baseline = staged_inputs.get(source_name)
            if baseline is not None:
                digest, size = _regular_file_sha256_at(
                    workspace_dir_fd,
                    source_name,
                    display_path=workspace / source_name,
                )
                if digest == baseline.staged_sha256 and size == baseline.staged_size_bytes:
                    continue
                raise EngineScratchError(
                    f"ORCA modified a staged immutable input in scratch: {source_name}"
                )
            source_info = os.stat(
                source_name,
                dir_fd=workspace_dir_fd,
                follow_symlinks=False,
            )
            if publish_name is not None and not publish_name(source_name):
                omitted.append(source_name)
                # Only regular-file content counts; a directory's st_size is
                # its dirent size, not an omitted volume.
                if stat.S_ISREG(source_info.st_mode):
                    omitted_bytes += int(source_info.st_size)
                continue
            if not stat.S_ISREG(source_info.st_mode):
                raise EngineScratchError(
                    f"engine scratch produced an unsupported entry: {workspace / source_name}"
                )
            if is_transient_scratch_file(_transient_classification_name(source_name, attempt_stem)):
                omitted.append(source_name)
                omitted_bytes += os.stat(
                    source_name,
                    dir_fd=workspace_dir_fd,
                    follow_symlinks=False,
                ).st_size
                continue
            staged_publications.append(
                _copy_artifact_to_staging(
                    source_name,
                    workspace,
                    workspace_dir_fd,
                    durable_dir,
                    durable_dir_fd,
                )
            )
        _require_directory_path_identity(
            workspace,
            workspace_dir_fd,
            workspace_identity,
            label="engine scratch workspace",
        )
        _atomic_write_json_at(
            durable_dir_fd,
            _PUBLICATION_JOURNAL_FILE_NAME,
            _publication_journal_payload(staged_publications, phase="prepared"),
        )
        journal_written = True
        for item in staged_publications:
            os.replace(
                item.temporary_name,
                item.target_name,
                src_dir_fd=durable_dir_fd,
                dst_dir_fd=durable_dir_fd,
            )
        os.fsync(durable_dir_fd)
        _require_directory_path_identity(
            durable_dir,
            durable_dir_fd,
            durable_dir_identity,
            label="engine durable generation",
        )
        _atomic_write_json_at(
            durable_dir_fd,
            _PUBLICATION_JOURNAL_FILE_NAME,
            _publication_journal_payload(staged_publications, phase="committed"),
        )
        commit_durable = True
        _recover_incomplete_publication(
            durable_dir_fd,
            durable_dir,
            durable_dir_identity,
        )
        return ScratchPublication(
            paths=tuple(durable_dir / item.target_name for item in staged_publications),
            omitted_transient_files=tuple(omitted),
            omitted_transient_bytes=omitted_bytes,
        )
    except BaseException:
        if journal_written:
            path_identity_safe = True
            try:
                _require_directory_path_identity(
                    durable_dir,
                    durable_dir_fd,
                    durable_dir_identity,
                    label="engine durable generation",
                )
            except EngineScratchError:
                path_identity_safe = False
            try:
                recovered_phase = _recover_incomplete_publication(
                    durable_dir_fd,
                    durable_dir,
                    durable_dir_identity,
                    require_path_identity=path_identity_safe,
                )
            except BaseException as recovery_exc:
                raise EngineScratchError(
                    "engine scratch publication failed and its durable transaction "
                    "could not be reconciled"
                ) from recovery_exc
            if path_identity_safe and (commit_durable or recovered_phase == "committed"):
                return ScratchPublication(
                    paths=tuple(durable_dir / item.target_name for item in staged_publications),
                    omitted_transient_files=tuple(omitted),
                    omitted_transient_bytes=omitted_bytes,
                )
        else:
            for item in staged_publications:
                if item.backup_name is not None:
                    _unlink_at_if_present(durable_dir_fd, item.backup_name)
                _unlink_at_if_present(durable_dir_fd, item.temporary_name)
            os.fsync(durable_dir_fd)
        raise


def _remove_owned_workspace(
    root: Path,
    root_identity: tuple[int, int],
    workspace_name: str,
    workspace_identity: tuple[int, int],
) -> None:
    root_fd, observed_root_identity = _open_pinned_directory(
        root,
        label="engine scratch root",
        expected_identity=root_identity,
    )
    try:
        _remove_owned_workspace_at(root_fd, workspace_name, workspace_identity)
    finally:
        os.close(root_fd)


def _remove_owned_workspace_at(
    root_fd: int,
    workspace_name: str,
    workspace_identity: tuple[int, int],
) -> None:
    tombstone_name = f".orca_auto_cleanup.{secrets.token_hex(16)}"
    before = os.stat(workspace_name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or _identity(before) != workspace_identity:
        raise EngineScratchError("engine scratch workspace identity changed before cleanup")
    os.rename(
        workspace_name,
        tombstone_name,
        src_dir_fd=root_fd,
        dst_dir_fd=root_fd,
    )
    after = os.stat(tombstone_name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(after.st_mode) or _identity(after) != workspace_identity:
        try:
            os.rename(
                tombstone_name,
                workspace_name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
        except OSError:
            pass
        raise EngineScratchError("engine scratch workspace identity changed during cleanup")
    shutil.rmtree(tombstone_name, dir_fd=root_fd)
    os.fsync(root_fd)


__all__ = [
    "EngineScratchError",
    "EngineScratchPolicy",
    "EngineScratchWorkspace",
    "SCRATCH_RUNTIME_HOME_DIR_NAME",
    "ScratchPublication",
    "attach_scratch_provenance_mapping_to_exception",
    "attach_scratch_provenance_to_exception",
    "is_transient_scratch_file",
    "publish_engine_scratch_workspace",
    "scratch_provenance_from_exception",
    "scratch_publication_provenance",
]
