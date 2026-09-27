from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

MACHINE_CONTRACT_NAME = "factory/machine-observation"
MACHINE_CONTRACT_VERSION = 1
RESULTS_PAYLOAD_CONTRACT_NAME = "chemistry/results-bundle"
RESULTS_PAYLOAD_CONTRACT_VERSION = 1

_CODE_TOKEN_RE = re.compile(r"[^a-z0-9._-]+")
_MAX_CODE_LENGTH = 200
_CODE_HASH_LENGTH = 16
_HASH_CHUNK_BYTES = 1024 * 1024


def machine_code(namespace: str, value: object, *, fallback: str) -> str:
    token = _CODE_TOKEN_RE.sub("-", str(value or "").strip().lower()).strip("-._")
    token = token or fallback
    code = f"{namespace}/{token}"
    if len(code) <= _MAX_CODE_LENGTH:
        return code
    suffix = f"-{hashlib.sha256(code.encode('utf-8')).hexdigest()[:_CODE_HASH_LENGTH]}"
    token_limit = _MAX_CODE_LENGTH - len(namespace) - 1 - len(suffix)
    readable = token[:token_limit].rstrip("-._")
    return f"{namespace}/{readable}{suffix}"


def machine_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


class ReceiptDigest:
    """One artifact receipt's ``bytes`` and ``byte_sha256``, accumulated over chunks.

    ``artifact_receipt`` fills one in while writing a receipt. A reader that
    checks a receipt hashes with this class too: a second hashing routine could
    drift from this one — a different chunk size is harmless, but a different
    algorithm or a size counted differently would silently accept or reject the
    wrong bytes.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)
        self._size += len(chunk)

    def consume(self, stream: IO[bytes]) -> None:
        """Accumulate everything left in one already-open binary stream."""
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            self.update(chunk)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _unavailable_receipt(
    *, required: bool, role: str, media_type: str, status: str
) -> dict[str, Any]:
    return {
        "status": status,
        "required": bool(required),
        "role": role,
        "path": None,
        "media_type": media_type,
        "bytes": None,
        "byte_sha256": None,
    }


def artifact_receipt(
    package_root: Path,
    candidate: Path | None,
    *,
    required: bool,
    role: str,
    media_type: str,
) -> dict[str, Any]:
    if candidate is None:
        return _unavailable_receipt(
            required=required,
            role=role,
            media_type=media_type,
            status="missing",
        )
    try:
        root = package_root.resolve(strict=True)
        raw_candidate = candidate if candidate.is_absolute() else root / candidate
        before = raw_candidate.lstat()
        resolved = raw_candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
        if raw_candidate != resolved or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError
        content = ReceiptDigest()
        with resolved.open("rb") as stream:
            content.consume(stream)
        after = resolved.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or content.size != before.st_size:
            raise ValueError
    except FileNotFoundError:
        return _unavailable_receipt(
            required=required,
            role=role,
            media_type=media_type,
            status="missing",
        )
    except (OSError, ValueError):
        return _unavailable_receipt(
            required=required,
            role=role,
            media_type=media_type,
            status="invalid",
        )
    return {
        "status": "available",
        "required": bool(required),
        "role": role,
        "path": relative.as_posix(),
        "media_type": media_type,
        "bytes": content.size,
        "byte_sha256": content.hexdigest(),
    }


def required_delivery_complete(artifacts: Mapping[str, Mapping[str, Any]]) -> bool:
    return all(
        receipt.get("status") == "available"
        for receipt in artifacts.values()
        if bool(receipt.get("required"))
    )


__all__ = [
    "MACHINE_CONTRACT_NAME",
    "MACHINE_CONTRACT_VERSION",
    "RESULTS_PAYLOAD_CONTRACT_NAME",
    "RESULTS_PAYLOAD_CONTRACT_VERSION",
    "ReceiptDigest",
    "artifact_receipt",
    "machine_code",
    "machine_json_bytes",
    "required_delivery_complete",
]
