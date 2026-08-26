from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from orca_auto import __version__
from orca_auto.core.artifacts import (
    MAX_RUN_ARTIFACT_JSON_BYTES,
    WORKFLOW_REPORT_HTML_FILE,
    WORKFLOW_SI_MD_FILE,
)
from orca_auto.core.engine_process import read_confined_text
from orca_auto.core.machine_observation import (
    MACHINE_CONTRACT_NAME,
    MACHINE_CONTRACT_VERSION,
    MACHINE_OBSERVATION_FILE,
    RESULTS_PAYLOAD_CONTRACT_NAME,
    RESULTS_PAYLOAD_CONTRACT_VERSION,
    artifact_receipt,
    machine_code,
    machine_json_bytes,
    required_delivery_complete,
    results_payload_from_observation,
)
from orca_auto.core.statuses import is_workflow_terminal_status
from orca_auto.core.utils.persistence import atomic_write_text
from orca_auto.flow.workflow.report_collection import collect_workflow_report_data


def _text(value: object) -> str:
    return str(value or "").strip()


def _workflow_outcome(status: str) -> str:
    if status == "completed":
        return "succeeded"
    if status == "cancelled":
        return "cancelled"
    if status in {"failed", "cancel_failed", "submission_failed"}:
        return "failed"
    return "uncertain"


def _upstream_orca_observations(workspace_dir: Path, report_data: Any) -> list[dict[str, Any]]:
    upstream: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        resolved_workspace = workspace_dir.expanduser().resolve(strict=True)
    except OSError:
        return upstream
    for machine_value in report_data.consumed_orca_machine_paths:
        try:
            machine_path = Path(machine_value)
        except TypeError:
            continue
        if (
            not machine_path.is_absolute()
            or machine_path.name != MACHINE_OBSERVATION_FILE
            or machine_path.is_symlink()
        ):
            # The writer never publishes through a symlink; do not accept one
            # as lineage either.
            continue
        try:
            resolved_machine = machine_path.resolve(strict=True)
        except OSError:
            continue
        if resolved_machine.name != MACHINE_OBSERVATION_FILE or not resolved_machine.is_relative_to(
            resolved_workspace
        ):
            continue
        try:
            payload_text = read_confined_text(
                resolved_machine.parent,
                resolved_machine,
                label="upstream ORCA machine observation",
                max_bytes=MAX_RUN_ARTIFACT_JSON_BYTES,
            )
            payload_bytes = payload_text.encode("utf-8")
            payload = json.loads(payload_text)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or results_payload_from_observation(payload) is None:
            continue
        producer = payload.get("producer")
        operation = payload.get("operation")
        lifecycle = payload.get("lifecycle")
        if (
            not isinstance(producer, dict)
            or not isinstance(operation, dict)
            or not isinstance(lifecycle, dict)
            or lifecycle.get("phase") != "finished"
        ):
            continue
        key = (
            _text(producer.get("name")),
            _text(operation.get("id")),
            hashlib.sha256(payload_bytes).hexdigest(),
        )
        if not all(key) or key in seen:
            continue
        seen.add(key)
        upstream.append(
            {
                "producer": {
                    "name": key[0],
                    "version": _text(producer.get("version")),
                },
                "operation_id": key[1],
                "byte_sha256": key[2],
            }
        )
    return upstream


def build_workflow_machine_observation(
    workspace_dir: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    status = _text(payload.get("status")).lower()
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    if (
        not is_workflow_terminal_status(status)
        or bool(metadata.get("final_child_sync_pending"))
        or bool(metadata.get("si_publish_pending"))
    ):
        return None

    report_data = collect_workflow_report_data(workspace_dir, payload)
    has_orca_stages = any(row.stage_kind == "orca_stage" for row in report_data.stage_rows)
    artifacts = {
        "human-report": artifact_receipt(
            workspace_dir,
            workspace_dir / WORKFLOW_REPORT_HTML_FILE,
            required=True,
            role="human-report",
            media_type="text/html",
        )
    }
    if has_orca_stages:
        artifacts["supporting-information"] = artifact_receipt(
            workspace_dir,
            workspace_dir / WORKFLOW_SI_MD_FILE,
            required=True,
            role="supporting-information",
            media_type="text/markdown",
        )
    complete = required_delivery_complete(artifacts)
    outcome = _workflow_outcome(status)
    error_reason = report_data.workflow_error_reason or report_data.workflow_error_message
    lifecycle_codes = (
        []
        if outcome == "succeeded"
        else [machine_code("orca_auto", error_reason or status, fallback="workflow_failed")]
    )
    delivery_status = "complete" if complete else "incomplete"
    delivery_codes = [] if complete else ["orca_auto/required_artifact_unavailable"]
    si_receipt = artifacts.get("supporting-information")
    if (
        bool(metadata.get("si_publish_blocked"))
        and isinstance(si_receipt, Mapping)
        and si_receipt.get("status") == "available"
    ):
        # SI regeneration is blocked, so the pinned workflow_si.md is the last
        # known-good file from an earlier advance and may predate the final
        # payload. Publication proceeds, but consumers get the signal. A
        # missing SI already reports required_artifact_unavailable instead.
        delivery_codes.append("orca_auto/si_publication_blocked")
    if outcome == "succeeded" and complete:
        handoff_status = "ready"
        handoff_codes: list[str] = []
    else:
        handoff_status = "blocked"
        handoff_codes = [
            machine_code(
                "orca_auto",
                error_reason if outcome != "succeeded" else "required_artifact_unavailable",
                fallback="workflow_not_ready",
            )
        ]
    operation_id = report_data.workflow_id
    results = {
        "template_name": report_data.template_name,
        "reaction_key": report_data.reaction_key,
        "stage_rows": [asdict(row) for row in report_data.stage_rows],
        "failure_rows": [
            {
                "stage_id": row.stage_id,
                "engine": row.engine,
                "status": row.status,
                "reason": row.reason,
                "explanation": row.explanation,
            }
            for row in report_data.failure_rows
        ],
        "orca_results": [
            {
                "stage_id": row.stage_id,
                "label": row.label,
                "status": row.status,
                "reason": row.reason,
                "energy": row.energy,
                "rel_kcal": row.rel_kcal,
                "imaginary_count": row.imaginary_count,
                "attempt_count": row.attempt_count,
            }
            for row in report_data.orca_results
        ],
        "crest_conformer_total": report_data.crest_conformer_total,
        "xtb_candidate_total": report_data.xtb_candidate_total,
    }
    return {
        "contract": {"name": MACHINE_CONTRACT_NAME, "version": MACHINE_CONTRACT_VERSION},
        "producer": {"name": "orca_auto", "version": __version__},
        "operation": {"id": operation_id, "kind": "chemistry/workflow"},
        "lifecycle": {"phase": "finished", "outcome": outcome, "codes": lifecycle_codes},
        "handoff": {"status": handoff_status, "codes": handoff_codes},
        "delivery": {"status": delivery_status, "codes": delivery_codes},
        "artifacts": artifacts,
        "lineage": {
            "trace_id": operation_id,
            "upstream": _upstream_orca_observations(workspace_dir, report_data),
        },
        "payload": {
            "contract": {
                "name": RESULTS_PAYLOAD_CONTRACT_NAME,
                "version": RESULTS_PAYLOAD_CONTRACT_VERSION,
            },
            "data": {
                "result_kind": "workflow",
                "engine": "workflow",
                "summary": {
                    "status": status,
                    "stage_count": len(report_data.stage_rows),
                    "failure_count": len(report_data.failure_rows),
                },
                "results": results,
                "artifact_refs": sorted(artifacts),
            },
        },
    }


def write_workflow_machine_observation(
    workspace_dir: Path,
    payload: Mapping[str, Any],
) -> Path | None:
    observation = build_workflow_machine_observation(workspace_dir, payload)
    if observation is None:
        return None
    path = workspace_dir / MACHINE_OBSERVATION_FILE
    if path.is_symlink():
        raise ValueError(f"workflow machine observation is unsafe: {path}")
    observation_bytes = machine_json_bytes(observation)
    if path.exists():
        if path.read_bytes() == observation_bytes:
            return path
        raise RuntimeError(f"terminal workflow machine observation is immutable: {path}")
    atomic_write_text(path, observation_bytes.decode("utf-8"))
    return path


__all__ = [
    "build_workflow_machine_observation",
    "write_workflow_machine_observation",
]
