from __future__ import annotations

import json

from orca_auto.flow.cli import workflow_output as output


def test_emit_worker_payload_json_pretty_only_for_single_cycle(capsys) -> None:
    payload = {"worker_session_id": "worker_1", "workflow_results": [{"workflow_id": "wf"}]}

    output.emit_worker_payload(payload, json_mode=True, single_cycle=True)
    pretty = capsys.readouterr().out
    assert json.loads(pretty)["worker_session_id"] == "worker_1"
    assert "\n  " in pretty

    output.emit_worker_payload(payload, json_mode=True, single_cycle=False)
    compact = capsys.readouterr().out.strip()
    assert json.loads(compact)["workflow_results"][0]["workflow_id"] == "wf"
    assert "\n" not in compact


def test_emit_restarted_workflow_names_the_new_generation_route_when_pinned(capsys) -> None:
    output.emit_restarted_workflow(
        {
            "workflow_id": "wf_pinned",
            "status": "restarted",
            "restarted_count": 1,
            "restarted_stages": [{"stage_id": "xtb_path_01"}],
            "pinned_by_terminal_observation": True,
        },
        json_mode=False,
    )

    stdout = capsys.readouterr().out
    assert "workflow_si.md" in stdout
    assert "machine.json" in stdout
    assert "run-dir on the scaffold directory" in stdout


def test_emit_restarted_workflow_stays_quiet_without_a_published_observation(capsys) -> None:
    output.emit_restarted_workflow(
        {"workflow_id": "wf_plain", "status": "restarted", "restarted_stages": []},
        json_mode=False,
    )

    assert "scaffold directory" not in capsys.readouterr().out


def test_emit_json_uses_ascii_for_non_ascii_payload(capsys) -> None:
    output.emit_json({"message": "반응"}, pretty=False)

    stdout = capsys.readouterr().out
    assert "\\ubc18\\uc751" in stdout
    assert "반응" not in stdout
