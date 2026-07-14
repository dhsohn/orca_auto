from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow.workflow import report as workflow_report
from orca_auto.flow.workflow.report import (
    _energy_axis_ticks,
    _tick_label,
    collect_workflow_report_data,
    count_xyz_frames,
    latest_engrad_energy,
    write_workflow_html_report,
)

_ENGRAD_TEMPLATE = """#
# Number of atoms
#
 3
#
# The current total energy in Eh
#
  {energy}
#
# The current gradient in Eh/bohr
#
       0.000085816662
"""


def _write_multi_xyz(path: Path, frames: int) -> None:
    blocks = []
    for index in range(frames):
        blocks.append(f"3\n -100.{index:04d}\nH 0 0 0\nO 1 0 0\nO 3 0 0\n")
    path.write_text("".join(blocks), encoding="utf-8")


def _orca_stage_dir(root: Path, name: str, *, energy: float, reason: str) -> Path:
    stage_dir = root / name
    stage_dir.mkdir(parents=True)
    (stage_dir / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy=f"{energy:.12f}"), encoding="utf-8"
    )
    report = {
        "engine_payload": {
            "attempts": [
                {
                    "index": 1,
                    "markers": {"imaginary_frequency_count": 0},
                }
            ],
            "final_result": {"reason": reason},
        }
    }
    (stage_dir / "job_report.json").write_text(json.dumps(report), encoding="utf-8")
    (stage_dir / "job_report.html").write_text("<html></html>", encoding="utf-8")
    return stage_dir


def _orca_stage(stage_id: str, stage_dir: Path, *, status: str, label: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "stage_kind": "orca_stage",
        "status": status,
        "metadata": {"selected_input_label": label},
        "output_artifacts": [
            {"kind": "orca_output_dir", "path": str(stage_dir)},
            {"kind": "orca_report_json", "path": str(stage_dir / "job_report.json")},
        ],
    }


def _orca_output_report(out_path: Path) -> dict[str, Any]:
    return {
        "engine_payload": {
            "attempts": [{"index": 1, "out_path": str(out_path)}],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(out_path),
            },
        }
    }


def _payload(workspace: Path, stages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "workflow_id": "wf_test",
        "template_name": "conformer_screening",
        "status": "completed",
        "reaction_key": "input",
        "requested_at": "2026-07-03T01:00:00+00:00",
        "metadata": {
            "workspace_dir": str(workspace),
            "last_advanced_at": "2026-07-03T05:30:00+00:00",
        },
        "stages": stages,
    }


def test_count_xyz_frames_and_engrad_energy(tmp_path: Path) -> None:
    xyz = tmp_path / "crest_conformers.xyz"
    _write_multi_xyz(xyz, frames=4)
    assert count_xyz_frames(xyz) == 4

    (tmp_path / "opt.engrad").write_text(
        _ENGRAD_TEMPLATE.format(energy="-100.123456789012"), encoding="utf-8"
    )
    assert latest_engrad_energy(tmp_path) == pytest.approx(-100.123456789012)


def test_collect_ranks_orca_results_and_counts_funnel(tmp_path: Path) -> None:
    crest_dir = tmp_path / "01_crest"
    crest_dir.mkdir()
    conformers = crest_dir / "crest_conformers.xyz"
    _write_multi_xyz(conformers, frames=5)

    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="normal_termination")

    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_conformer_01",
                "stage_kind": "crest_stage",
                "status": "completed",
                "metadata": {"input_role": "molecule", "mode": "nci"},
                "output_artifacts": [{"kind": "crest_conformer", "path": str(conformers)}],
            },
            _orca_stage("orca_conformer_01", stage_a, status="completed", label="conf_01"),
            _orca_stage("orca_conformer_02", stage_b, status="completed", label="conf_02"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.crest_conformer_total == 5
    assert [row.stage_kind for row in data.stage_rows] == [
        "crest_stage",
        "orca_stage",
        "orca_stage",
    ]
    # Lowest energy ranks first; relative energy measured from it.
    assert [entry.label for entry in data.orca_results] == ["conf_02", "conf_01"]
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)
    assert data.orca_results[1].rel_kcal == pytest.approx(0.004 * 627.5094740631, rel=1e-6)
    assert data.orca_results[0].imaginary_count == 0
    assert data.orca_results[0].report_href is not None
    assert "orca_b" in data.orca_results[0].report_href


def test_collect_uses_final_orca_output_energy_when_engrad_is_absent(tmp_path: Path) -> None:
    stage_dir = tmp_path / "orca_from_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_text(
        "\n".join(
            [
                "|  1> ! r2scan-3c Opt TightSCF",
                "FINAL SINGLE POINT ENERGY -1.000000000000",
                "FINAL SINGLE POINT ENERGY -1.100000000000",
                "****ORCA TERMINATED NORMALLY****",
                "",
            ]
        ),
        encoding="utf-8",
    )
    report = {
        "engine_payload": {
            "attempts": [
                {
                    "index": 1,
                    "out_path": str(out_path),
                    "markers": {"imaginary_frequency_count": 0},
                }
            ],
            "final_result": {
                "reason": "normal_termination",
                "last_out_path": str(out_path),
            },
        }
    }
    (stage_dir / "job_report.json").write_text(json.dumps(report), encoding="utf-8")
    (stage_dir / "job_report.html").write_text("<html></html>", encoding="utf-8")
    payload = _payload(
        tmp_path,
        [_orca_stage("orca_conformer_01", stage_dir, status="completed", label="conf_01")],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.orca_results[0].energy == pytest.approx(-1.1)
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)


def test_orca_output_energy_reads_only_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = tmp_path / "orca_large_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_bytes(
        b"FINAL SINGLE POINT ENERGY -9.000000000000\n"
        + b"x" * (workflow_report._MAX_ORCA_ENERGY_SCAN_BYTES + 4096)
        + b"\nFINAL SINGLE POINT ENERGY -2.500000000000\n"
    )
    bytes_requested = 0
    original_pread = workflow_report.os.pread

    def tracked_pread(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal bytes_requested
        bytes_requested += count
        return original_pread(descriptor, count, offset)

    monkeypatch.setattr(workflow_report.os, "pread", tracked_pread)

    energy = workflow_report._orca_report_output_energy(stage_dir, _orca_output_report(out_path))

    assert out_path.stat().st_size > workflow_report._MAX_ORCA_ENERGY_SCAN_BYTES
    assert bytes_requested == workflow_report._MAX_ORCA_ENERGY_SCAN_BYTES
    assert energy == pytest.approx(-2.5)


def test_orca_output_energy_rejects_file_changed_during_tail_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_dir = tmp_path / "orca_changing_output"
    stage_dir.mkdir()
    out_path = stage_dir / "opt.out"
    out_path.write_text(
        "FINAL SINGLE POINT ENERGY -1.100000000000\n",
        encoding="utf-8",
    )
    original_pread = workflow_report.os.pread
    changed = False

    def mutating_pread(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal changed
        chunk = original_pread(descriptor, count, offset)
        if not changed:
            changed = True
            with out_path.open("ab") as handle:
                handle.write(b"changed\n")
        return chunk

    monkeypatch.setattr(workflow_report.os, "pread", mutating_pread)

    assert (
        workflow_report._orca_report_output_energy(stage_dir, _orca_output_report(out_path)) is None
    )


def test_orca_output_energy_rejects_nonregular_multilink_or_unconfined_paths(
    tmp_path: Path,
) -> None:
    stage_dir = tmp_path / "orca_untrusted_output"
    stage_dir.mkdir()
    target = stage_dir / "target.out"
    target.write_text(
        "FINAL SINGLE POINT ENERGY -1.100000000000\n",
        encoding="utf-8",
    )
    symlink = stage_dir / "symlink.out"
    symlink.symlink_to(target.name)
    hardlink = stage_dir / "hardlink.out"
    os.link(target, hardlink)
    fifo = stage_dir / "fifo.out"
    os.mkfifo(fifo)
    outside = tmp_path / "outside.out"
    outside.write_text(
        "FINAL SINGLE POINT ENERGY -2.200000000000\n",
        encoding="utf-8",
    )

    for candidate in (symlink, hardlink, fifo, outside):
        assert (
            workflow_report._orca_report_output_energy(stage_dir, _orca_output_report(candidate))
            is None
        )


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


def test_failed_stage_energy_excluded_from_ranking_baseline(tmp_path: Path) -> None:
    # The failed stage's .engrad holds a lower transient energy; it must not
    # become the ΔE reference nor outrank the completed candidate.
    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="geometry_zero_distance")
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_conformer_01", stage_a, status="completed", label="conf_ok"),
            _orca_stage("orca_conformer_02", stage_b, status="failed", label="conf_failed"),
        ],
    )

    data = collect_workflow_report_data(tmp_path, payload)

    assert [entry.label for entry in data.orca_results] == ["conf_ok", "conf_failed"]
    assert data.orca_results[0].rel_kcal == pytest.approx(0.0)
    assert data.orca_results[1].rel_kcal is None
    assert data.orca_results[1].energy == pytest.approx(-100.005)


def test_write_workflow_html_report_handles_empty_payload(tmp_path: Path) -> None:
    path = write_workflow_html_report(tmp_path, {"workflow_id": "wf_empty"})

    assert path == tmp_path / "workflow_report.html"
    assert "wf_empty" in path.read_text(encoding="utf-8")


def test_failed_crest_topology_change_is_explained_in_workflow_report(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "01_crest" / "crest_reactant_01"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
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


def test_xtb_retry_prefers_refreshed_latest_path_over_original_task_job_dir(
    tmp_path: Path,
) -> None:
    old_job_dir = tmp_path / "02_xtb" / "xtb_old_attempt"
    old_job_dir.mkdir(parents=True)
    (old_job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "xtb-current"},
                "status": {"state": "failed", "reason": "old_xtb_failure"},
            }
        ),
        encoding="utf-8",
    )
    current_job_dir = tmp_path / "02_xtb" / "xtb_retry_attempt"
    current_job_dir.mkdir(parents=True)
    (current_job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "xtb-current", "queue_id": "xtb-q-current"},
                "status": {"state": "failed", "reason": "current_xtb_failure"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "xtb_path_retry",
                "stage_kind": "xtb_stage",
                "status": "submission_failed",
                "task": {
                    "engine": "xtb",
                    "status": "submission_failed",
                    "payload": {"job_dir": str(old_job_dir)},
                    "submission_result": {"queue_id": "xtb-q-current"},
                },
                "metadata": {
                    "child_job_id": "xtb-current",
                    "latest_known_path": str(current_job_dir),
                    "queue_id": "xtb-q-current",
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "current_xtb_failure"
    assert data.failure_rows[0].details_href == "02_xtb/xtb_retry_attempt/job_report.json"


def test_stage_report_prefers_latest_path_over_original_task_job_dir(
    tmp_path: Path,
) -> None:
    stale_job_dir = tmp_path / "01_crest" / "crest_stale"
    stale_job_dir.mkdir(parents=True)
    (stale_job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "crest-current"},
                "status": {"state": "failed", "reason": "stale_crest_failure"},
            }
        ),
        encoding="utf-8",
    )
    current_job_dir = tmp_path / "01_crest" / "crest_current"
    current_job_dir.mkdir(parents=True)
    (current_job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "crest-current"},
                "status": {"state": "failed", "reason": "current_crest_failure"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "crest_repaired",
                "stage_kind": "crest_stage",
                "status": "failed",
                "task": {
                    "engine": "crest",
                    "status": "failed",
                    "payload": {"job_dir": str(stale_job_dir)},
                },
                "metadata": {
                    "child_job_id": "crest-current",
                    "latest_known_path": str(current_job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "current_crest_failure"
    assert data.failure_rows[0].details_href == "01_crest/crest_current/job_report.json"


def test_orca_run_identity_allows_current_report_diagnostic(tmp_path: Path) -> None:
    job_dir = tmp_path / "03_orca" / "orca_current"
    job_dir.mkdir(parents=True)
    (job_dir / "job_report.json").write_text(
        json.dumps(
            {
                "job": {"id": "orca-child"},
                "status": {"state": "failed", "reason": "runner_exception"},
                "engine_payload": {"run_id": "run-current"},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(
        tmp_path,
        [
            {
                "stage_id": "orca_current",
                "stage_kind": "orca_stage",
                "status": "failed",
                "task": {"engine": "orca", "status": "failed", "payload": {}},
                "metadata": {
                    "run_id": "run-current",
                    "latest_known_path": str(job_dir),
                },
            }
        ],
    )
    payload["status"] = "failed"

    data = collect_workflow_report_data(tmp_path, payload)

    assert data.failure_rows[0].reason == "runner_exception"
    assert data.failure_rows[0].details_href == "03_orca/orca_current/job_report.json"


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
