from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.frequencies import parse_frequency_analysis
from orca_auto.orca.report import write_job_html_report
from orca_auto.orca.report.publication import write_report_files
from orca_auto.orca.report.scan import collect_scan_report_data
from tests.engine_artifact_helpers import bind_report_generation, report_generation_target
from tests.orca_output_helpers import (
    COORDS_BLOCK,
    SCAN_FREQ_BLOCK,
    SCAN_MODES_BLOCK,
    SCAN_SURFACE_BLOCK,
    write_scan_inp,
    write_scan_out,
)


def _state(reaction_dir: Path, out_path: Path) -> dict[str, Any]:
    return {
        "job_id": "job_test",
        "run_id": "run_test",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "status": "completed",
        "started_at": "2026-07-03T01:00:00+00:00",
        "updated_at": "2026-07-03T04:00:00+00:00",
        "attempts": [
            {
                "index": 1,
                "inp_path": str(reaction_dir / "rxn.inp"),
                "out_path": str(out_path),
                "return_code": 0,
                "analyzer_status": "completed",
                "analyzer_reason": "ts_criteria_met",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-03T01:00:00+00:00",
                "ended_at": "2026-07-03T03:48:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "ts_criteria_met",
            "completed_at": "2026-07-03T03:48:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_parse_frequency_analysis_reads_last_blocks(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    stale = SCAN_FREQ_BLOCK.replace("-155.30", "-999.00")
    out_path.write_text(
        COORDS_BLOCK + stale + COORDS_BLOCK + SCAN_FREQ_BLOCK + SCAN_MODES_BLOCK,
        encoding="utf-8",
    )

    analysis = parse_frequency_analysis(out_path)

    assert analysis is not None
    assert len(analysis.frequencies) == 9
    assert analysis.frequencies[6] == pytest.approx(-155.30)
    assert [atom[0] for atom in analysis.atoms] == ["H", "O", "O"]
    vector = analysis.mode_vector(6)
    assert vector[0] == pytest.approx(0.9)
    assert vector[3] == pytest.approx(-0.3)
    assert vector[8] == pytest.approx(0.0)


def test_parse_frequency_analysis_without_freq_block(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    out_path.write_text(COORDS_BLOCK + SCAN_SURFACE_BLOCK, encoding="utf-8")
    assert parse_frequency_analysis(out_path) is None


def test_collect_summarizes_imaginary_mode_and_alignment(tmp_path: Path) -> None:
    write_scan_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)

    data = collect_scan_report_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.imaginary_count == 1
    assert len(data.mode_summaries) == 1
    summary = data.mode_summaries[0]
    assert summary.imaginary
    assert summary.frequency_cm == pytest.approx(-155.30)
    top = summary.top_atoms[0]
    assert (top.element, top.atom_index) == ("H", 0)
    # Mode 6: H moves +x, first O moves -x; scanned bond B(0,1) lies on x, so
    # the alignment is |(0.9 - (-0.3))| / sqrt(2).
    assert summary.scan_alignment == pytest.approx(1.2 / 2**0.5, rel=1e-6)
    assert data.forward_barrier_kcal is not None
    assert data.forward_barrier_kcal > 0.5
    assert data.segments[0].points[0].coordinates[0] == pytest.approx(1.86)


def test_collect_returns_none_for_non_scan_input(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text("! Opt B3LYP def2-SVP\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)

    assert collect_scan_report_data(tmp_path, _state(tmp_path, out_path)) is None


def test_write_job_html_report_renders_scan_sections(tmp_path: Path) -> None:
    write_scan_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "Relaxed scan report" in text
    assert "ts_criteria_met" in text
    assert "<polyline" in text
    assert "initial relaxed scan" in text
    assert "imaginary mode" in text
    assert "B(0,1)" in text
    assert "85%" in text
    assert "TS criteria met" in text


def test_scan_report_footer_omits_a_missing_final_output(tmp_path: Path) -> None:
    write_scan_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)
    missing_out = tmp_path / "rxn_retry.out"
    state = _state(tmp_path, out_path)
    state["attempts"].append({"index": 2, "out_path": str(missing_out)})
    state["final_result"]["last_out_path"] = str(missing_out)

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Relaxed scan report" in text
    assert "last output:" not in text
    assert "rxn_retry.out" not in text
    assert "<code>rxn.out</code>" not in text


def test_relaxed_scan_gets_profile_report_not_opt_report(tmp_path: Path) -> None:
    inp = tmp_path / "rxn.inp"
    inp.write_text(
        "\n".join(
            [
                "! Opt B3LYP def2-SVP",
                "",
                "%geom",
                "  Scan",
                "    B 0 1 = 1.86, 1.96, 3",
                "  end",
                "end",
                "",
                "* xyzfile 0 1 input.xyz",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "Relaxed scan report" in text
    assert "ScanTS" not in text
    assert "Scan energy profile" in text
    assert "<polyline" in text
    assert "initial relaxed scan" in text
    assert "Interior barrier" in text
    assert "prominence over the shallower flank" in text
    assert "Optimization convergence" not in text
    # Freq block present in the fixture out: the vibrational summary and the
    # scan-coordinate alignment apply to relaxed scans too.
    assert "B(0,1)" in text
    assert "85%" in text


def _scan_report_text(tmp_path: Path, geom_block: str) -> str:
    (tmp_path / "rxn.inp").write_text(
        "! Opt B3LYP def2-SVP Freq\n" + geom_block + "* xyzfile 0 1 input.xyz\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)
    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )
    assert path is not None
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "geom_block",
    [
        "%geom Scan\n  B 0 1 = 1.86, 1.96, 3\n  end\nend\n",
        "%geom\n  MaxIter 80\n  Scan B 0 1 = 1.86, 1.96, 3 end\nend\n",
        "%geom\n  Scan\n    B 0 1 [1.86 1.91 1.96]\n  end\nend\n",
    ],
    ids=["header", "single-line", "value-list"],
)
def test_every_relaxed_scan_form_gets_the_profile_report(tmp_path: Path, geom_block: str) -> None:
    text = _scan_report_text(tmp_path, geom_block)

    assert "Relaxed scan report" in text
    assert "Scan energy profile" in text
    assert "Optimization convergence" not in text
    assert "B(0,1) = 1.86 &#8594; 1.96 Å, 3 pt" in text
    assert "B(0,1) / Å" in text


def test_unreadable_scan_coordinate_still_gets_the_profile_report(tmp_path: Path) -> None:
    text = _scan_report_text(tmp_path, "%geom\n  Scan\n    B 0 1 = 1.86 to 1.96\n  end\nend\n")

    assert "Relaxed scan report" in text
    assert "Optimization convergence" not in text
    assert "for a minimum" not in text
    assert "scan coordinate</text>" in text


@pytest.mark.parametrize(
    ("geom_block", "label"),
    [
        (
            "%geom\n  Scan\n    D 3 0 1 2 = 122.779, 61.1088, 13\n  end\nend\n",
            "D(3,0,1,2)",
        ),
        ("%geom\n  Scan\n    A 0 1 2 = 122.779, 61.1088, 13\n  end\nend\n", "A(0,1,2)"),
    ],
    ids=["dihedral", "angle"],
)
def test_angular_scan_is_labelled_in_degrees(tmp_path: Path, geom_block: str, label: str) -> None:
    text = _scan_report_text(tmp_path, geom_block)

    assert f"{label} = 122.779 &#8594; 61.1088 °, 13 pt" in text
    assert f"{label} / °" in text
    assert "&#8491;" not in text
    assert f"{label} / Å" not in text


def test_write_report_files_includes_html_for_scan(tmp_path: Path) -> None:
    write_scan_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)

    state = _state(tmp_path, out_path)
    generation = bind_report_generation(tmp_path, state)
    reports = write_report_files(tmp_path, state)

    assert reports["report_html"] == str(generation / "job_report.html")
    assert (generation / "job_report.html").exists()


def test_write_report_files_skips_html_and_removes_stale_for_md(
    tmp_path: Path,
) -> None:
    # MD is a non-stationary job type with no HTML report (single points and IRC
    # now have their own report flavors, so they no longer cover this path).
    inp = tmp_path / "rxn.inp"
    inp.write_text("! B3LYP def2-SVP MD\n* xyzfile 0 1 input.xyz\n", encoding="utf-8")
    out_path = tmp_path / "rxn.out"
    write_scan_out(out_path)
    state = _state(tmp_path, out_path)
    generation = bind_report_generation(tmp_path, state)
    # Leftover report from a previous Opt job in this reused generation
    # must not survive, or downstream links would surface an obsolete report.
    stale = generation / "job_report.html"
    stale.write_text("<html>old opt report</html>", encoding="utf-8")

    reports = write_report_files(tmp_path, state)

    assert "report_html" not in reports
    assert not stale.exists()
