from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from orca_auto.core.artifacts import CREST_PRIMARY_ENSEMBLE_NAMES


class WorkflowTaskOrcaMutationMixin:
    def update_orca_contract_payload(
        self: Any,
        contract: Any,
        normalize_text: Callable[[Any], str],
    ) -> None:
        payload = self.payload()
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


class WorkflowStageOrcaMutationMixin:
    def update_orca_contract_metadata(
        self: Any,
        contract: Any,
        normalize_text: Callable[[Any], str],
    ) -> None:
        metadata = self.metadata()
        self.update_metadata(
            {
                "queue_id": contract.queue_id or normalize_text(metadata.get("queue_id")),
                "run_id": contract.run_id or normalize_text(metadata.get("run_id")),
                "queue_status": contract.queue_status,
                "cancel_requested": bool(contract.cancel_requested),
                "latest_known_path": contract.latest_known_path,
                "optimized_xyz_path": contract.optimized_xyz_path,
                "analyzer_status": contract.analyzer_status,
                # Preserve like queue_id/run_id: a submission-failed stage has
                # no run, so the contract loader returns an unknown contract
                # with an empty reason every tick, and writing that "" would
                # erase the rejection reason the submission recorder just put
                # on the stage. A contract with a real reason still wins.
                "reason": getattr(contract, "reason", "") or normalize_text(metadata.get("reason")),
                "completed_at": contract.completed_at,
                "state_status": contract.state_status,
                "attempt_count": contract.attempt_count,
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
        metadata = self.metadata()
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
        rejected = [dict(item) for item in getattr(contract, "rejected_retained_outputs", ())]
        refused_primary = any(row.get("name") in CREST_PRIMARY_ENSEMBLE_NAMES for row in rejected)
        retained_primary = any(
            Path(str(path)).name in CREST_PRIMARY_ENSEMBLE_NAMES
            for path in getattr(contract, "retained_conformer_paths", ())
        )
        self.update_metadata(
            {
                "child_job_id": contract.job_id,
                "latest_known_path": contract.latest_known_path,
                "reason": getattr(contract, "reason", ""),
                "crest_rejected_retained_outputs": rejected,
                # True only when a primary ensemble was refused AND no other
                # primary reached the handoff, i.e. the stage passed on the
                # rotamer and single-best files alone. A refusal by itself does
                # not mean that: the two primary names can both be present in
                # one job directory, so a primary can survive its sibling's
                # refusal. The retained frame count cannot answer this either —
                # crest_rotamers.xyz keeps the count up either way — which is
                # why the question is asked by file name and not by count. Which
                # file was refused is in the rows above.
                "crest_no_primary_ensemble_retained": refused_primary and not retained_primary,
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


__all__ = [
    "WorkflowStageCrestMutationMixin",
    "WorkflowStageOrcaMutationMixin",
    "WorkflowTaskCrestMutationMixin",
    "WorkflowTaskOrcaMutationMixin",
]
