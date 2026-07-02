from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from .coercion import safe_int as _safe_int

JSON_LOAD_EXCEPTIONS = (OSError, UnicodeDecodeError, json.JSONDecodeError)

_DIR_FSYNC_UNSUPPORTED_ERRNOS = {
    code
    for code in (
        errno.EACCES,
        errno.EBADF,
        errno.EINVAL,
        errno.EISDIR,
        errno.EPERM,
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "ENOTTY", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if code is not None
}


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


def timestamped_token(prefix: str, *, token_bytes: int = 3) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{token_hex(token_bytes)}"


def coerce_int(value: Any, *, default: int = 0) -> int:
    return _safe_int(value, default=default)


def coerce_optional_int(value: Any) -> int | None:
    return _safe_int(value, default=None)


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"", "0", "false", "no", "n", "off"}:
            return False
    return default


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


def _is_unsupported_dir_fsync_error(exc: OSError) -> bool:
    return exc.errno in _DIR_FSYNC_UNSUPPORTED_ERRNOS


def _fsync_parent_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY

    try:
        dir_fd = os.open(str(path.parent), flags)
    except OSError as exc:
        if _is_unsupported_dir_fsync_error(exc):
            return
        raise

    try:
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            if not _is_unsupported_dir_fsync_error(exc):
                raise
    finally:
        os.close(dir_fd)


def atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent, sort_keys=False),
    )
