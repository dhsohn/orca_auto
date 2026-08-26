from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
        digest = hashlib.sha256()
        size = 0
        with resolved.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
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
        ) or size != before.st_size:
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
        "bytes": size,
        "byte_sha256": digest.hexdigest(),
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


def verify_available_artifacts(payload: Mapping[str, Any], package_root: Path) -> bool:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return False
    for receipt in artifacts.values():
        if not isinstance(receipt, Mapping):
            return False
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
            return False
        observed = artifact_receipt(
            package_root,
            Path(path),
            required=bool(receipt.get("required")),
            role=role,
            media_type=media_type,
        )
        if observed.get("status") != "available":
            return False
        if observed.get("bytes") != expected_size or observed.get("byte_sha256") != expected_hash:
            return False
    return True


__all__ = [
    "MACHINE_CONTRACT_NAME",
    "MACHINE_CONTRACT_VERSION",
    "MACHINE_OBSERVATION_FILE",
    "RESULTS_PAYLOAD_CONTRACT_NAME",
    "RESULTS_PAYLOAD_CONTRACT_VERSION",
    "artifact_receipt",
    "machine_code",
    "machine_json_bytes",
    "required_delivery_complete",
    "results_payload_from_observation",
    "verify_available_artifacts",
]
