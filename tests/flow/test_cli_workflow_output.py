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


def test_emit_json_uses_ascii_for_non_ascii_payload(capsys) -> None:
    output.emit_json({"message": "반응"}, pretty=False)

    stdout = capsys.readouterr().out
    assert "\\ubc18\\uc751" in stdout
    assert "반응" not in stdout
