from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

# The external factory/machine-observation contract fixes this filename, and the
# internal per-run report artifact happens to use the same one. They are two
# separate contracts that coincide, so this is spelled out rather than aliased to
# core.artifacts.RUN_REPORT_JSON_FILE: renaming the run report must not silently
# rename the file the external contract names.
MACHINE_OBSERVATION_FILE = "machine.json"

MACHINE_CONTRACT_NAME = "factory/machine-observation"
MACHINE_CONTRACT_VERSION = 1
RESULTS_PAYLOAD_CONTRACT_NAME = "chemistry/results-bundle"
RESULTS_PAYLOAD_CONTRACT_VERSION = 1

_TOP_LEVEL_FIELDS = frozenset(
    {
        "contract",
        "producer",
        "operation",
        "lifecycle",
        "handoff",
        "delivery",
        "artifacts",
        "lineage",
        "payload",
    }
)
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

    Both directions of a receipt measure content here. ``artifact_receipt``
    fills one in while writing a receipt, and a reader that must prove the
    bytes it consumed are the ones an accepted receipt binds fills one in from
    exactly the chunks it read and asks ``matches``. A second hashing routine
    on the reading side could drift from this one — a different chunk size is
    harmless, but a different algorithm or a size counted differently would
    silently accept or reject the wrong bytes.
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

    def matches(self, receipt: object) -> bool:
        """Whether an ``available`` receipt binds exactly the accumulated bytes.

        Anything else is a refusal: a receipt that is absent, malformed, or
        recorded ``missing``/``invalid`` binds no content at all, so bytes can
        never be shown to be the ones it covers.
        """
        return (
            isinstance(receipt, Mapping)
            and receipt.get("status") == "available"
            and receipt.get("bytes") == self._size
            and receipt.get("byte_sha256") == self.hexdigest()
        )


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


def results_payload_from_observation(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if set(payload) != _TOP_LEVEL_FIELDS:
        return None
    contract = payload.get("contract")
    if contract != {"name": MACHINE_CONTRACT_NAME, "version": MACHINE_CONTRACT_VERSION}:
        return None
    producer = payload.get("producer")
    operation = payload.get("operation")
    lifecycle = payload.get("lifecycle")
    handoff = payload.get("handoff")
    delivery = payload.get("delivery")
    artifacts = payload.get("artifacts")
    lineage = payload.get("lineage")
    domain = payload.get("payload")
    if (
        not isinstance(producer, Mapping)
        or not isinstance(operation, Mapping)
        or not isinstance(lifecycle, Mapping)
        or not isinstance(handoff, Mapping)
        or not isinstance(delivery, Mapping)
        or not isinstance(artifacts, Mapping)
        or not isinstance(lineage, Mapping)
        or not isinstance(domain, Mapping)
    ):
        return None
    domain_contract = domain.get("contract")
    data = domain.get("data")
    if domain_contract != {
        "name": RESULTS_PAYLOAD_CONTRACT_NAME,
        "version": RESULTS_PAYLOAD_CONTRACT_VERSION,
    } or not isinstance(data, dict):
        return None
    if set(data) != {"result_kind", "engine", "summary", "results", "artifact_refs"}:
        return None
    if not isinstance(data.get("summary"), dict) or not isinstance(data.get("results"), dict):
        return None
    refs = data.get("artifact_refs")
    if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
        return None
    if not set(refs).issubset(artifacts):
        return None
    return data


def _file_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


@dataclass(frozen=True)
class VerifiedArtifact:
    """Content read in this verification, bound to a file that can be rechecked."""

    path: Path
    receipt: dict[str, Any]
    file_identity: tuple[int, ...]

    def is_unchanged(self) -> bool:
        try:
            return (
                self.path.resolve(strict=True) == self.path
                and _file_identity(self.path.lstat()) == self.file_identity
            )
        except (OSError, RuntimeError):
            return False


def read_verified_artifacts(
    payload: Mapping[str, Any], package_root: Path, *, last_path: Path | None = None
) -> dict[str, VerifiedArtifact] | None:
    """Hash files once, with all references to ``last_path`` checked last."""
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    verified: dict[str, VerifiedArtifact] = {}
    by_path: dict[Path, VerifiedArtifact] = {}
    items = list(artifacts.items())
    if last_path is not None:
        # Sort by path, not artifact id: another receipt may alias the final input.
        def is_last(item: tuple[str, Any]) -> bool:
            receipt = item[1]
            path = receipt.get("path") if isinstance(receipt, Mapping) else None
            return isinstance(path, str) and package_root / path == last_path

        items.sort(key=is_last)
    for artifact_id, receipt in items:
        if not isinstance(receipt, Mapping):
            return None
        if receipt.get("status") != "available":
            continue
        path = receipt.get("path")
        role = receipt.get("role")
        media_type = receipt.get("media_type")
        expected_size = receipt.get("bytes")
        expected_hash = receipt.get("byte_sha256")
        if (
            not isinstance(path, str)
            or not path
            or not isinstance(role, str)
            or not role
            or not isinstance(media_type, str)
            or not media_type
        ):
            return None
        try:
            candidate = package_root.resolve(strict=True) / path
            previous = by_path.get(candidate)
            if previous is None:
                identity = _file_identity(candidate.lstat())
                observed = artifact_receipt(
                    package_root,
                    candidate,
                    required=bool(receipt.get("required")),
                    role=role,
                    media_type=media_type,
                )
            else:
                identity = previous.file_identity
                observed = {
                    **previous.receipt,
                    "required": bool(receipt.get("required")),
                    "role": role,
                    "media_type": media_type,
                }
        except (OSError, RuntimeError, ValueError):
            return None
        if observed.get("status") != "available":
            return None
        if observed.get("bytes") != expected_size or observed.get("byte_sha256") != expected_hash:
            return None
        artifact = VerifiedArtifact(candidate, observed, identity)
        if not artifact.is_unchanged():
            return None
        verified[artifact_id] = artifact
        by_path[candidate] = artifact
    return verified


__all__ = [
    "MACHINE_CONTRACT_NAME",
    "MACHINE_CONTRACT_VERSION",
    "MACHINE_OBSERVATION_FILE",
    "RESULTS_PAYLOAD_CONTRACT_NAME",
    "RESULTS_PAYLOAD_CONTRACT_VERSION",
    "ReceiptDigest",
    "VerifiedArtifact",
    "artifact_receipt",
    "machine_code",
    "machine_json_bytes",
    "required_delivery_complete",
    "results_payload_from_observation",
    "read_verified_artifacts",
]
