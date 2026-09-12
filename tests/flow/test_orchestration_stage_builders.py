from __future__ import annotations

from orca_auto.flow.orchestration.stage_builders import (
    new_crest_stage_impl,
)


def test_new_crest_stage_applies_manifest_overrides_to_payload_and_metadata() -> None:
    stage = new_crest_stage_impl(
        workflow_id="wf-1",
        template_name="conformer_screening",
        stage_id="crest_reactant_01",
        source_path="/tmp/reactant.xyz",
        input_role="reactant",
        mode="standard",
        priority=3,
        max_cores=8,
        max_memory_gb=32,
        manifest_overrides={"rthr": 0.3},
    )

    task = stage["task"]
    assert task["payload"]["job_manifest_overrides"] == {"rthr": 0.3}
    assert task["metadata"]["job_manifest_overrides"] == {"rthr": 0.3}
    assert stage["metadata"]["job_manifest_overrides"] == {"rthr": 0.3}
    assert task["enqueue_payload"]["config_argument_placeholder"] == "<crest_config>"
