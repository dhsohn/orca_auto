from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca_auto.flow.workflow.report_collection import collect_workflow_report_data
from orca_auto.flow.workflow.report_rendering import (
    _energy_axis_ticks,
    _tick_label,
    render_workflow_report_html,
    write_workflow_html_report,
)
from tests.flow.workflow_report_helpers import _orca_stage, _orca_stage_dir, _payload


@pytest.mark.parametrize(
    ("second_route", "second_charge", "second_multiplicity"),
    [
        ("! B3LYP Opt", 0, 1),
        ("! HF Opt", -1, 1),
        ("! HF Opt", 0, 2),
    ],
)
def test_workflow_report_omits_relative_energies_for_mixed_executed_science(
    tmp_path: Path,
    second_route: str,
    second_charge: int,
    second_multiplicity: int,
) -> None:
    first = _orca_stage_dir(
        tmp_path,
        "orca_first",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF Opt",
    )
    second = _orca_stage_dir(
        tmp_path,
        "orca_second",
        energy=-100.005,
        reason="normal_termination",
        route_line=second_route,
        charge=second_charge,
        multiplicity=second_multiplicity,
    )
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_first", first, status="completed", label="first"),
            _orca_stage("orca_second", second, status="completed", label="second"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.energy for entry in data.orca_results] == pytest.approx([-100.001, -100.005])
    assert all(entry.rel_kcal is None for entry in data.orca_results)
    rendered = render_workflow_report_html(data)
    assert "Relative energies are omitted" in rendered
    assert "provenance is missing or differs" in rendered
    orca_table = rendered.split("<h2>ORCA results</h2>", 1)[1].split("<h2>", 1)[0]
    assert orca_table.count("<tr><td>&#8211;</td>") == 2
    assert "<tr><td>1</td>" not in orca_table


def test_workflow_report_omits_relative_energies_for_mixed_orca_versions(
    tmp_path: Path,
) -> None:
    first = _orca_stage_dir(
        tmp_path,
        "orca_version_first",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF Opt",
        version="6.0.1",
    )
    second = _orca_stage_dir(
        tmp_path,
        "orca_version_second",
        energy=-100.005,
        reason="normal_termination",
        route_line="! HF Opt",
        version="6.1.0",
    )
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_version_first", first, status="completed", label="first"),
            _orca_stage("orca_version_second", second, status="completed", label="second"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert all(entry.rel_kcal is None for entry in data.orca_results)
    rendered = render_workflow_report_html(data)
    assert "Relative energies are omitted" in rendered
    assert "provenance is missing or differs" in rendered


def test_workflow_report_omits_relative_energies_for_mixed_active_directives(
    tmp_path: Path,
) -> None:
    first = _orca_stage_dir(
        tmp_path,
        "orca_directive_first",
        energy=-100.001,
        reason="normal_termination",
        route_line="! HF Opt",
    )
    second = _orca_stage_dir(
        tmp_path,
        "orca_directive_second",
        energy=-100.005,
        reason="normal_termination",
        route_line="! HF Opt",
        extra_directives="%scf\n  MaxIter 999\nend",
    )
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_directive_first", first, status="completed", label="first"),
            _orca_stage("orca_directive_second", second, status="completed", label="second"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.energy for entry in data.orca_results] == pytest.approx([-100.001, -100.005])
    assert all(entry.rel_kcal is None for entry in data.orca_results)
    assert "Relative energies are omitted" in render_workflow_report_html(data)


def test_single_completed_candidate_without_science_identity_has_no_numeric_rank(
    tmp_path: Path,
) -> None:
    stage_dir = _orca_stage_dir(
        tmp_path,
        "orca_missing_science_dependency",
        energy=-100.0,
        reason="normal_termination",
        extra_directives='%pointcharges "missing.pc"',
    )
    stage = _orca_stage(
        "orca_missing_science_dependency",
        stage_dir,
        status="completed",
        label="missing provenance",
    )

    data = collect_workflow_report_data(tmp_path, _payload(tmp_path, [stage]))
    rendered = render_workflow_report_html(data)
    orca_table = rendered.split("<h2>ORCA results</h2>", 1)[1].split("<h2>", 1)[0]

    assert data.orca_results[0].energy == pytest.approx(-100.0)
    assert data.orca_results[0].science_identity is None
    assert data.orca_results[0].rel_kcal is None
    assert "Relative energies are omitted" in orca_table
    assert "<tr><td>&#8211;</td>" in orca_table
    assert "<tr><td>1</td>" not in orca_table


def test_write_workflow_html_report_renders_sections(tmp_path: Path) -> None:
    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="ts_criteria_met")
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_optts_freq_01", stage_a, status="completed", label="ts_guess_a"),
            _orca_stage("orca_optts_freq_02", stage_b, status="completed", label="ts_guess_b"),
        ],
    )
    payload["template_name"] = "reaction_ts_search"

    path = write_workflow_html_report(tmp_path, payload)

    assert path == tmp_path / "workflow_report.html"
    text = path.read_text(encoding="utf-8")
    assert "workflow report" in text
    assert "TS candidates" in text
    assert "Stage chain" in text
    assert "ts_guess_a" in text
    assert 'href="orca_b' in text
    assert "<circle" in text
    assert "<polyline" not in text
    assert "kcal mol⁻¹" in text
    assert "total wall time" in text


def test_write_workflow_html_report_handles_empty_payload(tmp_path: Path) -> None:
    path = write_workflow_html_report(tmp_path, {"workflow_id": "wf_empty"})

    assert path == tmp_path / "workflow_report.html"
    assert "wf_empty" in path.read_text(encoding="utf-8")


def test_failed_crest_topology_change_is_explained_in_workflow_report(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "01_crest" / "crest_reactant_01"
    job_dir.mkdir(parents=True)
    (job_dir / "job_state.json").write_text(
        json.dumps(
            {
                "engine": "crest",
                "job": {"id": "crest-current"},
                "status": {
                    "state": "failed",
                    "reason": "crest_exit_code_156",
                    "exit_code": 156,
                },
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "crest.stdout.log").write_text(
        "\n".join(
            [
                "*WARNING* Change in topology detected!",
                "Topology change compared to the input affects atoms:",
                "21(P) 35(O) 42(C)",
                "A topology change was seen in the initial geometry optimization.",
                "Safety termination of CREST.",
            ]
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_reactant_01",
                "stage_kind": "crest_stage",
                "status": "failed",
                "task": {"engine": "crest", "status": "failed", "payload": {}},
                "metadata": {
                    "input_role": "reactant",
                    "child_job_id": "crest-current",
                    "latest_known_path": str(job_dir),
                },
            },
            {
                "stage_id": "crest_product_01",
                "stage_kind": "crest_stage",
                "status": "cancelled",
                "task": {
                    "engine": "crest",
                    "status": "cancelled",
                    "payload": {},
                    "cancel_result": {"reason": "cancel_requested"},
                },
                "metadata": {"input_role": "product"},
            },
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert len(data.failure_rows) == 1
    assert data.failure_rows[0].reason == "crest_exit_code_156"
    assert "21(P) 35(O) 42(C)" in data.failure_rows[0].explanation
    assert data.failure_rows[0].details_href == ("01_crest/crest_reactant_01/crest.stdout.log")
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Why it failed" in text
    assert "changed molecular topology" in text
    assert "crest_exit_code_156" in text
    assert "crest.noreftopo: true" in text
    assert "can retain artifacts" in text
    assert 'href="01_crest/crest_reactant_01/crest.stdout.log"' in text
    assert "crest_product_01" in text
    assert "cancel_requested" in text


def test_restarted_stage_does_not_show_stale_failure_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "01_crest" / "crest_reactant_01"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "crest-old"},
                "status": {"state": "failed", "reason": "crest_exit_code_156"},
            }
        ),
        encoding="utf-8",
    )
    (job_dir / "crest.stdout.log").write_text(
        "Change in topology detected!\n"
        "A topology change was seen in the initial geometry optimization.\n",
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_reactant_01",
                "stage_kind": "crest_stage",
                "status": "queued",
                "task": {"engine": "crest", "status": "submitted", "payload": {}},
                "metadata": {
                    "child_job_id": "crest-new",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "running"

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert data.failure_rows == ()
    assert data.stage_rows[0].detail == ""
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Why it failed" not in text
    assert "crest_exit_code_156" not in text
    assert "changed molecular topology" not in text


def test_failed_stage_without_current_identity_does_not_use_old_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "03_orca" / "orca_submission_failed"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "orca-old"},
                "status": {"state": "failed", "reason": "old_runner_error"},
                "engine_payload": {"run_id": "run-old"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_submission_failed",
                "stage_kind": "orca_stage",
                "status": "submission_failed",
                "task": {"engine": "orca", "status": "submission_failed", "payload": {}},
                "metadata": {"latest_known_path": str(job_dir)},
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert data.failure_rows[0].reason == ""
    assert data.failure_rows[0].details_href is None
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "old_runner_error" not in text
    assert "job_report.json" not in text


def test_workflow_error_message_is_primary_and_escaped(tmp_path: Path) -> None:
    payload = _payload(tmp_path, [])
    payload["status"] = "failed"
    payload["metadata"]["workflow_error"] = {
        "status": "failed",
        "reason": "no_endpoint_pairs",
        "message": "No pair passed <endpoint> filters.",
        "scope": "reaction_ts_search_endpoint_pairing",
        "stage_id": "crest_pair_01",
    }

    path = write_workflow_html_report(tmp_path, payload)

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "No pair passed &lt;endpoint&gt; filters." in text
    assert "code: no_endpoint_pairs" in text
    assert "stage: crest_pair_01" in text
    assert "scope: reaction_ts_search_endpoint_pairing" in text
    assert "No pair passed <endpoint> filters." not in text


def test_nonfatal_stage_failure_has_no_workflow_failure_verdict(tmp_path: Path) -> None:
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_candidate_bad",
                "stage_kind": "orca_stage",
                "status": "completed",
                "task": {"engine": "orca", "status": "failed", "payload": {}},
                "metadata": {"reason": "ts_criteria_failed"},
            }
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)
    path = write_workflow_html_report(tmp_path, payload)

    assert data.failure_rows[0].status == "failed"
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Stage failures" in text
    assert "Why it failed" not in text


def test_energy_axis_tick_labels_stay_exact_for_quarter_steps() -> None:
    # All candidates within 1 kcal/mol → 0.25-wide ticks; one-decimal labels
    # would render the 0.25 tick as "0.2".
    ticks = _energy_axis_ticks(1.0)

    assert ticks == (0.0, 0.25, 0.5, 0.75, 1.0)
    step = ticks[1] - ticks[0]
    assert [_tick_label(tick, step) for tick in ticks] == ["0", "0.25", "0.50", "0.75", "1"]
    assert _tick_label(2.5, 2.5) == "2.5"
