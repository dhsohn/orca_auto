from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from orca_auto.flow.adapters.orca import load_orca_artifact_contract
from tests.engine_artifact_helpers import orca_artifact_payload
from tests.flow.artifact_file_helpers import _write_json


def test_load_orca_artifact_contract_rejects_root_report_paths_in_both_loaders(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_current_generation"
    reaction_dir.mkdir(parents=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! Opt\n* xyz 0 1\nH 0 0 0\n*\n", encoding="utf-8")
    current_payload = orca_artifact_payload(
        job_id="job_new",
        run_id="run_new",
        reaction_dir=str(reaction_dir),
        selected_inp=str(inp),
        status="completed",
    )
    state_file = reaction_dir / "job_state.json"
    report_json = reaction_dir / "job_report.json"
    _write_json(state_file, current_payload)
    _write_json(report_json, current_payload)
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "completed",
                "metadata": {
                    "run_id": "run_new",
                    "reaction_dir": str(reaction_dir),
                    "selected_inp": str(inp),
                },
            }
        ],
    )

    canonical_contract = load_orca_artifact_contract(
        target="q_new",
        queue_id="q_new",
        reaction_dir=str(reaction_dir),
        orca_allowed_root=allowed_root,
    )
    with patch(
        "orca_auto.flow.adapters.orca._orca_tracking.load_orca_contract_payload_impl",
        return_value=None,
    ):
        fallback_contract = load_orca_artifact_contract(
            target="q_new",
            queue_id="q_new",
            reaction_dir=str(reaction_dir),
            orca_allowed_root=allowed_root,
        )

    for contract in (canonical_contract, fallback_contract):
        assert contract.run_state_path == ""
        assert contract.report_json_path == ""
    assert state_file.is_file()
    assert report_json.is_file()


def test_load_orca_artifact_contract_prefers_orca_contract_payload_helper(tmp_path: Path) -> None:
    with patch(
        "orca_auto.flow.adapters._orca_tracking.load_orca_contract_payload",
        lambda *_args, **_kwargs: {
            "run_id": "run_helper_1",
            "status": "completed",
            "reason": "normal_termination",
            "state_status": "completed",
            "reaction_dir": str((tmp_path / "rxn_helper").resolve()),
            "latest_known_path": str((tmp_path / "rxn_helper").resolve()),
            "optimized_xyz_path": str(
                (tmp_path / "outputs" / "run_helper_1" / "rxn.xyz").resolve()
            ),
            "queue_id": "q_helper_1",
            "queue_status": "completed",
            "cancel_requested": False,
            "selected_inp": str((tmp_path / "outputs" / "run_helper_1" / "rxn.inp").resolve()),
            "selected_input_xyz": str(
                (tmp_path / "outputs" / "run_helper_1" / "rxn.xyz").resolve()
            ),
            "analyzer_status": "completed",
            "completed_at": "2026-04-19T00:10:00+00:00",
            "last_out_path": str((tmp_path / "outputs" / "run_helper_1" / "rxn.out").resolve()),
            "run_state_path": str(
                (tmp_path / "outputs" / "run_helper_1" / "job_state.json").resolve()
            ),
            "report_json_path": str(
                (tmp_path / "outputs" / "run_helper_1" / "job_report.json").resolve()
            ),
            "attempt_count": 2,
            "attempts": [{"attempt_number": 1, "analyzer_status": "completed"}],
            "final_result": {"reason": "normal_termination"},
            "resource_request": {"max_cores": 8, "max_memory_gb": 16},
            "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
        },
    ):
        contract = load_orca_artifact_contract(
            target="job_helper_1",
            orca_allowed_root=tmp_path / "orca_runs",
        )

    assert contract.run_id == "run_helper_1"
    assert contract.status == "completed"
    assert contract.queue_id == "q_helper_1"
    assert contract.attempt_count == 2
    assert contract.final_result["reason"] == "normal_termination"
