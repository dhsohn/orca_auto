from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orca_auto.flow.workflow.report import (
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


def test_write_workflow_html_report_renders_sections(tmp_path: Path) -> None:
    stage_a = _orca_stage_dir(tmp_path, "orca_a", energy=-100.001, reason="normal_termination")
    stage_b = _orca_stage_dir(tmp_path, "orca_b", energy=-100.005, reason="ts_criteria_met")
    payload = _payload(
        tmp_path,
        [
            _orca_stage("orca_optts_freq_01", stage_a, status="completed", label="ts_guess_a"),
            _orca_stage("orca_optts_freq_02", stage_b, status="failed", label="ts_guess_b"),
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
    assert "<polyline" in text
    assert "total wall time" in text


def test_write_workflow_html_report_handles_empty_payload(tmp_path: Path) -> None:
    path = write_workflow_html_report(tmp_path, {"workflow_id": "wf_empty"})

    assert path == tmp_path / "workflow_report.html"
    assert "wf_empty" in path.read_text(encoding="utf-8")
