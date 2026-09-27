from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca import evidence
from orca_auto.orca.report import write_job_html_report
from orca_auto.orca.report.composer import collect_html_report_parts
from orca_auto.orca.report.irc import IrcReportData, parse_irc_output
from orca_auto.orca.report.publication import write_report_files
from tests.engine_artifact_helpers import bind_report_generation, report_generation_target
from tests.orca_output_helpers import (
    IRC_BLOCK,
    IRC_MONITOR_BLOCK,
    write_irc_inp,
    write_irc_out,
)


def _irc_data(reaction_dir: Path, state: dict[str, Any]) -> IrcReportData:
    parts = collect_html_report_parts(reaction_dir, state)
    assert parts is not None and parts.irc is not None
    return parts.irc


def _state(
    reaction_dir: Path,
    out_path: Path,
    *,
    extra_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = [
        {
            "index": 1,
            "inp_path": str(reaction_dir / "rxn.inp"),
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {"irc_marker_found": True},
            "patch_actions": [],
            "started_at": "2026-07-07T01:00:00+00:00",
            "ended_at": "2026-07-07T01:12:00+00:00",
        }
    ]
    attempts.extend(extra_attempts or [])
    return {
        "job_id": "job_irc",
        "run_id": "run_irc",
        "reaction_dir": str(reaction_dir),
        "selected_inp": str(reaction_dir / "rxn.inp"),
        "status": "completed",
        "started_at": "2026-07-07T01:00:00+00:00",
        "updated_at": "2026-07-07T01:12:00+00:00",
        "attempts": attempts,
        "final_result": {
            "status": "completed",
            "analyzer_status": "completed",
            "reason": "normal_termination",
            "completed_at": "2026-07-07T01:12:30+00:00",
            "last_out_path": str(out_path),
        },
    }


def test_parse_irc_output_reads_settings_iterations_and_path_summary(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")

    parsed = parse_irc_output(out_path)

    assert parsed.irc_marker_found
    assert parsed.settings[0].label == "Nr. of atoms"
    assert any(setting.label == "Max. no of cycles MaxIter" for setting in parsed.settings)
    assert any(setting.value == "2.000 mEh" for setting in parsed.settings)
    assert any(setting.value == "job_IRC_Full_trj.xyz" for setting in parsed.settings)
    assert [point.direction for point in parsed.iterations] == [
        "FORWARD",
        "FORWARD",
        "FORWARD",
        "BACKWARD",
        "BACKWARD",
    ]
    assert parsed.iterations[2].delta_e_kcal == pytest.approx(-33.081)
    assert len(parsed.path_points) == 5
    ts = next(point for point in parsed.path_points if point.marker == "TS")
    assert ts.step == 3
    assert ts.energy_hartree == pytest.approx(-343.99728)


def test_parse_irc_output_accepts_monitored_internal_columns(tmp_path: Path) -> None:
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC", irc_block=IRC_MONITOR_BLOCK)

    parsed = parse_irc_output(out_path)

    assert [(point.direction, point.iteration) for point in parsed.iterations] == [
        ("FORWARD", 0),
        ("FORWARD", 1),
        ("BACKWARD", 0),
        ("BACKWARD", 1),
    ]
    assert parsed.iterations[1].delta_e_kcal == pytest.approx(-0.044911)
    assert parsed.iterations[3].rms_gradient == pytest.approx(0.000141)
    assert [point.step for point in parsed.path_points] == [1, 2, 3, 4, 5]
    assert [point.marker for point in parsed.path_points] == ["", "", "TS", "", ""]
    assert parsed.path_points[2].energy_hartree == pytest.approx(-1613.778834)
    assert parsed.path_points[0].relative_kcal == pytest.approx(-0.061075)
    assert parsed.path_points[4].rms_gradient == pytest.approx(0.000128)


def test_irc_report_with_monitored_internals_renders_path_profile(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC", irc_block=IRC_MONITOR_BLOCK)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "No IRC path-summary points were parsed" not in text
    assert "IRC path profile" in text
    assert "TS marker" in text
    assert "5 path pts, 4 IRC iter" in text


def test_collect_irc_report_data_summarizes_path(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")

    data = _irc_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert data.orca_version == "6.0.1"
    assert data.path_points[-1].relative_kcal == pytest.approx(-33.081)
    assert data.attempts[0].detail == "5 path pts, 5 IRC iter"
    assert data.optimization_steps == ()


def test_collect_irc_report_data_skips_contentless_final_attempt(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")
    dead_out = tmp_path / "rxn_retry.out"
    dead_out.write_text("ORCA crashed before the IRC driver started\n", encoding="utf-8")

    state = _state(
        tmp_path,
        out_path,
        extra_attempts=[
            {
                "index": 2,
                "inp_path": str(tmp_path / "rxn.inp"),
                "out_path": str(dead_out),
                "return_code": 1,
                "analyzer_status": "failed",
                "analyzer_reason": "abnormal_termination",
                "markers": {},
                "patch_actions": [],
                "started_at": "2026-07-07T01:13:00+00:00",
                "ended_at": "2026-07-07T01:13:30+00:00",
            }
        ],
    )
    data = _irc_data(tmp_path, state)

    assert data is not None
    assert len(data.path_points) == 5
    assert data.irc_marker_found


@pytest.mark.parametrize(
    "initial_has_data,trailing_kind",
    [(True, None), (True, "empty"), (True, "freq"), (True, "missing"), (False, "empty")],
    ids=["complete", "trailing-empty", "trailing-freq", "trailing-missing", "only-empty"],
)
def test_irc_report_decodes_each_attempt_output_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_has_data: bool,
    trailing_kind: str | None,
) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(
        out_path,
        route="! B3LYP def2-SVP IRC",
        irc_block=IRC_BLOCK if initial_has_data else "",
    )
    existing_outputs = [str(out_path)]
    state = _state(tmp_path, out_path)
    if trailing_kind is not None:
        trailing_out = tmp_path / "rxn_retry.out"
        if trailing_kind == "freq":
            write_irc_out(trailing_out, route="! B3LYP def2-SVP Freq", freq=True, irc_block="")
        elif trailing_kind == "empty":
            trailing_out.write_text(
                "ORCA crashed before the IRC driver started\n", encoding="utf-8"
            )
        if trailing_out.exists():
            existing_outputs.append(str(trailing_out))
        state["attempts"].append({"index": 2, "out_path": str(trailing_out)})
        state["final_result"]["last_out_path"] = str(trailing_out)

    # Every IRC fact (path table, final result, frequencies, optimization
    # progress) comes from one decoded snapshot per attempt output.
    read_paths: list[str] = []
    original_read = evidence.read_orca_text

    def tracked_read(path: str) -> str:
        read_paths.append(path)
        return original_read(path)

    monkeypatch.setattr(evidence, "read_orca_text", tracked_read)

    data = _irc_data(tmp_path, state)

    assert data is not None
    assert len(data.path_points) == (5 if initial_has_data else 0)
    assert data.irc_marker_found is initial_has_data
    assert data.attempts[0].detail == ("5 path pts, 5 IRC iter" if initial_has_data else "")
    if trailing_kind is not None:
        assert data.attempts[-1].detail == ""
    assert data.imaginary_count == (1 if trailing_kind == "freq" else None)
    assert sorted(read_paths) == sorted(existing_outputs)


def test_irc_report_footer_omits_a_missing_final_output(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")
    missing_out = tmp_path / "rxn_retry.out"
    state = _state(tmp_path, out_path, extra_attempts=[{"index": 2, "out_path": str(missing_out)}])
    state["final_result"]["last_out_path"] = str(missing_out)

    path = write_job_html_report(
        tmp_path, state, generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "IRC path profile" in text
    assert "last output:" not in text
    assert "rxn_retry.out" not in text
    assert "<code>rxn.out</code>" not in text


def test_irc_report_html_renders_path_profile(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path == report_generation_target(tmp_path)[0] / "job_report.html"
    text = path.read_text(encoding="utf-8")
    assert "IRC report" in text
    assert "IRC path profile" in text
    assert "TS marker" in text
    assert "path endpoint 1" in text
    assert "job_IRC_Full_trj.xyz" in text
    assert "kcal mol⁻¹" in text
    assert "<polyline" in text
    assert "TS optimization convergence" not in text


def test_irc_report_does_not_publish_unverified_electronic_state(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")
    out_path.write_text(out_path.read_text().replace("|  2> * xyz 0 1", ""))

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "0 / 1" not in text
    assert "unavailable" in text


def test_combined_optts_freq_irc_route_renders_composite_sections(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! OptTS Freq IRC B3LYP def2-SVP")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! OptTS Freq IRC B3LYP def2-SVP", freq=True, opt=True)

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "IRC report" in text
    assert "TS report" not in text
    assert "Calculation summary" in text
    assert "TS optimization convergence" in text
    assert "Vibrational summary" in text
    assert "IRC path profile" in text
    assert "TS opt cycles" in text
    assert text.count('<div class="metric-label">Final energy</div>') == 1
    assert text.count('<div class="metric-label">Imaginary frequencies</div>') == 1


def test_irc_report_with_missing_path_summary_has_fallback(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(
        out_path,
        route="! B3LYP def2-SVP IRC",
        irc_block=IRC_BLOCK.split("IRC PATH SUMMARY", maxsplit=1)[0],
    )

    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "No IRC path-summary points were parsed" in text
    assert "IRC setup" in text


def test_multiline_route_classifies_ts_correctly(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP\n! OptTS Freq IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! OptTS Freq IRC B3LYP def2-SVP", freq=True, opt=True)

    data = _irc_data(tmp_path, _state(tmp_path, out_path))

    assert data is not None
    assert "OptTS" in " ".join(data.header.route_lines)
    assert data.ts_route
    path = write_job_html_report(
        tmp_path, _state(tmp_path, out_path), generation_target=report_generation_target(tmp_path)
    )
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "TS optimization convergence" in text
    assert "TS opt cycles" in text
    assert "expected 1" in text


def test_neb_trajectory_not_captured_as_irc_setting(tmp_path: Path) -> None:
    neb_plus_irc_block = ("Writing initial trajectory to file  .... neb_init.xyz\n\n") + IRC_BLOCK
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! NEB-TS IRC B3LYP def2-SVP", irc_block=neb_plus_irc_block)

    parsed = parse_irc_output(out_path)

    assert not any("neb_init" in s.value for s in parsed.settings)
    assert any("job_IRC_Full_trj.xyz" in s.value for s in parsed.settings)


def test_write_report_files_emits_irc_html_and_summary_si(tmp_path: Path) -> None:
    write_irc_inp(tmp_path / "rxn.inp", "! B3LYP def2-SVP IRC")
    out_path = tmp_path / "rxn.out"
    write_irc_out(out_path, route="! B3LYP def2-SVP IRC")

    state = _state(tmp_path, out_path)
    generation = bind_report_generation(tmp_path, state)
    reports = write_report_files(tmp_path, state)

    assert reports["report_html"] == str(generation / "job_report.html")
    assert reports["si_block"] == str(generation / "si_block.md")
    si_text = (generation / "si_block.md").read_text(encoding="utf-8")
    assert "IRC validation summary" in si_text
    assert "Storing full IRC trajectory in: job_IRC_Full_trj.xyz" in si_text
    assert "C      0.000000" not in si_text
