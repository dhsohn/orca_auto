"""Every field of a generation's ``machine.json`` and where it comes from.

:func:`build_machine_observation` assembles the factory v1 envelope and its
``chemistry/results-bundle`` payload from the normalized job state and the
generation's files:

- ``contract``, ``producer`` (with the package version), ``operation.kind``,
  ``lineage`` and the payload's contract, ``result_kind`` and ``engine`` are
  constants of this module.
- ``operation.id`` is the job ID, else the run ID.
- ``lifecycle`` maps the run status through :func:`machine_lifecycle`; its
  failure code comes from the final reason.
- ``artifacts`` holds one receipt (:func:`artifact_receipt`) per file: the
  selected input, the last ORCA output, the published HTML report and SI block,
  and ``execution_provenance.json``, each with its role and media type from
  :data:`ARTIFACT_ROLES`.
- ``delivery`` and ``handoff`` follow from the lifecycle and whether every
  required receipt is available.
- ``payload.data.summary`` and ``results`` are :func:`report_result_fields`.

``report/publication.py`` writes these bytes once and keeps a terminal
observation immutable; ``tests/contracts/report_verifier.py`` re-derives them.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

from orca_auto import __version__
from orca_auto.core.artifacts import EXECUTION_PROVENANCE_FILE, RUN_REPORT_JSON_FILE
from orca_auto.core.statuses import STATUS_PENDING, STATUS_QUEUED
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.core.utils import normalize_text

from .state_reading import normalized_text
from .statuses import ACTIVE_RUN_STATUS_VALUES, RunStatus

MACHINE_CONTRACT_NAME = "factory/machine-observation"
MACHINE_CONTRACT_VERSION = 1
RESULTS_PAYLOAD_CONTRACT_NAME = "chemistry/results-bundle"
RESULTS_PAYLOAD_CONTRACT_VERSION = 1
PRODUCER_NAME = "orca_auto"
OPERATION_KIND = "chemistry/orca-run"
RESULT_KIND = "engine-run"
ENGINE_NAME = "orca"
EXECUTION_PROVENANCE_ARTIFACT_ID = "execution-provenance"
# Role and media type of each receipt, by artifact ID.
ARTIFACT_ROLES: dict[str, tuple[str, str]] = {
    "input": ("source", "text/plain"),
    "orca-output": ("log", "text/plain"),
    "human-report": ("human-report", "text/html"),
    "supporting-information": ("supporting-information", "text/markdown"),
    EXECUTION_PROVENANCE_ARTIFACT_ID: ("supporting-information", "application/json"),
}

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


def report_json_path(generation_dir: Path) -> Path:
    return generation_dir / RUN_REPORT_JSON_FILE


def machine_lifecycle(status: str) -> tuple[str, str]:
    """Map a run/queue status to the report ``(phase, outcome)`` pair."""

    normalized = status.strip().lower()
    if normalized in {RunStatus.CREATED.value, STATUS_PENDING, STATUS_QUEUED}:
        return "queued", "pending"
    if normalized in ACTIVE_RUN_STATUS_VALUES:
        return "running", "pending"
    if normalized == RunStatus.COMPLETED.value:
        return "finished", "succeeded"
    if normalized == RunStatus.CANCELLED.value:
        return "finished", "cancelled"
    if normalized == RunStatus.FAILED.value:
        return "finished", "failed"
    return "finished", "uncertain"


def report_result_fields(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``summary`` and ``results`` of the results bundle, from the normalized job state."""
    status = _dict(payload.get("status"))
    engine = _dict(payload.get("engine_payload"))
    final = _dict(engine.get("final_result"))
    attempts = engine.get("attempts")
    common = {
        "reason": normalize_text(final.get("reason") or status.get("reason") or ""),
        "analyzer_status": normalize_text(final.get("analyzer_status") or ""),
        "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
    }
    summary = {"status": normalize_text(status.get("state") or ""), **common}
    results = {
        "run_id": normalize_text(engine.get("run_id") or ""),
        **common,
        "resumed": bool(final.get("resumed", False)),
        "skipped_execution": bool(final.get("skipped_execution", False)),
        "runner_error": normalize_text(final.get("runner_error") or ""),
    }
    if _dict(engine.get("execution_provenance")).get("source_inputs"):
        results["execution_provenance_artifact"] = EXECUTION_PROVENANCE_ARTIFACT_ID
    return summary, results


def _receipt(
    generation_dir: Path, artifact_id: str, candidate: Path | None, *, required: bool
) -> dict[str, Any]:
    role, media_type = ARTIFACT_ROLES[artifact_id]
    return artifact_receipt(
        generation_dir, candidate, required=required, role=role, media_type=media_type
    )


def build_machine_observation(
    generation_dir: Path,
    report_payload: Mapping[str, Any],
    *,
    html_path: Path | None = None,
    si_path: Path | None = None,
) -> dict[str, Any]:
    """The ``machine.json`` document of the generation at ``generation_dir``.

    ``report_payload`` is the normalized job state; ``html_path`` and
    ``si_path`` are the report and SI block published in the generation.
    """
    job = _dict(report_payload.get("job"))
    input_payload = _dict(report_payload.get("input"))
    engine_payload = _dict(report_payload.get("engine_payload"))
    final_result = _dict(engine_payload.get("final_result"))
    summary, result_details = report_result_fields(report_payload)
    status = summary["status"]
    reason = summary["reason"]
    phase, outcome = machine_lifecycle(status)
    operation_id = normalized_text(job.get("id")) or normalized_text(engine_payload.get("run_id"))

    primary_path = normalized_text(input_payload.get("primary_path"))
    artifacts: dict[str, dict[str, Any]] = {
        "input": _receipt(
            generation_dir, "input", Path(primary_path) if primary_path else None, required=True
        )
    }
    last_out_path = normalized_text(final_result.get("last_out_path"))
    if last_out_path or outcome == "succeeded":
        artifacts["orca-output"] = _receipt(
            generation_dir,
            "orca-output",
            Path(last_out_path) if last_out_path else None,
            required=outcome == "succeeded",
        )
    for artifact_id, path in (("human-report", html_path), ("supporting-information", si_path)):
        if path is not None:
            artifacts[artifact_id] = _receipt(generation_dir, artifact_id, path, required=False)
    if result_details.get("execution_provenance_artifact"):
        artifacts[EXECUTION_PROVENANCE_ARTIFACT_ID] = _receipt(
            generation_dir,
            EXECUTION_PROVENANCE_ARTIFACT_ID,
            generation_dir / EXECUTION_PROVENANCE_FILE,
            required=True,
        )

    complete = required_delivery_complete(artifacts)
    if phase != "finished":
        handoff_status = "pending"
        delivery_status = "pending"
        handoff_codes: list[str] = []
        delivery_codes: list[str] = []
    else:
        delivery_status = "complete" if complete else "incomplete"
        delivery_codes = [] if complete else ["orca_auto/required_artifact_unavailable"]
        if outcome == "succeeded" and complete:
            handoff_status = "ready"
            handoff_codes = []
        else:
            handoff_status = "blocked"
            handoff_codes = [
                machine_code(
                    PRODUCER_NAME,
                    reason if outcome != "succeeded" else "required_artifact_unavailable",
                    fallback="operation_not_ready",
                )
            ]
    lifecycle_codes = (
        []
        if outcome in {"pending", "succeeded"}
        else [machine_code(PRODUCER_NAME, reason or outcome, fallback="operation_failed")]
    )
    return {
        "contract": {"name": MACHINE_CONTRACT_NAME, "version": MACHINE_CONTRACT_VERSION},
        "producer": {"name": PRODUCER_NAME, "version": __version__},
        "operation": {"id": operation_id, "kind": OPERATION_KIND},
        "lifecycle": {"phase": phase, "outcome": outcome, "codes": lifecycle_codes},
        "handoff": {"status": handoff_status, "codes": handoff_codes},
        "delivery": {"status": delivery_status, "codes": delivery_codes},
        "artifacts": artifacts,
        "lineage": {"trace_id": None, "upstream": []},
        "payload": {
            "contract": {
                "name": RESULTS_PAYLOAD_CONTRACT_NAME,
                "version": RESULTS_PAYLOAD_CONTRACT_VERSION,
            },
            "data": {
                "result_kind": RESULT_KIND,
                "engine": ENGINE_NAME,
                "summary": summary,
                "results": result_details,
                "artifact_refs": sorted(artifacts),
            },
        },
    }


__all__ = [
    "ARTIFACT_ROLES",
    "ENGINE_NAME",
    "EXECUTION_PROVENANCE_ARTIFACT_ID",
    "MACHINE_CONTRACT_NAME",
    "MACHINE_CONTRACT_VERSION",
    "OPERATION_KIND",
    "PRODUCER_NAME",
    "RESULTS_PAYLOAD_CONTRACT_NAME",
    "RESULTS_PAYLOAD_CONTRACT_VERSION",
    "RESULT_KIND",
    "ReceiptDigest",
    "artifact_receipt",
    "build_machine_observation",
    "machine_code",
    "machine_json_bytes",
    "machine_lifecycle",
    "report_json_path",
    "report_result_fields",
    "required_delivery_complete",
]
