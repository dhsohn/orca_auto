from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WorkflowTaskOrcaMutationMixin:
    def update_orca_contract_payload(
        self: Any,
        contract: Any,
        normalize_text: Callable[[Any], str],
    ) -> None:
        payload = self.payload(None)
        fields = {
            "selected_inp": contract.selected_inp or normalize_text(payload.get("selected_inp")),
        }
        for key in ("selected_input_xyz", "last_out_path", "optimized_xyz_path"):
            value = getattr(contract, key)
            if value:
                fields[key] = value
        self.update_payload(fields)

    def set_orca_latest_attempt_paths(
        self: Any,
        attempt: dict[str, Any],
        normalize_text: Callable[[Any], str],
    ) -> None:
        self.update_payload(
            {
                "orca_latest_attempt_inp": normalize_text(attempt.get("inp_path")),
                "orca_latest_attempt_out": normalize_text(attempt.get("out_path")),
            }
        )


class WorkflowTaskCrestMutationMixin:
    def record_crest_job_materialization(
        self: Any, *, job_dir: Path | str, input_target: Path | str
    ) -> None:
        self.update_payload(
            {
                "job_dir": str(job_dir),
                "selected_input_xyz": str(input_target),
            }
        )
        self.update_enqueue_payload({"job_dir": str(job_dir)})

    def update_crest_contract_payload(self: Any, contract: Any) -> None:
        self.set_payload_field("selected_input_xyz", contract.selected_input_xyz)


class WorkflowTaskXtbMutationMixin:
    def set_selected_input_xyz(self: Any, value: Any) -> None:
        self.set_payload_field("selected_input_xyz", value)

    def record_xtb_path_job_payload(
        self: Any,
        *,
        recipe: dict[str, Any],
        job_dir: Path | str,
        reactant_target: Path | str,
        product_target: Path | str,
        attempt_number: int,
        reaction_key: str,
        normalize_text: Callable[[Any], str],
    ) -> None:
        self.update_payload(
            {
                "job_dir": str(job_dir),
                "selected_input_xyz": str(reactant_target),
                "secondary_input_xyz": str(product_target),
                "xtb_active_attempt_number": int(attempt_number),
                "xtb_retry_recipe_id": normalize_text(recipe.get("recipe_id")),
            }
        )
        self.update_enqueue_payload({"job_dir": str(job_dir), "reaction_key": reaction_key})


class WorkflowStageOrcaMutationMixin:
    def update_orca_contract_metadata(
        self: Any,
        contract: Any,
        normalize_text: Callable[[Any], str],
    ) -> None:
        metadata = self.metadata(None)
        self.update_metadata(
            {
                "queue_id": contract.queue_id or normalize_text(metadata.get("queue_id")),
                "run_id": contract.run_id or normalize_text(metadata.get("run_id")),
                "queue_status": contract.queue_status,
                "cancel_requested": bool(contract.cancel_requested),
                "latest_known_path": contract.latest_known_path,
                "optimized_xyz_path": contract.optimized_xyz_path,
                "analyzer_status": contract.analyzer_status,
                "reason": getattr(contract, "reason", ""),
                "completed_at": contract.completed_at,
                "state_status": contract.state_status,
                "attempt_count": contract.attempt_count,
                "max_retries": contract.max_retries,
                "orca_attempts": [dict(item) for item in contract.attempts],
                "orca_final_result": dict(contract.final_result),
            }
        )

    def update_orca_attempt_metadata(
        self: Any,
        contract: Any,
        task_view: Any,
        normalize_text: Callable[[Any], str],
    ) -> None:
        metadata = self.metadata(None)
        if contract.state_status in {"running", "retrying"}:
            metadata["orca_current_attempt_number"] = max(0, contract.attempt_count)
        elif contract.attempts:
            metadata["orca_current_attempt_number"] = contract.attempts[-1].get("attempt_number")
        else:
            metadata.pop("orca_current_attempt_number", None)

        if contract.attempts:
            last_attempt = contract.attempts[-1]
            metadata["orca_latest_attempt_number"] = last_attempt.get("attempt_number")
            metadata["orca_latest_attempt_status"] = last_attempt.get("analyzer_status")
            task_view.set_orca_latest_attempt_paths(last_attempt, normalize_text)
            return

        metadata.pop("orca_latest_attempt_number", None)
        metadata.pop("orca_latest_attempt_status", None)


class WorkflowStageCrestMutationMixin:
    def update_crest_contract_metadata(self: Any, contract: Any) -> None:
        self.update_metadata(
            {
                "child_job_id": contract.job_id,
                "latest_known_path": contract.latest_known_path,
                "reason": getattr(contract, "reason", ""),
            }
        )

    def set_crest_conformer_artifacts(self: Any, contract: Any) -> None:
        self.set_output_artifacts(
            [
                {
                    "kind": "crest_conformer",
                    "path": path,
                    "selected": index == 1,
                    "metadata": {"rank": index, "mode": contract.mode},
                }
                for index, path in enumerate(contract.retained_conformer_paths, start=1)
            ]
        )


class WorkflowStageXtbMutationMixin:
    def update_xtb_contract_metadata(self: Any, contract: Any) -> None:
        self.update_metadata(
            {
                "child_job_id": contract.job_id,
                "latest_known_path": contract.latest_known_path,
                "reason": contract.reason,
            }
        )

    def update_xtb_attempt_record(
        self: Any,
        attempt_number: int,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        record = self.xtb_attempt_record(attempt_number)
        record.update(fields)
        return record

    def set_xtb_handoff_retry_state(
        self: Any,
        *,
        retries_used: int,
        retry_limit: int,
    ) -> None:
        self.update_metadata(
            {
                "xtb_handoff_retries_used": retries_used,
                "xtb_handoff_retry_limit": retry_limit,
            }
        )

    def set_xtb_handoff_retrying(
        self: Any,
        *,
        retry_limit: int,
        retries_used: int | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "reaction_handoff_status": "retrying",
            "xtb_handoff_retry_limit": retry_limit,
        }
        if retries_used is not None:
            fields["xtb_handoff_retries_used"] = retries_used
        self.update_metadata(fields)

    def xtb_attempt_rows(self: Any) -> list[dict[str, Any]]:
        metadata = self.metadata(None)
        attempts = metadata.get("xtb_attempts")
        if isinstance(attempts, list):
            filtered = [item for item in attempts if isinstance(item, dict)]
            metadata["xtb_attempts"] = filtered
            return filtered
        metadata["xtb_attempts"] = []
        return metadata["xtb_attempts"]

    def xtb_attempt_record(self: Any, attempt_number: int) -> dict[str, Any]:
        rows = self.xtb_attempt_rows()
        target_number = int(attempt_number)
        for row in rows:
            if _safe_int(row.get("attempt_number"), default=-1) == target_number:
                return row
        record = {"attempt_number": target_number}
        rows.append(record)
        rows.sort(key=lambda item: _safe_int(item.get("attempt_number"), default=0))
        return record

    def record_xtb_path_job_metadata(
        self: Any,
        *,
        recipe: dict[str, Any],
        attempt_number: int,
        normalize_text: Callable[[Any], str],
    ) -> None:
        self.update_metadata(
            {
                "xtb_active_attempt_number": int(attempt_number),
                "xtb_retry_recipe_id": normalize_text(recipe.get("recipe_id")),
                "xtb_retry_recipe_label": normalize_text(recipe.get("recipe_label")),
            }
        )

    def record_xtb_path_attempt(
        self: Any,
        *,
        recipe: dict[str, Any],
        job_dir: Path | str,
        manifest_path: Path | str,
        xcontrol_path: Path | str,
        namespace: str,
        reaction_key: str,
        attempt_number: int,
        normalize_text: Callable[[Any], str],
    ) -> None:
        self.update_xtb_attempt_record(
            attempt_number,
            {
                "attempt_number": int(attempt_number),
                "recipe_id": normalize_text(recipe.get("recipe_id")),
                "recipe_label": normalize_text(recipe.get("recipe_label")),
                "job_dir": str(job_dir),
                "manifest_path": str(manifest_path),
                "xcontrol_path": str(xcontrol_path),
                "namespace": namespace,
                "reaction_key": reaction_key,
            },
        )

    def xtb_current_attempt_number(self: Any) -> int:
        metadata = self.metadata(None)
        current = _safe_int(metadata.get("xtb_active_attempt_number"), default=-1)
        if current >= 0:
            return current
        attempts = self.xtb_attempt_rows()
        if attempts:
            return max(_safe_int(item.get("attempt_number"), default=0) for item in attempts)
        return 0

    def set_reaction_handoff(self: Any, handoff: dict[str, str]) -> None:
        if not handoff.get("status"):
            return
        metadata = self.metadata(None)
        metadata["reaction_handoff_status"] = handoff["status"]
        for source_key, metadata_key in (
            ("reason", "reaction_handoff_reason"),
            ("message", "reaction_handoff_message"),
            ("artifact_path", "reaction_handoff_artifact_path"),
        ):
            value = handoff.get(source_key, "")
            if value:
                metadata[metadata_key] = value
            else:
                metadata.pop(metadata_key, None)


__all__ = [
    "WorkflowStageCrestMutationMixin",
    "WorkflowStageOrcaMutationMixin",
    "WorkflowStageXtbMutationMixin",
    "WorkflowTaskCrestMutationMixin",
    "WorkflowTaskOrcaMutationMixin",
    "WorkflowTaskXtbMutationMixin",
]
