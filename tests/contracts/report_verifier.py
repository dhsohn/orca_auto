"""Test-side reader that verifies a published ``machine.json`` against its generation.

Production never reads ``machine.json`` back: the writer publishes it once and
terminal replay preserves it. This reader re-derives every receipt, checks it
against the generation state and the bound input, and returns the state
payload only when all of them agree. The release smoke
(``examples/fake_orca_smoke/run.sh``) and the report tests use it.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import EXECUTION_PROVENANCE_FILE, MAX_RUN_ARTIFACT_JSON_BYTES
from orca_auto.core.confined_io import read_confined_text, require_confined_regular_file
from orca_auto.core.queue.generation import is_visible_generation_name
from orca_auto.core.utils import copy_dict_or_empty as _dict
from orca_auto.orca.generation_validation import is_retired_generation_marker
from orca_auto.orca.machine_observation import (
    ARTIFACT_ROLES,
    ENGINE_NAME,
    EXECUTION_PROVENANCE_ARTIFACT_ID,
    MACHINE_CONTRACT_NAME,
    MACHINE_CONTRACT_VERSION,
    OPERATION_KIND,
    PRODUCER_NAME,
    RESULT_KIND,
    RESULTS_PAYLOAD_CONTRACT_NAME,
    RESULTS_PAYLOAD_CONTRACT_VERSION,
    ReceiptDigest,
    artifact_receipt,
    machine_json_bytes,
    machine_lifecycle,
    report_json_path,
    report_result_fields,
)
from orca_auto.orca.state_reading import (
    _execution_provenance,
    _selected_input_text,
    load_generation_state,
    normalized_text,
    verified_generation_artifact_target,
)

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


def _receipt_binds(digest: ReceiptDigest, receipt: object) -> bool:
    """Whether an ``available`` receipt binds exactly the bytes ``digest`` accumulated.

    Anything else is a refusal: a receipt that is absent, malformed, or
    recorded ``missing``/``invalid`` binds no content at all, so bytes can
    never be shown to be the ones it covers.
    """
    return (
        isinstance(receipt, Mapping)
        and receipt.get("status") == "available"
        and receipt.get("bytes") == digest.size
        and receipt.get("byte_sha256") == digest.hexdigest()
    )


def load_report_json(
    generation_dir: Path,
    *,
    require_consumable_success: bool = False,
) -> dict[str, Any] | None:
    """Load one provenance-verified ORCA report from an exact visible generation."""

    loaded = load_report_json_with_output_receipt(
        generation_dir,
        require_consumable_success=require_consumable_success,
    )
    return None if loaded is None else loaded[0]


def _report_artifact_receipt(
    verified: Mapping[str, VerifiedArtifact],
    artifact_id: str,
    generation_dir: Path,
    candidate: Path | None,
    *,
    required: bool,
) -> dict[str, Any] | None:
    role, media_type = ARTIFACT_ROLES[artifact_id]
    artifact = verified.get(artifact_id)
    if artifact is None:
        return artifact_receipt(
            generation_dir, candidate, required=required, role=role, media_type=media_type
        )
    if candidate is None or generation_dir / candidate != artifact.path:
        return None
    return {**artifact.receipt, "required": required, "role": role, "media_type": media_type}


def _retired_retry_budget_matches(
    results: Mapping[str, Any], engine_payload: Mapping[str, Any]
) -> bool:
    """Whether the observation records the retry budget its generation's state carries.

    Only a retired (pre-4.0) generation carries ``max_retries``, and its
    observation recorded the same value. A current generation and its
    observation carry none.
    """
    if is_retired_generation_marker(engine_payload) or "max_retries" in results:
        return results.get("max_retries") == engine_payload.get("max_retries")
    return True


def load_report_json_with_output_receipt(
    generation_dir: Path,
    *,
    require_consumable_success: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """Load one verified report together with the ``orca-output`` receipt it accepted.

    The second element is the receipt this load re-hashed from disk and found
    equal to the one the machine observation records, so a reader that must
    prove it read the observed bytes can compare its own digest against it
    instead of re-deriving one from a file it opened later. It is ``None`` when
    the generation records no terminal output, and carries a ``missing`` or
    ``invalid`` status when the recorded output is not a file the observation
    could bind — the caller decides what an unavailable receipt means for it.
    """

    raw_generation_dir = generation_dir.expanduser()
    if (
        not raw_generation_dir.is_absolute()
        or raw_generation_dir.is_symlink()
        or not is_visible_generation_name(raw_generation_dir.name)
    ):
        return None
    report_path = report_json_path(raw_generation_dir)
    try:
        resolved_generation_dir = raw_generation_dir.resolve(strict=True)
        generation_before = resolved_generation_dir.stat()
        before = report_path.lstat()
        observation = json.loads(
            read_confined_text(
                resolved_generation_dir,
                report_path,
                label="ORCA generation report",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if (
        raw_generation_dir != resolved_generation_dir
        or not stat.S_ISDIR(generation_before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not isinstance(observation, dict)
    ):
        return None
    result_data = results_payload_from_observation(observation)
    if result_data is None:
        return None
    if (
        observation.get("producer", {}).get("name") != PRODUCER_NAME
        or observation.get("operation", {}).get("kind") != OPERATION_KIND
        or result_data.get("result_kind") != RESULT_KIND
        or result_data.get("engine") != ENGINE_NAME
    ):
        return None
    loaded_state = load_generation_state(resolved_generation_dir)
    if loaded_state is None:
        return None
    payload, _state = loaded_state
    job = _dict(payload.get("job"))
    status = _dict(payload.get("status"))
    input_payload = _dict(payload.get("input"))
    engine_payload = _dict(payload.get("engine_payload"))
    final_result = _dict(engine_payload.get("final_result"))
    summary = _dict(result_data.get("summary"))
    results = _dict(result_data.get("results"))
    operation_id = normalized_text(observation.get("operation", {}).get("id"))
    expected_phase, expected_outcome = machine_lifecycle(normalized_text(status.get("state")))
    lifecycle = _dict(observation.get("lifecycle"))
    expected_summary, expected_results = report_result_fields(payload)
    if (
        operation_id
        not in {
            normalized_text(job.get("id")),
            normalized_text(engine_payload.get("run_id")),
        }
        or lifecycle.get("phase") != expected_phase
        or lifecycle.get("outcome") != expected_outcome
        or any(summary.get(key) != value for key, value in expected_summary.items())
        or any(results.get(key) != value for key, value in expected_results.items())
        or not _retired_retry_budget_matches(results, engine_payload)
        or results.get("execution_provenance_artifact")
        != expected_results.get("execution_provenance_artifact")
    ):
        return None
    artifacts = observation.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    outcome = expected_outcome
    if require_consumable_success and outcome == "succeeded":
        handoff = _dict(observation.get("handoff"))
        delivery = _dict(observation.get("delivery"))
        if handoff.get("status") != "ready" or delivery.get("status") != "complete":
            return None
    target = verified_generation_artifact_target(resolved_generation_dir.parent, payload)
    if target is None or target[0] != resolved_generation_dir:
        return None
    provenance = _execution_provenance(payload)
    bound_selected_identity = provenance.get("bound_selected_identity")
    selected_text = _selected_input_text(payload)
    if not isinstance(bound_selected_identity, Mapping) or not selected_text:
        return None
    try:
        selected = require_confined_regular_file(
            resolved_generation_dir,
            Path(selected_text).expanduser(),
            label="ORCA report selected input",
        )
        # Hash the input after ownership checks and the other artifacts, as the
        # original final input verification did. Timestamps alone cannot expose
        # every same-size write within one filesystem clock tick.
        verified_artifacts = read_verified_artifacts(
            observation, resolved_generation_dir, last_path=selected
        )
        if verified_artifacts is None:
            return None
        verified_input = verified_artifacts.get("input")
        if (
            verified_input is None
            or verified_input.path != selected
            or {
                "path": str(selected),
                "sha256": verified_input.receipt["byte_sha256"],
                "size_bytes": verified_input.receipt["bytes"],
            }
            != dict(bound_selected_identity)
        ):
            return None
        if expected_results.get("execution_provenance_artifact"):
            expected_provenance = _report_artifact_receipt(
                verified_artifacts,
                EXECUTION_PROVENANCE_ARTIFACT_ID,
                resolved_generation_dir,
                resolved_generation_dir / EXECUTION_PROVENANCE_FILE,
                required=True,
            )
            # A self-consistent replacement receipt must still match the
            # submission evidence persisted in this generation's state.
            expected_digest = ReceiptDigest()
            expected_digest.update(machine_json_bytes(provenance))
            if (
                EXECUTION_PROVENANCE_ARTIFACT_ID not in result_data["artifact_refs"]
                or artifacts.get(EXECUTION_PROVENANCE_ARTIFACT_ID) != expected_provenance
                or not _receipt_binds(expected_digest, expected_provenance)
            ):
                return None
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    expected_input = _report_artifact_receipt(
        verified_artifacts,
        "input",
        resolved_generation_dir,
        Path(normalized_text(input_payload.get("primary_path"))),
        required=True,
    )
    if artifacts.get("input") != expected_input:
        return None
    last_out_path = normalized_text(final_result.get("last_out_path"))
    accepted_output_receipt: dict[str, Any] | None = None
    if last_out_path or outcome == "succeeded":
        expected_output = _report_artifact_receipt(
            verified_artifacts,
            "orca-output",
            resolved_generation_dir,
            Path(last_out_path) if last_out_path else None,
            required=outcome == "succeeded",
        )
        if artifacts.get("orca-output") != expected_output:
            return None
        accepted_output_receipt = expected_output
    try:
        after = report_path.lstat()
        generation_details = resolved_generation_dir.stat()
    except OSError:
        return None
    if (
        (
            before.st_dev,
            before.st_ino,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or (
            int(generation_before.st_dev),
            int(generation_before.st_ino),
        )
        != (
            int(generation_details.st_dev),
            int(generation_details.st_ino),
        )
        or target[1] != (int(generation_details.st_dev), int(generation_details.st_ino))
        or not all(artifact.is_unchanged() for artifact in verified_artifacts.values())
    ):
        return None
    return payload, accepted_output_receipt
