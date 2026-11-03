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
        return JsonArtifactRead("invalid", reason=exc.reason)
    except OSError as exc:
        # A final symlink (ELOOP), a permission error, an I/O error.
        return JsonArtifactRead("invalid", reason=f"unreadable ({exc.__class__.__name__})")
    try:
        value = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        # JSONDecodeError and UnicodeDecodeError are ValueErrors, and so is
        # the parser's own refusal of an integer over the int-to-str digit
        # limit: each is a file that exists but cannot be used.
        return JsonArtifactRead(
            "invalid", reason=f"unreadable JSON ({exc.__class__.__name__})", status=status
        )
    return JsonArtifactRead("valid", value=value, status=status)


__all__ = ["JsonArtifactRead", "read_json_artifact"]
