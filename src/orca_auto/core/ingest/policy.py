"""Provider-neutral policy for accepting uploaded run-dir archives.

The interactive bot may accept a compressed run directory over a messenger and
submit it to the queue.  Because the archive is untrusted bytes that become a
directory on the host, every acceptance is bounded by this policy: a hard
feature switch, size ceilings that guard against decompression bombs, and an
extension allowlist so only known run-dir file types are written to disk.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orca_auto.core.utils.coercion import normalize_bool

_MIB = 1024 * 1024
MAX_CONCURRENT_UPLOAD_DOWNLOADS = 16

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


def _positive_int(value: Any, *, default: int, field_name: str) -> int:
    if value in (None, ""):
        return default
    # ``bool`` is an ``int`` subclass; ``int(True) == 1`` would silently accept
    # ``max_archive_bytes: true`` as a 1-byte limit. Reject it explicitly.
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, not a boolean. got={value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer. got={value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be positive. got={parsed}")
    return parsed


def _normalized_extensions(raw: object) -> tuple[str, ...]:
    # Absent (key omitted / empty string) falls back to the default allowlist; an
    # explicitly provided list is honored verbatim, including an empty list, which
    # locks the feature down (fail closed) rather than reverting to the default.
    if raw is None or raw == "":
        return DEFAULT_ALLOWED_EXTENSIONS
    if isinstance(raw, str) or not isinstance(raw, Iterable):
        raise ValueError("uploads.allowed_extensions must be a list of extensions")
    seen: dict[str, None] = {}
    for item in raw:
        text = str(item).strip().lower()
        if not text:
            continue
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

    data = raw if isinstance(raw, Mapping) else {}
    policy = UploadPolicy(
        enabled=normalize_bool(data.get("enabled"), default=False),
        max_archive_bytes=_positive_int(
            data.get("max_archive_bytes"),
            default=25 * _MIB,
            field_name="uploads.max_archive_bytes",
        ),
        max_total_uncompressed_bytes=_positive_int(
            data.get("max_total_uncompressed_bytes"),
            default=200 * _MIB,
            field_name="uploads.max_total_uncompressed_bytes",
        ),
        max_entries=_positive_int(
            data.get("max_entries"),
            default=4000,
            field_name="uploads.max_entries",
        ),
        max_file_bytes=_positive_int(
            data.get("max_file_bytes"),
            default=100 * _MIB,
            field_name="uploads.max_file_bytes",
        ),
        allowed_extensions=_normalized_extensions(data.get("allowed_extensions")),
        max_staged_bytes=_positive_int(
            data.get("max_staged_bytes"),
            default=512 * _MIB,
            field_name="uploads.max_staged_bytes",
        ),
        max_staged_uploads=_positive_int(
            data.get("max_staged_uploads"),
            default=32,
            field_name="uploads.max_staged_uploads",
        ),
        max_pending_per_actor=_positive_int(
            data.get("max_pending_per_actor"),
            default=4,
            field_name="uploads.max_pending_per_actor",
        ),
        max_concurrent_downloads=_positive_int(
            data.get("max_concurrent_downloads"),
            default=4,
            field_name="uploads.max_concurrent_downloads",
        ),
        staging_ttl_seconds=_positive_int(
            data.get("staging_ttl_seconds"),
            default=3600,
            field_name="uploads.staging_ttl_seconds",
        ),
        committed_retention_seconds=_positive_int(
            data.get("committed_retention_seconds"),
            default=86400,
            field_name="uploads.committed_retention_seconds",
        ),
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
    "UploadPolicy",
    "upload_policy_from_mapping",
]
