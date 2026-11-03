"""Read one JSON artifact file through the stable regular-file reader.

Every reader of a durable JSON artifact (``job_state.json``, ``machine.json``)
opens it here: without following a final symlink, without blocking on a FIFO,
only as a regular file within a byte limit, and only when the file did not
change while it was read. :class:`JsonArtifactRead` separates a missing file
from one that exists but cannot be used, so each caller decides only how to
report the second.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .stable_fs import StableFsError, read_stable_regular_file_at


@dataclass(frozen=True)
class JsonArtifactRead:
    """``missing``, ``invalid`` (with ``reason``) or ``valid`` (with the parsed ``value``)."""

    kind: Literal["missing", "invalid", "valid"]
    value: Any = None
    reason: str = ""
    status: os.stat_result | None = None  # the file's fstat after the read, when read
    error: BaseException | None = None  # the exception behind an invalid read


def read_json_artifact(
    name: str | Path, *, max_bytes: int, dir_fd: int | None = None
) -> JsonArtifactRead:
    """Read and parse ``name`` (relative to ``dir_fd`` when given)."""
    try:
        payload, status = read_stable_regular_file_at(name, dir_fd=dir_fd, max_bytes=max_bytes)
    except FileNotFoundError:
        return JsonArtifactRead("missing")
    except StableFsError as exc:
        # not_regular (a FIFO, a directory), too_large, changed while read.
        return JsonArtifactRead("invalid", reason=exc.reason, error=exc)
    except OSError as exc:
        # A final symlink (ELOOP), a permission error, an I/O error.
        return JsonArtifactRead(
            "invalid", reason=f"unreadable ({exc.__class__.__name__})", error=exc
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        # JSONDecodeError and UnicodeDecodeError are ValueErrors, and so is
        # the parser's own refusal of an integer over the int-to-str digit
        # limit: each is a file that exists but cannot be used.
        return JsonArtifactRead(
            "invalid",
            reason=f"unreadable JSON ({exc.__class__.__name__})",
            status=status,
            error=exc,
        )
    return JsonArtifactRead("valid", value=value, status=status)


# The byte limit of one durable JSON store (queue.json, admission_slots.json,
# job_locations.json). These hold one row per job, far above any run artifact.
MAX_STORE_JSON_BYTES = 256 * 1024 * 1024


def load_json_list_file(
    path: Path,
    *,
    corrupt_error: type[Exception],
    description: str,
    max_bytes: int = MAX_STORE_JSON_BYTES,
) -> list[Any]:
    """The JSON list at ``path``; ``[]`` only when the file is missing.

    Read through :func:`read_json_artifact`, so a FIFO is refused without
    blocking (a caller holding its store lock releases it at once), and a
    dangling symlink, a non-regular file or a file changed while read is
    ``corrupt_error``, never an empty store. Each store keeps its own row
    checks and error type.
    """
    read = read_json_artifact(path, max_bytes=max_bytes)
    if read.kind == "missing":
        return []
    if read.kind == "invalid":
        if read.status is not None:
            # The bytes were read; they are not JSON.
            raise corrupt_error(f"{description} is not valid JSON: {path}") from read.error
        # Not a regular file (a FIFO, a dangling symlink), changed while read,
        # too large, or an I/O error: the store exists but cannot be used.
        raise corrupt_error(f"{description} cannot be read: {path}") from read.error
    if not isinstance(read.value, list):
        raise corrupt_error(f"{description} must contain a JSON list: {path}")
    return read.value


__all__ = [
    "MAX_STORE_JSON_BYTES",
    "JsonArtifactRead",
    "load_json_list_file",
    "read_json_artifact",
]
