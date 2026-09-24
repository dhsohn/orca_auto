"""Journaled publication of workspace outputs into the durable generation and
its crash recovery: copy each artifact to a temp name, record a
``prepared`` / ``committed`` journal, rename into place, and on the next
launch replay or roll back whatever an interrupted publication left. Also
owns the ``ScratchPublication`` result and the provenance helpers that
attach it to exceptions for the caller's report.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.utils import stable_fs
from orca_auto.core.utils.persistence import open_pinned_readonly
from orca_auto.core.utils.stable_fs import unlink_at_if_present as _unlink_at_if_present

from ._constants import (
    _COPY_CHUNK_BYTES,
    _DURABLE_RESERVED_FILE_NAMES,
    _PUBLICATION_BACKUP_NAME_RE,
    _PUBLICATION_BACKUP_PREFIX,
    _PUBLICATION_JOURNAL_FILE_NAME,
    _PUBLICATION_META_TEMP_PREFIX,
    _PUBLICATION_TEMP_NAME_RE,
    _PUBLICATION_TEMP_PREFIX,
    _SCRATCH_CONTROL_FILE_NAMES,
    _TRANSIENT_FILE_RE,
)
from ._errors import EngineScratchError
from ._fs import (
    _read_stable_regular_file_at,
    _regular_file_sha256_at,
    _require_directory_path_identity,
)
from ._staging import _StagedInput


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


def is_transient_scratch_file(name: str) -> bool:
    return _TRANSIENT_FILE_RE.search(name) is not None


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
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    destination_flags |= os.O_NOFOLLOW
    source_fd = open_pinned_readonly(source_name, dir_fd=workspace_dir_fd)
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
            if stable_fs.inode_identity(linked) != stable_fs.inode_identity(
                target_info
            ) or stable_fs.inode_identity(current_target) != stable_fs.inode_identity(target_info):
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
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        stable_fs.atomic_write_bytes_at(
            directory_fd,
            name,
            encoded,
            mode=0o600,
            temp_prefix=_PUBLICATION_META_TEMP_PREFIX,
            force_mode=False,
        )
    except stable_fs.StableFsError as exc:
        raise EngineScratchError("Failed to persist engine scratch publication journal") from exc


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
    except ValueError as exc:
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
                if target_info is not None and stable_fs.inode_identity(
                    target_info
                ) == stable_fs.inode_identity(backup_info):
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


def _scratch_entries(workspace_dir_fd: int) -> list[str]:
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
) -> ScratchPublication:
    staged_publications: list[_PreparedPublication] = []
    omitted: list[str] = []
    omitted_bytes = 0
    journal_written = False
    commit_durable = False
    try:
        for source_name in _scratch_entries(workspace_dir_fd):
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
