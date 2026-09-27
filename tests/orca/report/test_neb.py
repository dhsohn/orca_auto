from __future__ import annotations

import builtins
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report import write_job_html_report
from orca_auto.orca.report.composer import collect_html_report_parts
from orca_auto.orca.report.neb import (
    NebPathPoint,
    NebReportData,
    _parse_ts_refinement_steps,
    _path_plot_x,
)
from orca_auto.orca.report.render import ChartSeries, line_chart_svg
from tests.engine_artifact_helpers import report_generation_target
from tests.orca_output_helpers import (
    FREQ_TS_BLOCK,
    write_neb_inp,
    write_neb_irc_out,
    write_neb_out,
)


def _neb_data(reaction_dir: Path, state: dict[str, Any]) -> NebReportData:
    parts = collect_html_report_parts(reaction_dir, state)
    assert parts is not None and parts.neb is not None
    return parts.neb


@pytest.mark.parametrize(
    "markers,expected",
    [
        ((), ((1, -1.0), (2, -2.0), (3, -3.0))),
        ((1,), ((2, -2.0), (3, -3.0))),
        ((1, 2), ((3, -3.0),)),
        ((3,), ()),
    ],
    ids=["no-marker", "one-marker", "last-marker", "no-refinement-after-marker"],
)
def test_ts_refinement_cycles_start_after_last_neb_convergence(
    markers: tuple[int, ...], expected: tuple[tuple[int, float], ...]
) -> None:
    lines = []
    for cycle in (1, 2, 3):
        lines.extend(
            [f"Geometry Optimization Cycle {cycle}", f"FINAL SINGLE POINT ENERGY -{cycle}"]
        )
        if cycle in markers:
            lines.append("the NEB optimization has converged")
    assert _parse_ts_refinement_steps("\n".join(lines)) == expected


def test_ts_refinement_cycles_keep_last_finite_energy_and_skip_empty_cycles() -> None:
    text = (
        "THE NEB OPTIMIZATION HAS CONVERGED\n"
        "FINAL SINGLE POINT ENERGY -99\n"
        "Geometry Optimization Cycle 1\n"
        "FINAL SINGLE POINT ENERGY -1\n"
        "FINAL SINGLE POINT ENERGY -2D0 (SCF not fully converged!)\n"
        "FINAL SINGLE POINT ENERGY -1E999\n"
        "GEOMETRY OPTIMIZATION CYCLE 2\n"
        "FINAL SINGLE POINT ENERGY nan\n"
        "Geometry Optimization Cycle 1\n"
        "FINAL SINGLE POINT ENERGY -3\n"
    )

    assert _parse_ts_refinement_steps(text) == ((1, -2.0), (1, -3.0))


def _state(reaction_dir: Path, out_path: Path) -> dict[str, Any]:
    return {
        "job_id": "job_neb",
        "run_id": "run_neb",
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
                "ended_at": "2026-07-03T04:00:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "ts_criteria_met",
            "completed_at": "2026-07-03T04:00:30+00:00",
            "last_out_path": str(out_path),
        },
    }


@pytest.mark.parametrize(
    "tail,encoding",
    [("", "utf-8"), ("", "utf-16"), ("empty", "utf-8"), ("freq", "utf-8"), ("missing", "utf-8")],
    ids=["complete", "utf16", "trailing-empty", "trailing-freq", "trailing-missing"],
)
def test_neb_report_decodes_each_attempt_output_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str, encoding: str
) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_neb_out(out_path)
    if encoding != "utf-8":
        out_path.write_text(out_path.read_text(encoding="utf-8"), encoding=encoding)
    state = _state(tmp_path, out_path)
    # Path table, TS refinement, optimization progress, and frequency analysis
    # all come from one decoded snapshot per attempt output.
    expected_reads = {out_path: 1}
    if tail:
        tail_path = tmp_path / "tail.out"
        if tail != "missing":
            tail_path.write_text(FREQ_TS_BLOCK if tail == "freq" else "", encoding="utf-8")
            expected_reads[tail_path] = 1
        state["attempts"].append({"index": 2, "out_path": str(tail_path)})
        state["final_result"]["last_out_path"] = str(tail_path)
    observed_reads: Counter[Path] = Counter()
    original_open = builtins.open

    def counted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "rb" and Path(file) in expected_reads:
            observed_reads[Path(file)] += 1
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counted_open)

    data = _neb_data(tmp_path, state)

    assert data is not None
    assert len(data.path_points) == 11
    assert data.ts_steps == ((1, -343.999), (2, -343.99864))
    assert data.imaginary_count == 1
    assert data.frequency_attempt_index == (2 if tail == "freq" else 1)
    assert observed_reads == expected_reads


def test_neb_report_retains_empty_fallback_for_unreadable_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_neb_out(out_path)
    original_open = builtins.open

    def unreadable_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "rb" and Path(file) == out_path:
            raise PermissionError("synthetic unreadable output")
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", unreadable_open)

    data = _neb_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.path_points == ()
    assert data.ts_steps == ()
    assert data.final_energy is None
    assert data.imaginary_count is None


def test_collect_neb_report_data_parses_path_and_iterations(tmp_path: Path) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_neb_out(out_path)

    data = _neb_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.neb_converged
    assert data.ts_converged
    assert len(data.path_points) == 11
    ci = next(point for point in data.path_points if point.marker == "CI")
    ts = next(point for point in data.path_points if point.marker == "TS")
    assert ci.label == "6"
    assert ci.relative_kcal == pytest.approx(53.32)
    assert ts.label == "TS"
    assert ts.energy_hartree == pytest.approx(-343.99864)
    assert [point.iteration for point in data.iterations] == [0, 1, 49, 50]
    assert data.iterations[-1].phase == "CI"
    assert data.ts_steps == ((1, -343.999), (2, -343.99864))
    assert data.imaginary_count == 1
    assert data.settings[0].label == "Method type"
    # "Generation of initial path ...." is a setting row, not the section
    # terminator, and dotted labels may contain periods ("Max. iterations");
    # the table must run up to the "Generation of  the initial path:" line.
    labels = [setting.label for setting in data.settings]
    assert "Generation of initial path" in labels
    assert "Max. iterations" in labels
    assert not any("nebts_initial_path_trj" in setting.value for setting in data.settings)


def test_collect_neb_report_data_skips_contentless_final_attempt(tmp_path: Path) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_neb_out(out_path)
    dead_out = tmp_path / "rxn_retry.out"
    dead_out.write_text("ORCA crashed before the NEB driver started\n", encoding="utf-8")

    state = _state(tmp_path, out_path)
    state["attempts"].append(
        {
            "index": 2,
            "inp_path": str(tmp_path / "rxn.inp"),
            "out_path": str(dead_out),
            "return_code": 1,
            "analyzer_status": "failed",
            "analyzer_reason": "abnormal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-07-03T04:01:00+00:00",
            "ended_at": "2026-07-03T04:01:30+00:00",
        }
    )
    data = _neb_data(tmp_path, state)

    assert data is not None
    assert len(data.path_points) == 11
    assert data.neb_converged
    assert data.ts_steps == ((1, -343.999), (2, -343.99864))


def test_neb_path_plot_x_places_ts_between_neighboring_images() -> None:
    points = (
        NebPathPoint("4", 4, 4, -1.0, 10.0, 0.1, 0.1, ""),
        NebPathPoint("TS", 5, None, -0.9, 20.0, 0.1, 0.1, "TS"),
        NebPathPoint("5", 6, 5, -0.8, 15.0, 0.1, 0.1, ""),
    )

    assert _path_plot_x(points, 0) == pytest.approx(4.0)
    assert _path_plot_x(points, 1) == pytest.approx(4.5)
    assert _path_plot_x(points, 2) == pytest.approx(5.0)


def test_line_chart_svg_uses_marker_legend_for_single_point_series() -> None:
    svg = line_chart_svg(
        (
            ChartSeries(label="selected point", color="#158a72", dash="", points=((0.5, 0.5),)),
            ChartSeries(label="path", color="#2f6fb2", dash="", points=((0.0, 0.0), (1.0, 1.0))),
        ),
        x_label="image",
        y_label="energy",
    )

    assert '<circle cx="90" cy="24" r="3.5" fill="#158a72"/>' in svg
    assert '<line x1="76" y1="24" x2="104" y2="24" stroke="#158a72"' not in svg


def test_neb_report_footer_omits_a_missing_final_output(tmp_path: Path) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_neb_out(out_path)
    missing_out = tmp_path / "rxn_retry.out"
    state = _state(tmp_path, out_path)
    state["attempts"].append({"index": 2, "out_path": str(missing_out)})
    state["final_result"]["last_out_path"] = str(missing_out)

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "NEB-CI path profile" in text
    assert "last output:" not in text
    assert "rxn_retry.out" not in text
    assert "<code>rxn.out</code>" not in text


def test_neb_ts_report_renders_neb_specific_sections(tmp_path: Path) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    out_path = tmp_path / "rxn.out"
    write_neb_out(out_path)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "NEB-TS report" in text
    assert "NEB-CI path profile" in text
    assert "NEB-CI optimization" in text
    assert "TS optimization convergence" in text
    assert "NEB setup" in text
    assert "Number of intermediate images" in text
    assert "climbing image" in text
    assert "optimized TS" in text
    assert "CI-NEB converged" in text
    assert "TS optimized" in text
    assert "initial NEB-TS" in text
    assert "imaginary mode" in text
    assert "kcal mol⁻¹" in text
    assert "mol^-1" not in text
    assert "<polyline" in text


def test_neb_ts_irc_report_composes_neb_and_irc_sections(tmp_path: Path) -> None:
    write_neb_inp(tmp_path / "rxn.inp")
    (tmp_path / "rxn.inp").write_text(
        (tmp_path / "rxn.inp").read_text(encoding="utf-8").replace("Freq", "Freq IRC"),
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    write_neb_irc_out(out_path)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "NEB-TS report" in text
    assert "NEB-CI path profile" in text
    assert "IRC path profile" in text
    assert "neb_IRC_Full.xyz" in text
    assert "IRC path found" in text
    assert "Vibrational summary" in text
    assert "initial NEB-TS" in text
    assert text.count('<div class="metric-label">Imaginary frequencies</div>') == 1
