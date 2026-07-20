"""Provider-neutral policy for accepting uploaded run-dir archives.

The interactive bot may accept a compressed run directory over a messenger and
submit it to the queue.  Because the archive is untrusted bytes that become a
directory on the host, every acceptance is bounded by this policy: a hard
feature switch, size ceilings that guard against decompression bombs, and an
extension allowlist so only known run-dir file types are written to disk.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_MIB = 1024 * 1024
MAX_CONCURRENT_UPLOAD_DOWNLOADS = 16
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_ASCII_INTEGER_PATTERN = re.compile(r"^[+-]?[0-9]+$")
UPLOAD_POLICY_CONFIG_FIELDS = frozenset(
    {
        "allowed_extensions",
        "committed_retention_seconds",
        "enabled",
        "max_archive_bytes",
        "max_concurrent_downloads",
        "max_entries",
        "max_file_bytes",
        "max_pending_per_actor",
        "max_staged_bytes",
        "max_staged_uploads",
        "max_total_uncompressed_bytes",
        "staging_ttl_seconds",
    }
)

# Known run-dir file types.  ORCA run-dirs carry ``*.inp``/``*.xyz`` (plus
# optional restart/hessian artifacts); fresh workflow run-dirs carry
# ``flow.yaml`` and per-endpoint geometries. Persisted ``workflow.json`` state is
# rejected independently even though other JSON inputs may be allowed.
DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    ".inp",
    ".xyz",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".gbw",
    ".hess",
    ".pc",
    ".cif",
    ".pdb",
    ".mol",
    ".mol2",
    ".sdf",
    ".trj",
    ".allxyz",
    ".engrad",
    ".gzmt",
    ".zmat",
)


def _explicit_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise ValueError(
        f"{field_name} must be a boolean or one of true/false, yes/no, on/off, or 1/0."
    )


def _positive_int(value: Any, *, field_name: str) -> int:
    # ``bool`` is an ``int`` subclass; ``int(True) == 1`` would silently accept
    # ``max_archive_bytes: true`` as a 1-byte limit. Reject it explicitly.
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not a boolean. got={value!r}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be an integer. got={value!r}")
        parsed = int(value)
    elif isinstance(value, str) and _ASCII_INTEGER_PATTERN.fullmatch(value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{field_name} must be an integer. got={value!r}")
    if parsed <= 0:
        raise ValueError(f"{field_name} must be positive. got={parsed}")
    return parsed


def _normalized_extensions(raw: object) -> tuple[str, ...]:
    # An explicitly provided list is honored, including an empty list, which locks
    # the feature down rather than reverting to the default allowlist.
    if isinstance(raw, (str, bytes, bytearray, Mapping)) or not isinstance(raw, Sequence):
        raise ValueError("uploads.allowed_extensions must be a list of extensions")
    seen: dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("uploads.allowed_extensions entries must be non-empty strings")
        text = item.strip().lower()
        if not text.startswith("."):
            text = f".{text}"
        seen.setdefault(text, None)
    return tuple(seen)


@dataclass(frozen=True)
class UploadPolicy:
    """Bounds applied to an inbound run-dir archive before it is written."""

    enabled: bool = False
    max_archive_bytes: int = 25 * _MIB
    max_total_uncompressed_bytes: int = 200 * _MIB
    max_entries: int = 4000
    max_file_bytes: int = 100 * _MIB
    allowed_extensions: tuple[str, ...] = field(default=DEFAULT_ALLOWED_EXTENSIONS)
    max_staged_bytes: int = 512 * _MIB
    max_staged_uploads: int = 32
    max_pending_per_actor: int = 4
    max_concurrent_downloads: int = 4
    staging_ttl_seconds: int = 3600
    committed_retention_seconds: int = 86400

    def allows_suffix(self, suffix: str) -> bool:
        return suffix.strip().lower() in self.allowed_extensions


def upload_policy_from_mapping(raw: object) -> UploadPolicy:
    """Build an :class:`UploadPolicy` from a ``messenger.discord.uploads`` block."""

    if isinstance(raw, Mapping):
        data = raw
    else:
        raise ValueError("uploads must be a mapping when configured")
    unknown = sorted(str(key) for key in data if key not in UPLOAD_POLICY_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"Unknown uploads config fields: {', '.join(unknown)}.")

    def positive_int_field(key: str, default: int) -> int:
        if key not in data:
            return default
        return _positive_int(data.get(key), field_name=f"uploads.{key}")

    policy = UploadPolicy(
        enabled=(
            _explicit_bool(data.get("enabled"), field_name="uploads.enabled")
            if "enabled" in data
            else False
        ),
        max_archive_bytes=positive_int_field("max_archive_bytes", 25 * _MIB),
        max_total_uncompressed_bytes=positive_int_field("max_total_uncompressed_bytes", 200 * _MIB),
        max_entries=positive_int_field("max_entries", 4000),
        max_file_bytes=positive_int_field("max_file_bytes", 100 * _MIB),
        allowed_extensions=(
            _normalized_extensions(data.get("allowed_extensions"))
            if "allowed_extensions" in data
            else DEFAULT_ALLOWED_EXTENSIONS
        ),
        max_staged_bytes=positive_int_field("max_staged_bytes", 512 * _MIB),
        max_staged_uploads=positive_int_field("max_staged_uploads", 32),
        max_pending_per_actor=positive_int_field("max_pending_per_actor", 4),
        max_concurrent_downloads=positive_int_field("max_concurrent_downloads", 4),
        staging_ttl_seconds=positive_int_field("staging_ttl_seconds", 3600),
        committed_retention_seconds=positive_int_field("committed_retention_seconds", 86400),
    )
    if policy.max_staged_bytes < policy.max_archive_bytes:
        raise ValueError(
            "uploads.max_staged_bytes must be greater than or equal to uploads.max_archive_bytes"
        )
    if policy.max_concurrent_downloads > MAX_CONCURRENT_UPLOAD_DOWNLOADS:
        raise ValueError(
            "uploads.max_concurrent_downloads must be less than or equal to "
            f"{MAX_CONCURRENT_UPLOAD_DOWNLOADS}"
        )
    return policy


__all__ = [
    "DEFAULT_ALLOWED_EXTENSIONS",
    "MAX_CONCURRENT_UPLOAD_DOWNLOADS",
    "UPLOAD_POLICY_CONFIG_FIELDS",
    "UploadPolicy",
    "upload_policy_from_mapping",
]
