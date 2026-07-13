from __future__ import annotations

import json
import os
import re
import shutil
import stat
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

from orca_auto.core.utils import process as process_utils
from orca_auto.core.utils.lock import file_lock
from orca_auto.core.utils.persistence import (
    atomic_write_json,
    durable_mkdir,
    fsync_directory,
    now_utc_iso,
)

SNAPSHOT_INTENT_TOKEN_KEY = "snapshot_intent_token"
SNAPSHOT_INTENT_QUEUE_ROOT_KEY = "snapshot_intent_queue_root"
SNAPSHOT_INTENT_STATE_CREATING = "creating"
SNAPSHOT_INTENT_STATE_ENQUEUEING = "enqueueing"
SNAPSHOT_INTENT_STATE_OWNED = "owned"

_INTENT_DIR_NAME = ".orca_auto_snapshot_intents"
_MAINTENANCE_LOCK_NAME = ".orca_auto_snapshot_intents.lock"
_MUTATION_LOCK_NAME = ".orca_auto_snapshot_intents.mutation.lock"
_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{16,160}")
_MANAGED_PARENT_NAMES = frozenset({".orca_auto_input_snapshots", ".orca_auto_orca_executions"})
_KINDS = frozenset({"input_snapshot_namespace", "orca_execution_pair"})
_MAX_INTENT_BYTES = 32 * 1024
_MAX_PENDING_INTENTS = 1024
_MAX_INTENT_DIRECTORY_ENTRIES = 2 * _MAX_PENDING_INTENTS


class _IntentDirectoryLimitError(RuntimeError):
    pass


def _bounded_intent_paths(intent_dir: Path) -> list[Path]:
    paths: list[Path] = []
    inspected = 0
    with os.scandir(intent_dir) as entries:
        for entry in entries:
            inspected += 1
            if inspected > _MAX_INTENT_DIRECTORY_ENTRIES:
                raise _IntentDirectoryLimitError(
                    "Snapshot intent directory exceeds its bounded scan limit"
                )
            if entry.name.endswith(".json"):
                paths.append(intent_dir / entry.name)
                if len(paths) > _MAX_PENDING_INTENTS:
                    raise _IntentDirectoryLimitError(
                        "Snapshot intent backlog exceeds its safety limit"
                    )
    return sorted(paths)


def _normalized_token(token: str) -> str:
    normalized = str(token).strip()
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError("Snapshot intent token contains unsupported characters")
    return normalized


def _resolved_queue_root(queue_root: str | Path) -> Path:
    raw_root = Path(queue_root).expanduser()
    if raw_root.is_symlink():
        raise ValueError(f"Queue root must not be a symlink: {raw_root}")
    resolved = raw_root.resolve()
    if not resolved.is_dir() or resolved.stat().st_uid != os.geteuid():
        raise ValueError(f"Queue root must be an owned directory: {raw_root}")
    return resolved


def _validated_generation_paths(
    queue_root: Path,
    generation_paths: Iterable[str | Path],
    *,
    require_existing: bool | None,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for raw_value in generation_paths:
        raw_path = Path(raw_value).expanduser()
        if not raw_path.is_absolute() or raw_path.is_symlink():
            raise ValueError(f"Snapshot generation must be an absolute real path: {raw_path}")
        resolved = raw_path.resolve()
        if (
            not resolved.is_relative_to(queue_root)
            or resolved.parent.name not in _MANAGED_PARENT_NAMES
        ):
            raise ValueError(f"Snapshot generation escapes its queue root: {raw_path}")
        if require_existing:
            if not resolved.is_dir() or resolved.stat().st_uid != os.geteuid():
                raise ValueError(f"Snapshot generation must be an owned directory: {raw_path}")
        elif require_existing is False and resolved.exists():
            raise FileExistsError(f"Snapshot generation already exists: {raw_path}")
        if resolved not in paths:
            paths.append(resolved)
    if not paths:
        raise ValueError("Snapshot intent must own at least one generation")
    return tuple(paths)


def _intent_dir(queue_root: Path, *, create: bool) -> Path:
    intent_dir = queue_root / _INTENT_DIR_NAME
    if intent_dir.is_symlink():
        raise ValueError(f"Snapshot intent directory must not be a symlink: {intent_dir}")
    if create:
        durable_mkdir(intent_dir, mode=0o700, exist_ok=True)
    if not intent_dir.is_dir() or intent_dir.resolve().parent != queue_root:
        raise ValueError(f"Snapshot intent directory is invalid: {intent_dir}")
    status = intent_dir.stat()
    if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) & 0o077:
        raise ValueError(f"Snapshot intent directory permissions are unsafe: {intent_dir}")
    return intent_dir.resolve()


def _intent_path(queue_root: Path, token: str, *, create_dir: bool) -> Path:
    return _intent_dir(queue_root, create=create_dir) / f"{_normalized_token(token)}.json"


def _read_intent(path: Path, *, expected_root: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_size > _MAX_INTENT_BYTES
        ):
            raise ValueError(f"Snapshot intent is not a small owned regular file: {path}")
        payload = os.read(descriptor, _MAX_INTENT_BYTES + 1)
        after = os.fstat(descriptor)
        if len(payload) > _MAX_INTENT_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"Snapshot intent changed while being read: {path}")
    finally:
        os.close(descriptor)
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Snapshot intent is invalid JSON: {path}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError(f"Snapshot intent has an unsupported format: {path}")
    token = _normalized_token(str(raw.get("token") or ""))
    if path.name != f"{token}.json" or str(raw.get("queue_root") or "") != str(expected_root):
        raise ValueError(f"Snapshot intent identity mismatch: {path}")
    if str(raw.get("kind") or "") not in _KINDS or str(raw.get("state") or "") not in {
        SNAPSHOT_INTENT_STATE_CREATING,
        SNAPSHOT_INTENT_STATE_ENQUEUEING,
        SNAPSHOT_INTENT_STATE_OWNED,
    }:
        raise ValueError(f"Snapshot intent contains invalid values: {path}")
    raw_paths = raw.get("generation_paths")
    if not isinstance(raw_paths, list):
        raise ValueError(f"Snapshot intent has no generation paths: {path}")
    validated = _validated_generation_paths(expected_root, raw_paths, require_existing=None)
    raw["generation_paths"] = [str(item) for item in validated]
    return raw


def create_snapshot_intent(
    queue_root: str | Path,
    *,
    token: str,
    kind: str,
    generation_paths: Iterable[str | Path],
) -> None:
    resolved_root = _resolved_queue_root(queue_root)
    normalized_kind = str(kind).strip()
    if normalized_kind not in _KINDS:
        raise ValueError(f"Unsupported snapshot intent kind: {kind!r}")
    normalized_token = _normalized_token(token)
    paths = _validated_generation_paths(
        resolved_root,
        generation_paths,
        require_existing=False,
    )
    intent_dir = _intent_dir(resolved_root, create=True)
    with file_lock(resolved_root / _MUTATION_LOCK_NAME):
        if len(_bounded_intent_paths(intent_dir)) >= _MAX_PENDING_INTENTS:
            raise RuntimeError(
                "Snapshot intent backlog reached its safety limit; "
                "retry after worker reconciliation"
            )
        intent_path = intent_dir / f"{normalized_token}.json"
        if intent_path.exists() or intent_path.is_symlink():
            raise FileExistsError(f"Snapshot intent already exists: {intent_path}")
        owner_pid = os.getpid()
        timestamp = now_utc_iso()
        atomic_write_json(
            intent_path,
            {
                "version": 1,
                "token": normalized_token,
                "kind": normalized_kind,
                "state": SNAPSHOT_INTENT_STATE_CREATING,
                "queue_root": str(resolved_root),
                "generation_paths": [str(item) for item in paths],
                "owner_pid": owner_pid,
                "owner_process_start_ticks": process_utils.process_start_ticks(owner_pid),
                "owner_boot_id": process_utils.linux_boot_id(),
                "created_at": timestamp,
                "updated_at": timestamp,
            },
            ensure_ascii=True,
            indent=2,
        )


def transition_snapshot_intent(
    queue_root: str | Path,
    token: str,
    *,
    target_state: str,
    expected_states: Iterable[str],
) -> None:
    resolved_root = _resolved_queue_root(queue_root)
    with file_lock(resolved_root / _MUTATION_LOCK_NAME):
        intent_path = _intent_path(resolved_root, token, create_dir=False)
        marker = _read_intent(intent_path, expected_root=resolved_root)
        target = str(target_state).strip().lower()
        allowed = {str(state).strip().lower() for state in expected_states}
        current = str(marker["state"])
        if current == target:
            return
        if current not in allowed or target not in {
            SNAPSHOT_INTENT_STATE_ENQUEUEING,
            SNAPSHOT_INTENT_STATE_OWNED,
        }:
            raise ValueError(f"Snapshot intent state changed unexpectedly: {intent_path}")
        marker["state"] = target
        marker["updated_at"] = now_utc_iso()
        atomic_write_json(intent_path, marker, ensure_ascii=True, indent=2)


def _unlink_intent(intent_path: Path) -> None:
    try:
        intent_path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(intent_path.parent)


def discard_snapshot_intent(queue_root: str | Path, token: str) -> None:
    resolved_root = _resolved_queue_root(queue_root)
    with file_lock(resolved_root / _MUTATION_LOCK_NAME):
        raw_intent_dir = resolved_root / _INTENT_DIR_NAME
        if not raw_intent_dir.exists() and not raw_intent_dir.is_symlink():
            return
        try:
            intent_path = _intent_path(resolved_root, token, create_dir=False)
        except FileNotFoundError:
            return
        _unlink_intent(intent_path)


def discard_snapshot_intent_if_generations_absent(
    queue_root: str | Path,
    token: str,
) -> bool:
    """Discard an intent only after every declared generation is certainly absent."""

    resolved_root = _resolved_queue_root(queue_root)
    with file_lock(resolved_root / _MUTATION_LOCK_NAME):
        raw_intent_dir = resolved_root / _INTENT_DIR_NAME
        if not raw_intent_dir.exists() and not raw_intent_dir.is_symlink():
            return True
        try:
            intent_path = _intent_path(resolved_root, token, create_dir=False)
            marker = _read_intent(intent_path, expected_root=resolved_root)
        except FileNotFoundError:
            return True
        for generation_path in marker["generation_paths"]:
            try:
                os.lstat(generation_path)
            except FileNotFoundError:
                continue
            return False
        _unlink_intent(intent_path)
        return True


def finalize_queued_snapshot_intent(queue_root: str | Path, entry: Any) -> None:
    """Durably retire an intent before a reserved queue entry starts execution."""

    metadata = getattr(entry, "metadata", {})
    if not isinstance(metadata, Mapping):
        return
    snapshot = metadata.get("execution_snapshot")
    if not isinstance(snapshot, Mapping):
        return
    token = str(snapshot.get(SNAPSHOT_INTENT_TOKEN_KEY) or "").strip()
    intent_root_text = str(snapshot.get(SNAPSHOT_INTENT_QUEUE_ROOT_KEY) or "").strip()
    if not token and not intent_root_text:
        return
    resolved_root = _resolved_queue_root(queue_root)
    if (
        not token
        or not intent_root_text
        or Path(intent_root_text).expanduser().resolve() != resolved_root
    ):
        raise ValueError("Queued snapshot intent does not match its queue root")
    with file_lock(resolved_root / _MUTATION_LOCK_NAME):
        raw_intent_dir = resolved_root / _INTENT_DIR_NAME
        if not raw_intent_dir.exists() and not raw_intent_dir.is_symlink():
            return
        intent_path = _intent_path(resolved_root, token, create_dir=False)
        try:
            marker = _read_intent(intent_path, expected_root=resolved_root)
        except FileNotFoundError:
            return
        _validated_generation_paths(
            resolved_root,
            marker["generation_paths"],
            require_existing=True,
        )
        _unlink_intent(intent_path)


def _owner_is_alive(marker: Mapping[str, Any]) -> bool:
    try:
        owner_pid = int(marker.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return True
    if owner_pid <= 0:
        return True
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    recorded_boot = str(marker.get("owner_boot_id") or "").strip()
    current_boot = process_utils.linux_boot_id()
    if recorded_boot and current_boot and recorded_boot != current_boot:
        return False
    try:
        recorded_ticks = int(marker.get("owner_process_start_ticks") or 0)
    except (TypeError, ValueError):
        recorded_ticks = 0
    current_ticks = process_utils.process_start_ticks(owner_pid)
    if recorded_ticks > 0 and current_ticks is not None:
        return recorded_ticks == current_ticks
    return True


def _entry_references_intent(entry: Any, marker: Mapping[str, Any]) -> bool:
    metadata = getattr(entry, "metadata", {})
    if not isinstance(metadata, Mapping):
        return False
    token = str(marker["token"])
    generation_paths = {Path(value) for value in marker["generation_paths"]}

    def references(value: Any) -> bool:
        if isinstance(value, Mapping):
            if str(value.get(SNAPSHOT_INTENT_TOKEN_KEY) or "") == token:
                return True
            return any(references(nested) for nested in value.values())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(references(nested) for nested in value)
        if isinstance(value, str) and value.startswith("/"):
            try:
                candidate = Path(value).resolve()
            except (OSError, RuntimeError):
                return False
            return any(
                candidate == path or candidate.is_relative_to(path) for path in generation_paths
            )
        return False

    return references(metadata)


def _remove_generation(path: Path, *, queue_root: Path) -> None:
    if not path.exists():
        return
    validated = _validated_generation_paths(queue_root, [path], require_existing=True)[0]
    shutil.rmtree(validated)
    fsync_directory(validated.parent)


def reconcile_orphaned_snapshot_generations(
    queue_roots: Iterable[str | Path],
    *,
    list_queue_fn: Any | None = None,
    owner_is_alive_fn: Any = _owner_is_alive,
) -> int:
    """Resolve durable pre-enqueue intents without scanning active job trees.

    Every production worker retires a referenced intent before starting its
    child process. Therefore a dead ENQUEUEING owner with no raw queue row
    cannot have reached execution; completed jobs cannot lose their last
    ownership evidence before the intent has already been durably retired.
    """

    removed = 0
    roots = tuple(dict.fromkeys(_resolved_queue_root(root) for root in queue_roots))
    for root in roots:
        intent_dir = root / _INTENT_DIR_NAME
        if not intent_dir.exists():
            continue
        resolved_intent_dir = _intent_dir(root, create=False)
        try:
            with file_lock(root / _MAINTENANCE_LOCK_NAME, timeout_seconds=0.0):
                if list_queue_fn is None:
                    from orca_auto.core.queue.store import load_entries, queue_lock

                    queue_context: AbstractContextManager[Any] = queue_lock(root)
                else:
                    queue_context = nullcontext()
                with queue_context:
                    entries = load_entries(root) if list_queue_fn is None else list_queue_fn(root)
                    with file_lock(root / _MUTATION_LOCK_NAME):
                        for intent_path in _bounded_intent_paths(resolved_intent_dir):
                            try:
                                marker = _read_intent(intent_path, expected_root=root)
                            except (FileNotFoundError, OSError, ValueError):
                                continue
                            if marker["state"] == SNAPSHOT_INTENT_STATE_OWNED or any(
                                _entry_references_intent(entry, marker) for entry in entries
                            ):
                                try:
                                    _unlink_intent(intent_path)
                                except OSError:
                                    pass
                                continue
                            if owner_is_alive_fn(marker):
                                continue
                            try:
                                for generation_path in marker["generation_paths"]:
                                    _remove_generation(Path(generation_path), queue_root=root)
                                _unlink_intent(intent_path)
                            except (OSError, ValueError):
                                continue
                            removed += 1
        except (TimeoutError, _IntentDirectoryLimitError):
            continue
    return removed


def snapshot_runtime_roots_for_cfg(cfg: Any) -> tuple[Path, ...]:
    from orca_auto.core.engine_catalog import known_engine_ids
    from orca_auto.core.indexing.roots import runtime_roots_for_cfg

    roots: list[Path] = []
    for engine in known_engine_ids():
        for root in runtime_roots_for_cfg(cfg, engine=engine):
            resolved = Path(root).expanduser().resolve()
            if resolved not in roots:
                roots.append(resolved)
    return tuple(roots)


__all__ = [
    "SNAPSHOT_INTENT_QUEUE_ROOT_KEY",
    "SNAPSHOT_INTENT_STATE_CREATING",
    "SNAPSHOT_INTENT_STATE_ENQUEUEING",
    "SNAPSHOT_INTENT_STATE_OWNED",
    "SNAPSHOT_INTENT_TOKEN_KEY",
    "create_snapshot_intent",
    "discard_snapshot_intent",
    "discard_snapshot_intent_if_generations_absent",
    "finalize_queued_snapshot_intent",
    "reconcile_orphaned_snapshot_generations",
    "snapshot_runtime_roots_for_cfg",
    "transition_snapshot_intent",
]
