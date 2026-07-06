"""Recovery-pending payloads must carry the writer's engine, not a heuristic."""

from __future__ import annotations

from pathlib import Path

from orca_auto.core.state.engine import recovery_pending_payload


def _payload(tmp_path: Path, *, existing: dict, engine: str, identity: dict) -> dict:
    return recovery_pending_payload(
        tmp_path,
        existing=existing,
        job_id="job-1",
        selected_input_xyz=tmp_path / "input.xyz",
        reason="worker_shutdown",
        now="2026-07-06T00:00:00+00:00",
        manifest_filename="manifest.json",
        identity_fields=identity,
        retained_fields={},
        resource_request=None,
        resource_actual=None,
        engine=engine,
    )


def test_recovery_payload_uses_the_writer_engine_not_field_heuristics(tmp_path: Path) -> None:
    # Pre-fix, the engine was guessed from the presence of a "job_type"
    # identity field — a crest payload carrying one was mislabeled "xtb".
    payload = _payload(tmp_path, existing={}, engine="crest", identity={"job_type": "ranking"})
    assert payload["engine"] == "crest"


def test_recovery_payload_keeps_the_engine_already_recorded_in_state(tmp_path: Path) -> None:
    payload = _payload(tmp_path, existing={"engine": "xtb"}, engine="crest", identity={})
    assert payload["engine"] == "xtb"
