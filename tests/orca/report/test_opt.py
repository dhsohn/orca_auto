from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca.report.composer import collect_html_report_parts
from orca_auto.orca.report.opt import OptReportData
from orca_auto.orca.report.publication import write_job_html_report
from orca_auto.orca.statuses import AnalyzerStatus
from tests.engine_artifact_helpers import report_generation_target
from tests.orca_output_helpers import (
    FREQ_TS_BLOCK,
    write_opt_inp,
    write_opt_out,
)


def _opt_data(reaction_dir: Path, state: dict[str, Any]) -> OptReportData:
    parts = collect_html_report_parts(reaction_dir, state)
    assert parts is not None and parts.opt is not None
    return parts.opt


def _state(reaction_dir: Path, out_path: Path, *, reason: str) -> dict[str, Any]:
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
                "analyzer_reason": reason,
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-03T01:00:00+00:00",
                "ended_at": "2026-07-03T02:15:00+00:00",
            }
        ],
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": reason,
            "completed_at": "2026-07-03T02:15:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_collect_opt_report_parses_cycles_and_convergence(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)

    data = _opt_data(tmp_path, _state(tmp_path, out_path, reason="normal_termination"))

    assert data is not None
    assert data.kind == "opt"
    assert [cycle for cycle, _ in data.steps] == [1, 2, 3]
    assert data.final_energy == pytest.approx(-100.0052)
    assert data.opt_converged
    assert data.imaginary_count is None
    assert data.mode_summaries == ()


def test_collect_opt_report_skips_contentless_final_attempt(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)
    dead_out = tmp_path / "rxn_retry.out"
    dead_out.write_text("ORCA crashed before the first cycle\n", encoding="utf-8")

    state = _state(tmp_path, out_path, reason="normal_termination")
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
            "started_at": "2026-07-03T02:16:00+00:00",
            "ended_at": "2026-07-03T02:16:30+00:00",
        }
    )
    data = _opt_data(tmp_path, state)

    assert data is not None
    assert [cycle for cycle, _ in data.steps] == [1, 2, 3]
    assert data.final_energy == pytest.approx(-100.0052)
    assert data.opt_converged


def test_opt_report_footer_omits_a_missing_final_output(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)
    missing_out = tmp_path / "rxn_retry.out"
    state = _state(tmp_path, out_path, reason="normal_termination")
    state["attempts"].append({"index": 2, "out_path": str(missing_out)})
    state["final_result"]["last_out_path"] = str(missing_out)

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Optimization convergence" in text
    # The recorded final output is gone: the footer names nothing rather than
    # the earlier attempt's file or the missing one.
    assert "last output:" not in text
    assert "rxn_retry.out" not in text
    assert "<code>rxn.out</code>" not in text


@pytest.mark.parametrize(
    ("route", "kind"),
    [
        ("! SloppyOpt B3LYP def2-SVP", "opt"),
        ("! CrudeOpt B3LYP def2-SVP", "opt"),
        # Partial optimizations still get the optimization report, under a
        # kind that claims no minimum.
        ("! OptH B3LYP def2-SVP", "partial"),
        ("! L-OptH B3LYP def2-SVP", "partial"),
        ("! TightOpt OptH B3LYP def2-SVP", "partial"),
        ("! QMMMOpt B3LYP def2-SVP", "partial"),
        ("! QMMMOpt/pDynamo B3LYP def2-SVP", "partial"),
        ("! SurfCrossOpt B3LYP def2-SVP", "partial"),
        ("! MECP-Opt B3LYP def2-SVP", "partial"),
        ("! CI-Opt B3LYP def2-SVP", "partial"),
        ("! ConicalIntersect-Opt B3LYP def2-SVP", "partial"),
    ],
)
def test_every_optimization_gets_the_opt_report(tmp_path: Path, route: str, kind: str) -> None:
    write_opt_inp(tmp_path / "rxn.inp", route)
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)

    parts = collect_html_report_parts(
        tmp_path, _state(tmp_path, out_path, reason="normal_termination")
    )

    assert parts is not None
    assert parts.opt is not None
    assert parts.opt.kind == kind
    assert parts.sp is None


def test_partial_opt_report_makes_no_minimum_claim(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! MECP-Opt Freq B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path, freq_block=FREQ_TS_BLOCK)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="normal_termination"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Partial Opt report" in text
    assert "Optimization convergence" in text
    assert "Imaginary frequencies" in text
    assert "-410.2" in text
    assert "SP report" not in text
    assert "minimum" not in text
    assert "expected" not in text


@pytest.mark.parametrize(
    "route",
    [
        "! Opt B3LYP def2-SVP",
        "! OptH B3LYP def2-SVP",
        "! Opt OptH B3LYP def2-SVP",
        "! TightOpt OptH B3LYP def2-SVP",
    ],
)
def test_relaxed_scan_of_any_optimization_gets_the_scan_report(tmp_path: Path, route: str) -> None:
    (tmp_path / "rxn.inp").write_text(
        f"{route}\n%geom\n  Scan\n    B 0 1 = 1.0, 2.0, 5\n  end\nend\n* xyzfile 0 1 input.xyz\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)

    parts = collect_html_report_parts(
        tmp_path, _state(tmp_path, out_path, reason="normal_termination")
    )

    assert parts is not None
    assert parts.scan is not None
    assert parts.opt is None
    assert parts.sp is None


def test_opt_report_html_renders_convergence_chart(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="normal_termination"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "Opt report" in text
    assert "Optimization convergence" in text
    assert "<polyline" in text
    assert "initial Opt" in text
    assert "converged" in text
    assert "No frequency calculation found" in text


def test_attempt_table_normalizes_live_analyzer_status_enum(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! Opt B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path)
    state = _state(tmp_path, out_path, reason="normal_termination")
    state["attempts"][0]["analyzer_status"] = AnalyzerStatus.COMPLETED

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert '<td class="ok">completed<div class="sub">normal_termination</div></td>' in text
    assert "AnalyzerStatus.COMPLETED" not in text


def test_frequency_without_mode_vectors_is_not_reported_as_missing_calculation(
    tmp_path: Path,
) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! OptTS B3LYP def2-SVP Freq")
    out_path = tmp_path / "rxn.out"
    frequency_only = FREQ_TS_BLOCK.split("------------\nNORMAL MODES", maxsplit=1)[0]
    write_opt_out(out_path, freq_block=frequency_only)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="ts_criteria_met"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Frequency values were parsed" in text
    assert "no usable normal-mode displacement vectors were available" in text
    assert "No frequency calculation found" not in text


def test_optts_report_summarizes_imaginary_mode(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! OptTS B3LYP def2-SVP Freq")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path, freq_block=FREQ_TS_BLOCK)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="ts_criteria_met"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "TS report" in text
    assert "initial OptTS" in text
    assert "imaginary mode" in text
    assert "-410.2" in text
    assert "as expected for a TS" in text
    assert "TS criteria met" in text


def test_opt_report_flags_unexpected_imaginary_mode(tmp_path: Path) -> None:
    write_opt_inp(tmp_path / "rxn.inp", "! Opt Freq B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_opt_out(out_path, freq_block=FREQ_TS_BLOCK)

    path = write_job_html_report(
        tmp_path,
        _state(tmp_path, out_path, reason="normal_termination"),
        generation_target=report_generation_target(tmp_path),
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "Opt report" in text
    assert "expected 0 for a minimum" in text


def test_opt_card_prefers_the_final_output_and_labels_an_earlier_frequency(
    tmp_path: Path,
) -> None:
    # The SI block reads only the final output. When that output has no
    # frequency section but an earlier attempt does, the card used to show the
    # earlier count with no label while the SI warned "no frequency
    # calculation"; the card now says where the count came from.
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    write_opt_inp(reaction_dir / "rxn.inp", "! Opt Freq B3LYP def2-SVP")
    first_out = reaction_dir / "rxn.out"
    final_out = reaction_dir / "rxn.retry01.out"
    write_opt_out(first_out, freq_block=FREQ_TS_BLOCK)
    write_opt_out(final_out)
    state = _state(reaction_dir, final_out, reason="completed")
    state["attempts"].insert(
        0,
        {
            **state["attempts"][0],
            "index": 1,
            "out_path": str(first_out),
            "analyzer_status": "failed",
        },
    )
    state["attempts"][1]["index"] = 2

    data = _opt_data(reaction_dir, state)

    assert data is not None
    assert data.imaginary_count == 1
    assert data.frequency_attempt_index == 1
    assert data.frequency_from_earlier_attempt is True
    assert "from attempt 1, not the final output" in _imaginary_note_for(data)


def test_opt_card_final_output_frequency_is_not_labeled(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    write_opt_inp(reaction_dir / "rxn.inp", "! Opt Freq B3LYP def2-SVP")
    out_path = reaction_dir / "rxn.out"
    write_opt_out(out_path, freq_block=FREQ_TS_BLOCK)
    state = _state(reaction_dir, out_path, reason="completed")

    data = _opt_data(reaction_dir, state)

    assert data is not None
    assert data.imaginary_count == 1
    assert data.frequency_attempt_index == 1
    assert data.frequency_from_earlier_attempt is False
    assert "not the final output" not in _imaginary_note_for(data)


def test_opt_card_matches_the_final_attempt_through_a_symlinked_path(tmp_path: Path) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    write_opt_inp(reaction_dir / "rxn.inp", "! Opt Freq B3LYP def2-SVP")
    out_path = reaction_dir / "rxn.out"
    write_opt_out(out_path, freq_block=FREQ_TS_BLOCK)
    alias = tmp_path / "alias"
    alias.symlink_to(reaction_dir, target_is_directory=True)
    state = _state(reaction_dir, out_path, reason="completed")
    # The attempt row keeps the alias path; the final result holds the resolved one.
    state["attempts"][0]["out_path"] = str(alias / "rxn.out")

    data = _opt_data(reaction_dir, state)

    assert data is not None
    assert data.imaginary_count == 1
    assert data.frequency_attempt_index == 1
    assert data.frequency_from_earlier_attempt is False


def _imaginary_note_for(data: Any) -> str:
    from orca_auto.orca.report.opt import _imaginary_note

    return _imaginary_note(data)
