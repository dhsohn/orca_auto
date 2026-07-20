from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from orca_auto.flow.adapters.orca import load_orca_artifact_contract
from tests.engine_artifact_helpers import orca_artifact_payload


def _write_json(path: Path, payload: object) -> None:
    if path.name == "queue.json" and isinstance(payload, list):
        payload = [
            {
                "app_name": "orca_auto_orca",
                "engine": "orca",
                "task_kind": "orca_run_inp",
                **item,
            }
            if isinstance(item, dict)
            else item
            for item in payload
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def test_load_orca_artifact_contract_ignores_root_only_tracking_artifacts(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    job_dir = allowed_root / "rxn_hist_1"
    job_dir.mkdir(parents=True)

    inp = job_dir / "rxn.inp"
    inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
    xyz = job_dir / "rxn.xyz"
    xyz.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    out = job_dir / "rxn.out"
    out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    _write_json(
        job_dir / "job_state.json",
        orca_artifact_payload(
            job_id="job_hist_1",
            run_id="run_hist_1",
            reaction_dir=str(job_dir),
            selected_inp=str(inp),
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-04-19T00:00:00+00:00",
                "last_out_path": str(out),
            },
        ),
    )
    _write_json(
        job_dir / "job_report.json",
        orca_artifact_payload(
            job_id="job_hist_1",
            run_id="run_hist_1",
            reaction_dir=str(job_dir),
            selected_inp=str(inp),
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-04-19T00:00:00+00:00",
                "last_out_path": str(out),
            },
        ),
    )
    _write_json(
        allowed_root / "job_locations.json",
        [
            {
                "job_id": "job_hist_1",
                "app_name": "orca_auto_orca",
                "job_type": "orca_opt",
                "status": "completed",
                "original_run_dir": str(job_dir),
                "molecule_key": "H2",
                "selected_input_xyz": str(inp),
                "latest_known_path": str(job_dir),
                "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
            }
        ],
    )

    contract = load_orca_artifact_contract(
        target="job_hist_1",
        orca_allowed_root=allowed_root,
    )

    assert contract.status == "completed"
    assert contract.run_id == ""
    assert contract.reaction_dir == str(job_dir.resolve())
    assert contract.latest_known_path == str(job_dir.resolve())
    assert contract.selected_inp == str(inp.resolve())
    assert contract.selected_input_xyz == str(xyz.resolve())
    assert contract.last_out_path == ""
    assert contract.run_state_path == ""
    assert contract.report_json_path == ""


def test_load_orca_artifact_contract_matches_queue_task_id(tmp_path: Path) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_queue"
    reaction_dir.mkdir(parents=True)
    inp = reaction_dir / "rxn.inp"
    inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
    xyz = reaction_dir / "rxn.xyz"
    xyz.write_text("2\nsource\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")

    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_123",
                "task_id": "job_q_123",
                "status": "pending",
                "cancel_requested": False,
                "metadata": {
                    "reaction_dir": str(reaction_dir),
                    "selected_inp": str(inp),
                    "selected_input_xyz": str(xyz),
                },
            }
        ],
    )
    _write_json(
        allowed_root / "job_locations.json",
        [
            {
                "job_id": "job_q_123",
                "app_name": "orca_auto_orca",
                "job_type": "orca_opt",
                "status": "queued",
                "original_run_dir": str(reaction_dir),
                "molecule_key": "unknown",
                # Production tracking records prefer the xyzfile input here;
                # the queue's generation-specific selected_inp must still win.
                "selected_input_xyz": str(xyz),
                "latest_known_path": str(reaction_dir),
                "resource_request": {"max_cores": 8},
                "resource_actual": {"max_cores": 8},
            }
        ],
    )

    contract = load_orca_artifact_contract(
        target="job_q_123",
        orca_allowed_root=allowed_root,
    )

    assert contract.status == "queued"
    assert contract.queue_id == "q_123"
    assert contract.queue_status == "pending"
    assert contract.reaction_dir == str(reaction_dir.resolve())
    assert contract.latest_known_path == str(reaction_dir.resolve())
    assert contract.selected_inp == str(inp.resolve())
    assert contract.selected_input_xyz == str(xyz.resolve())


def test_load_orca_artifact_contract_prefers_queue_xyz_over_previous_generation_record(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_queue_generation"
    reaction_dir.mkdir(parents=True)
    inp = reaction_dir / "new.inp"
    inp.write_text("! Opt\n* xyzfile 0 1 new.xyz\n", encoding="utf-8")
    new_xyz = reaction_dir / "new.xyz"
    new_xyz.write_text("1\nnew\nH 0 0 0\n", encoding="utf-8")
    old_xyz = reaction_dir / "old.xyz"
    old_xyz.write_text("1\nold\nH 1 1 1\n", encoding="utf-8")
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "pending",
                "metadata": {
                    "reaction_dir": str(reaction_dir),
                    "selected_inp": str(inp),
                    "selected_input_xyz": str(new_xyz),
                },
            }
        ],
    )
    _write_json(
        allowed_root / "job_locations.json",
        [
            {
                "job_id": "job_old",
                "app_name": "orca_auto_orca",
                "job_type": "orca_opt",
                "status": "failed",
                "original_run_dir": str(reaction_dir),
                "latest_known_path": str(reaction_dir),
                "selected_input_xyz": str(old_xyz),
            }
        ],
    )

    canonical_contract = load_orca_artifact_contract(
        target="q_new",
        orca_allowed_root=allowed_root,
        queue_id="q_new",
        reaction_dir=str(reaction_dir),
    )
    with patch(
        "orca_auto.flow.adapters.orca._orca_tracking.load_orca_contract_payload_impl",
        return_value=None,
    ):
        fallback_contract = load_orca_artifact_contract(
            target="q_new",
            orca_allowed_root=allowed_root,
            queue_id="q_new",
            reaction_dir=str(reaction_dir),
        )

    assert canonical_contract.selected_input_xyz == str(new_xyz.resolve())
    assert fallback_contract.selected_input_xyz == str(new_xyz.resolve())


def test_load_orca_artifact_contract_ignores_previous_force_restart_artifacts(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    reaction_dir = allowed_root / "rxn_force_restart"
    reaction_dir.mkdir(parents=True)
    inp = reaction_dir / "rxn.inp"
    xyz = reaction_dir / "rxn.xyz"
    out = reaction_dir / "rxn.out"
    inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
    xyz.write_text("2\nold\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    out.write_text("old failure", encoding="utf-8")
    old_final = {
        "status": "failed",
        "analyzer_status": "incomplete",
        "reason": "old_failure",
        "completed_at": "2026-04-19T00:00:00+00:00",
        "last_out_path": str(out),
    }
    old_payload = orca_artifact_payload(
        job_id="job_old",
        run_id="run_old",
        reaction_dir=str(reaction_dir),
        selected_inp=str(inp),
        final_result=old_final,
    )
    _write_json(reaction_dir / "job_state.json", old_payload)
    _write_json(reaction_dir / "job_report.json", old_payload)
    (reaction_dir / "job_report.md").write_text("# Old generation\n", encoding="utf-8")
    _write_json(
        allowed_root / "queue.json",
        [
            {
                "queue_id": "q_new",
                "task_id": "job_new",
                "status": "pending",
                "cancel_requested": False,
                "metadata": {"reaction_dir": str(reaction_dir), "force": True},
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
        assert contract.status == "queued"
        assert contract.queue_id == "q_new"
        assert contract.run_id == ""
        assert contract.state_status == ""
        assert contract.reason == ""
        assert contract.attempt_count == 0
        assert contract.attempts == ()
        assert contract.final_result == {}
        assert contract.run_state_path == ""
        assert contract.report_json_path == ""
        assert contract.report_md_path == ""


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
    report_md = reaction_dir / "job_report.md"
    _write_json(state_file, current_payload)
    _write_json(report_json, current_payload)
    report_md.write_text(
        "# Report\n- Job ID: `job_new`\n- run_id: `run_new`\n",
        encoding="utf-8",
    )
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
        assert contract.report_md_path == ""
    assert state_file.is_file()
    assert report_json.is_file()
    assert report_md.is_file()


def test_load_orca_artifact_contract_does_not_resolve_run_id_from_root_report(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "orca_runs"
    job_dir = allowed_root / "rxn_hist_2"
    job_dir.mkdir(parents=True)

    inp = job_dir / "rxn.inp"
    inp.write_text("! Opt\n* xyzfile 0 1 rxn.xyz\n", encoding="utf-8")
    xyz = job_dir / "rxn.xyz"
    xyz.write_text("2\ncomment\nH 0 0 0\nH 0 0 0.74\n", encoding="utf-8")
    out = job_dir / "rxn.out"
    out.write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    _write_json(
        job_dir / "job_state.json",
        orca_artifact_payload(
            job_id="job_hist_2",
            run_id="run_hist_2",
            reaction_dir=str(job_dir),
            selected_inp=str(inp),
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-04-19T00:00:00+00:00",
                "last_out_path": str(out),
            },
        ),
    )
    _write_json(
        job_dir / "job_report.json",
        orca_artifact_payload(
            job_id="job_hist_2",
            run_id="run_hist_2",
            reaction_dir=str(job_dir),
            selected_inp=str(inp),
            final_result={
                "status": "completed",
                "analyzer_status": "completed",
                "reason": "normal_termination",
                "completed_at": "2026-04-19T00:00:00+00:00",
                "last_out_path": str(out),
            },
        ),
    )
    _write_json(
        allowed_root / "job_locations.json",
        [
            {
                "job_id": "job_hist_2",
                "app_name": "orca_auto_orca",
                "job_type": "orca_opt",
                "status": "completed",
                "original_run_dir": str(job_dir),
                "molecule_key": "H2",
                "selected_input_xyz": str(inp),
                "latest_known_path": str(job_dir),
                "resource_request": {"max_cores": 8, "max_memory_gb": 16},
                "resource_actual": {"max_cores": 8, "max_memory_gb": 16},
            }
        ],
    )

    contract = load_orca_artifact_contract(
        target="run_hist_2",
        orca_allowed_root=allowed_root,
    )

    assert contract.run_id == ""
    assert contract.run_state_path == ""
    assert contract.report_json_path == ""
    assert contract.status == "completed"
    assert contract.reaction_dir == str(job_dir.resolve())
    assert contract.latest_known_path == str(job_dir.resolve())
    assert contract.selected_inp == str(inp.resolve())
    assert contract.selected_input_xyz == str(xyz.resolve())
    assert contract.last_out_path == ""


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
            "report_md_path": str(
                (tmp_path / "outputs" / "run_helper_1" / "job_report.md").resolve()
            ),
            "attempt_count": 2,
            "max_retries": 3,
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
    assert contract.max_retries == 3
    assert contract.final_result["reason"] == "normal_termination"
