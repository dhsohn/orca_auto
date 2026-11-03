"""One publication's evidence and artifact results are explicit values.

The publisher collects :class:`PublicationEvidence` once and turns each
optional report into an :class:`ArtifactResult`; ``machine.json`` is built from
those results, not from a shared mutable map. The renderers return text and
never write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orca_auto.orca import route_rules
from orca_auto.orca.machine_observation import (
    REPORT_GENERATION_FAILED,
    REPORT_NOT_APPLICABLE,
    REPORT_PRODUCED,
    ArtifactResult,
)
from orca_auto.orca.report import composer, publication, si
from orca_auto.orca.report.publication import write_report_files
from tests.engine_artifact_helpers import bind_report_generation
from tests.orca_output_helpers import sp_out_text

_SP_INP = "! wB97M-V def2-TZVPP\n* xyz 0 1\nC 0 0 0\nH 0 0 1.1\n*\n"
# Plain MD has neither an HTML report nor an SI block.
_MD_INP = "! MD B3LYP def2-SVP\n* xyz 0 1\nC 0 0 0\nH 0 0 1.1\n*\n"


def test_artifact_results_keep_produced_absent_and_failed_apart(tmp_path: Path) -> None:
    produced = ArtifactResult.produced("human-report", tmp_path / "job_report.html")

    assert produced.outcome == REPORT_PRODUCED
    assert ArtifactResult.not_applicable("human-report").outcome == REPORT_NOT_APPLICABLE
    assert ArtifactResult.failed("supporting-information").outcome == REPORT_GENERATION_FAILED
    assert ArtifactResult.failed("supporting-information").path is None
    # A path only on a produced report, and only the optional artifact IDs.
    with pytest.raises(ValueError, match="only a produced report"):
        ArtifactResult("human-report", REPORT_GENERATION_FAILED, tmp_path / "x.html")
    with pytest.raises(ValueError, match="only a produced report"):
        ArtifactResult("human-report", REPORT_PRODUCED)
    with pytest.raises(ValueError, match="optional report artifact"):
        ArtifactResult.not_applicable("machine-observation")
    with pytest.raises(ValueError, match="report outcome"):
        ArtifactResult("human-report", "skipped")


def _sp_job(tmp_path: Path, inp_text: str = _SP_INP) -> dict[str, Any]:
    (tmp_path / "rxn.inp").write_text(inp_text, encoding="utf-8")
    state: dict[str, Any] = {
        "job_id": "job_sp",
        "run_id": "run_sp",
        "reaction_dir": str(tmp_path),
        "selected_inp": str(tmp_path / "rxn.inp"),
        "status": "completed",
        "started_at": "2026-07-03T01:00:00+00:00",
        "updated_at": "2026-07-03T02:15:30+00:00",
    }
    generation = bind_report_generation(tmp_path, state)
    out_path = generation / "rxn.out"
    out_path.write_text(sp_out_text(), encoding="utf-8")
    state["attempts"] = [
        {
            "index": 1,
            "inp_path": state["selected_inp"],
            "out_path": str(out_path),
            "return_code": 0,
            "analyzer_status": "completed",
            "analyzer_reason": "normal_termination",
            "markers": {},
            "patch_actions": [],
            "started_at": "2026-07-03T01:00:00+00:00",
            "ended_at": "2026-07-03T02:15:00+00:00",
        }
    ]
    state["final_result"] = {
        "status": "completed",
        "analyzer_status": "completed",
        "reason": "normal_termination",
        "completed_at": "2026-07-03T02:15:30+00:00",
        "last_out_path": str(out_path),
    }
    return state


def test_one_publication_reads_the_selected_route_once_for_both_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _sp_job(tmp_path)
    calls: dict[str, int] = {"publication": 0, "renderers": 0}
    real = route_rules.route_facts

    def counted(name: str) -> Any:
        def read(*args: Any, **kwargs: Any) -> Any:
            calls[name] += 1
            return real(*args, **kwargs)

        return read

    monkeypatch.setattr(publication, "route_facts", counted("publication"))
    monkeypatch.setattr(composer, "route_facts", counted("renderers"))
    monkeypatch.setattr(si, "route_facts", counted("renderers"))

    reports = write_report_files(tmp_path, state)

    assert {"report_html", "si_block", "report_json"} <= set(reports)
    assert calls == {"publication": 1, "renderers": 0}


def test_the_renderers_return_text_and_never_write(tmp_path: Path) -> None:
    state = _sp_job(tmp_path)
    before = sorted(path.name for path in tmp_path.rglob("*"))

    html = composer.compose_job_report_html(tmp_path, state)
    block = si.render_si_block(tmp_path, state)

    assert html is not None and "<html" in html.lower()
    assert block is not None and block.strip()
    assert sorted(path.name for path in tmp_path.rglob("*")) == before


def _generation(state: dict[str, Any]) -> Path:
    return Path(state["attempts"][0]["out_path"]).parent


def test_a_job_without_reports_removes_stale_ones_from_its_own_generation(
    tmp_path: Path,
) -> None:
    state = _sp_job(tmp_path, _MD_INP)
    generation = _generation(state)
    (generation / "job_report.html").write_text("stale", encoding="utf-8")
    (generation / "si_block.md").write_text("stale", encoding="utf-8")

    reports = write_report_files(tmp_path, state)

    assert set(reports) == {"report_json"}
    assert not (generation / "job_report.html").exists()
    assert not (generation / "si_block.md").exists()


def test_report_removal_never_touches_a_directory_that_replaced_the_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The generation is verified, then replaced at the same path before the
    # optional reports are decided. Removing the not-applicable reports must
    # refuse the replacement exactly as writing them would.
    state = _sp_job(tmp_path, _MD_INP)
    generation = _generation(state)
    real_route = publication._selected_route

    def replace_generation(current: Any) -> Any:
        generation.rename(generation.with_name(generation.name + ".moved"))
        generation.mkdir()
        (generation / "job_report.html").write_text("other", encoding="utf-8")
        (generation / "si_block.md").write_text("other", encoding="utf-8")
        return real_route(current)

    monkeypatch.setattr(publication, "_selected_route", replace_generation)

    with pytest.raises(ValueError, match="parent directory identity changed"):
        write_report_files(tmp_path, state)

    assert (generation / "job_report.html").read_text(encoding="utf-8") == "other"
    assert (generation / "si_block.md").read_text(encoding="utf-8") == "other"
    assert not (generation / "machine.json").exists()


@pytest.mark.parametrize("existing", ["[]", "null", "3", '"text"', "{"])
def test_an_existing_machine_json_that_is_not_an_object_blocks_publication(
    tmp_path: Path, existing: str
) -> None:
    # Only a missing machine.json means "not yet published". A present one
    # that cannot be an observation fails closed before any writer runs.
    state = _sp_job(tmp_path)
    generation = _generation(state)
    files = {
        "machine.json": existing,
        "job_report.html": "<p>earlier report</p>",
        "si_block.md": "earlier block",
    }
    for name, text in files.items():
        (generation / name).write_text(text, encoding="utf-8")

    with pytest.raises(RuntimeError, match="existing machine observation is invalid"):
        write_report_files(tmp_path, state)

    for name, text in files.items():
        assert (generation / name).read_text(encoding="utf-8") == text, name
