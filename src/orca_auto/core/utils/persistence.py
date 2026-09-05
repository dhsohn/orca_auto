from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from .coercion import safe_int as _safe_int

JSON_LOAD_EXCEPTIONS = (OSError, UnicodeDecodeError, json.JSONDecodeError)


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def timestamped_token(prefix: str, *, token_bytes: int = 16) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{token_hex(token_bytes)}"


def timestamped_token_pattern(prefix: str, *, token_bytes: int = 16) -> re.Pattern[str]:
    """Pattern whose ``fullmatch`` accepts exactly what ``timestamped_token`` mints."""
    return re.compile(rf"{re.escape(prefix)}_[0-9]{{8}}_[0-9]{{6}}_[0-9a-f]{{{2 * token_bytes}}}")


def coerce_int(value: Any, *, default: int = 0) -> int:
    return _safe_int(value, default=default)


def resolve_root_path(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def load_json_list_file(
    path: Path,
    *,
    corrupt_error: type[Exception],
    description: str,
) -> list[Any]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise corrupt_error(f"{description} cannot be read: {path}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise corrupt_error(f"{description} is not valid JSON: {path}") from exc
    if not isinstance(raw, list):
        raise corrupt_error(f"{description} must contain a JSON list: {path}")
    return raw


def load_json_file(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except JSON_LOAD_EXCEPTIONS:
        return None


def load_json_mapping_file(path: Path) -> dict[str, Any] | None:
    raw = load_json_file(path)
    return raw if isinstance(raw, dict) else None


def load_json_mapping_list_file(path: Path) -> list[dict[str, Any]]:
    raw = load_json_file(path)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def open_pinned_readonly(path: str | Path, *, dir_fd: int | None = None) -> int:
    """Acquire a non-inheritable read-only fd without following the final symlink.

    Nonblocking acquisition lets callers reject FIFOs before reading. The caller
    owns confinement, regular-file/link validation, stability checks and closing
    the descriptor; opening alone does not establish those guarantees.
    """
    return os.open(
        path,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=dir_fd,
    )


def fsync_directory(path: str | Path) -> None:
    """Durably publish directory-entry changes."""

    flags = os.O_RDONLY
    flags |= os.O_DIRECTORY

    dir_fd = os.open(str(Path(path)), flags)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def durable_mkdir(
    path: str | Path,
    *,
    mode: int = 0o777,
    parents: bool = False,
    exist_ok: bool = False,
) -> Path:
    """Create a directory and durably publish every newly needed path component."""

    target = Path(path)
    missing: list[Path] = []
    current = target
    if parents:
        while not current.exists():
            missing.append(current)
            if current == current.parent:
                break
            current = current.parent
    elif not target.exists():
        missing.append(target)
    target.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)
    # Always sync the leaf parent. Another creator may have won after the
    # existence probe, and its directory entry still needs our durability gate.
    fsync_directory(target.parent)
    for created in missing[1:]:
        fsync_directory(created.parent)
    return target


def _fsync_parent_dir(path: Path) -> None:
    fsync_directory(path.parent)


def atomic_write_text(path: Path, payload: str) -> None:
    durable_mkdir(path.parent, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_dir(path)
    finally:
        if tmp_path.exists():
            with suppress(OSError):
                tmp_path.unlink()


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    ensure_ascii: bool = True,
    indent: int | None = 2,
) -> None:
    atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=ensure_ascii,
            indent=indent,
            sort_keys=False,
            allow_nan=False,
        ),
    )
